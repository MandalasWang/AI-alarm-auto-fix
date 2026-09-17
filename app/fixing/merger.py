"""修复 MR 合拢服务：提交成功自动建 MR（带分析摘要）+ fix_bug 分支超时未合并定时催办。

建 MR：修复提交 push 后调用，描述含告警服务/接口/traceId/修复说明/分析建议，
目标分支=修复基线分支；MR 失败（token 无 api 权限等）只记日志，不影响修复结果反馈。

催办扫描（GitLab 侧真相，不依赖本地状态）：遍历 git.projects 配置的项目列出 fix_bug_*
分支，最新 commit 早于 stale_hours 且未合并（merged=false）即为滞留；有打开态 MR 提醒合并，
无 MR 提醒补建。每轮扫描每分支提醒一次（频率=cron），发文本消息（催办无配套卡片模板）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import AppConfig
from app.core import metrics
from app.core.text import truncate_chars
from app.feishu.client import FeishuClient
from app.providers.gitlab import GitLabClient

log = logging.getLogger(__name__)

FIX_BRANCH_PREFIX = "fix_bug_"

#: MR 描述里「分析建议」的截断长度（描述整体 GitLab 上限 1MB，无需抠字节）
_PROPOSAL_MAX_CHARS = 2000


@dataclass(frozen=True)
class StaleBranch:
    """一条滞留的 fix 分支（催办文案素材）。"""

    service: str
    branch: str
    age_hours: int
    mr_url: str | None      # 有打开态 MR 时的链接；None=尚未建 MR
    mr_title: str | None


class MergeRequestService:
    def __init__(self, config: AppConfig, gitlab: GitLabClient, feishu: FeishuClient):
        self._config = config
        self._gitlab = gitlab
        self._feishu = feishu

    # ------------------------------------------------------------------ 建 MR

    def create_for_fix(self, service: str, trace_id: str, branch: str,
                       base_branch: str | None, fix_summary: str | None,
                       proposal: str | None) -> str | None:
        """为修复分支创建 MR，返回 MR 链接；未启用/失败/已存在返回 None（已存在无需重复建）。"""
        git = self._config.analyse.git
        if not (git.mr.enabled and git.api_ready):
            metrics.MR_RESULT.labels("skipped").inc()
            return None
        project_id = git.projects.get(service) or self._gitlab.search_project(service)
        if project_id is None:
            log.warning("MR 创建跳过：GitLab 项目定位失败, service=%s, branch=%s", service, branch)
            metrics.MR_RESULT.labels("skipped").inc()
            return None

        target = base_branch or git.fix_base_branch or git.default_branch or "prod"
        title = f"[AI] fix: 自动修复告警 {trace_id or '-'}（{service}）"
        ok, web_url = self._gitlab.create_merge_request(
            project_id, branch, target, title,
            build_mr_description(service=service, trace_id=trace_id, branch=branch,
                                 base_branch=target, fix_summary=fix_summary,
                                 proposal=proposal),
            remove_source_branch=git.mr.remove_source_branch)
        if not ok:
            metrics.MR_RESULT.labels("failed").inc()
            return None
        metrics.MR_RESULT.labels("created" if web_url else "exists").inc()
        return web_url

    # ------------------------------------------------------------------ 超时未合并催办

    def remind_stale(self) -> int:
        """扫描全部配置项目的 fix_bug_* 滞留分支并发群提醒；返回滞留分支数。"""
        git = self._config.analyse.git
        if not (git.mr.enabled and git.api_ready and git.projects):
            return 0
        now = datetime.now().astimezone()
        stale: list[StaleBranch] = []
        for service, project_id in git.projects.items():
            for branch in self._gitlab.list_branches(project_id, FIX_BRANCH_PREFIX):
                if branch.get("merged"):
                    continue
                created_at = _parse_gitlab_time((branch.get("commit") or {}).get("created_at"))
                age = now - created_at if created_at else None
                if age is None or age < timedelta(hours=git.mr.stale_hours):
                    continue
                name = str(branch.get("name") or "")
                mr = self._gitlab.find_open_merge_request(project_id, name)
                stale.append(StaleBranch(
                    service=service, branch=name, age_hours=max(1, int(age.total_seconds() // 3600)),
                    mr_url=mr.get("web_url") if mr else None,
                    mr_title=mr.get("title") if mr else None,
                ))
        if not stale:
            return 0

        text = build_remind_text(stale, stale_hours=self._config.analyse.git.mr.stale_hours)
        chat_ids = self._config.analyse.git.mr.chat_ids or (
            [self._config.feishu.default_chat_id] if self._config.feishu.default_chat_id else [])
        if not chat_ids:
            log.warning("发现 %s 条滞留修复分支，但未配置催办群（git.mr.chat_ids / feishu.default_chat_id）",
                        len(stale))
            return len(stale)
        for chat_id in chat_ids:
            try:
                self._feishu.send_text_to_chat(chat_id, text)
            except Exception:  # noqa: BLE001 - 催办发送失败只记日志
                log.exception("修复分支催办提醒发送失败, chatId=%s", chat_id)
        log.info("修复分支催办提醒已发送, stale=%s, chats=%s", len(stale), len(chat_ids))
        return len(stale)


# ------------------------------------------------------------------ 报文构建（纯函数，可测）

def build_mr_description(service: str, trace_id: str, branch: str, base_branch: str,
                         fix_summary: str | None, proposal: str | None) -> str:
    """MR 描述：告警上下文 + 修复说明 + 分析建议，评审人不需要回到飞书就能看懂改动背景。"""
    lines = [
        "## 告警自动修复",
        "",
        f"- **服务**: {service or '-'}",
        f"- **traceId**: {trace_id or '-'}",
        f"- **修复分支**: `{branch}`（合并后自动删除）",
        f"- **基线分支**: `{base_branch}`",
        "",
        "## 修复说明",
        "",
        fix_summary or "-",
    ]
    if proposal:
        lines += ["", "## 修复建议（来自告警 AI 分析）", "", truncate_chars(proposal, _PROPOSAL_MAX_CHARS)]
    lines += ["", "---", "*本 MR 由告警自动修复链路创建，请评审后合并。*"]
    return "\n".join(lines)


def build_remind_text(stale: list[StaleBranch], stale_hours: int) -> str:
    """催办文本：逐条列出滞留分支，区分「MR 未合并」与「尚未建 MR」。"""
    lines = [f"⚠️ 告警修复分支滞留提醒：以下 fix 分支超过 {stale_hours} 小时未合并"]
    for index, item in enumerate(stale, start=1):
        if item.mr_url:
            detail = f"MR 待合并: {item.mr_url}"
        else:
            detail = "尚未创建 MR，请评审处理"
        lines.append(f"{index}. [{item.service}] {item.branch}（已滞留 {item.age_hours} 小时）— {detail}")
    lines.append("请及时评审合并或另行处理。")
    return "\n".join(lines)


def _parse_gitlab_time(value) -> datetime | None:
    """解析 GitLab ISO8601 时间（带时区偏移）；解析失败返回 None（该分支本轮跳过）。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.astimezone()
    except ValueError:
        return None


__all__ = ["MergeRequestService", "StaleBranch", "build_mr_description", "build_remind_text",
           "FIX_BRANCH_PREFIX"]
