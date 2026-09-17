"""修复提交器：从修复基线分支建 fix_bug_yyyyMMdd_<traceId> 分支，提交修复文件并 push。

优先走 Agent 网关（POST /fix/commit，网关本地独立 worktree 提交并 push，
本服务无需直连 GitLab——prod 到内网 GitLab 路由不通时仍可用）；
网关不可用/未启用写 token 时降级 GitLab API 直连（建分支 → 服务端落 commit → 反显 commit 信息）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from app.config import AppConfig
from app.providers.agent_gateway import AgentGatewayClient
from app.providers.gitlab import GitLabClient

log = logging.getLogger(__name__)

#: 提交信息 AI 标识前缀：提交使用人工 token，GitLab 记录的 author 无法区分
#: AI 自动提交与人工提交，靠此前缀在提交记录里区分
AI_COMMIT_PREFIX = "[AI]"

#: 分支后缀合法字符（字母数字下划线点横线，对齐 Java \w 的 ASCII 语义）
_BRANCH_SUFFIX_CLEAN = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class CommitResult:
    branch: str
    short_id: str | None
    web_url: str | None
    message: str | None


def build_branch_name(trace_id: str | None, now: datetime | None = None) -> str:
    """fix 分支名 fix_bug_yyyyMMdd_<traceId>：同一天多条 bug 各自独立分支，
    互不混入提交且避免并发建分支冲突；traceId 清洗后为空用时分秒兜底。"""
    moment = now or datetime.now()
    suffix = _BRANCH_SUFFIX_CLEAN.sub("", trace_id or "")
    if not suffix:
        suffix = moment.strftime("%H%M%S")
    return f"fix_bug_{moment:%Y%m%d}_{suffix}"


class FixCommitter:
    def __init__(self, config: AppConfig, gateway: AgentGatewayClient, gitlab: GitLabClient):
        self._config = config
        self._gateway = gateway
        self._gitlab = gitlab

    def commit_fix(self, service: str, file_path: str, file_content: str, commit_message: str,
                   base_branch: str | None, trace_id: str | None) -> CommitResult | None:
        """在 fix 分支上提交修复文件；失败返回 None。"""
        git = self._config.analyse.git
        message = f"{AI_COMMIT_PREFIX} {(commit_message or '').strip()}"
        branch = build_branch_name(trace_id)
        ref = base_branch or git.fix_base_branch or "prod"

        # 网关优先：在网关本地 workspace 提交并 push
        if self._gateway.commit_available:
            via_gateway = self._gateway.commit_fix(service, branch, ref, file_path, file_content, message)
            if via_gateway is not None:
                return CommitResult(via_gateway.branch, via_gateway.short_id,
                                    via_gateway.web_url, via_gateway.message)
            log.warning("网关修复提交失败，降级 GitLab API 直连, service=%s, branch=%s", service, branch)

        # ---- GitLab API 直连兜底 ----
        if not git.api_ready:
            log.warning("GitLab 未配置，无法提交修复")
            return None
        project_id = git.projects.get(service) or self._gitlab.search_project(service)
        if project_id is None:
            log.warning("GitLab 项目定位失败, service=%s", service)
            return None
        if not self._gitlab.ensure_branch(project_id, branch, ref):
            return None
        if not self._gitlab.upsert_file(project_id, branch, file_path, file_content, message):
            return None
        # 反显真实 commit 信息（shortId/webUrl/message），与 GitLab 提交记录一字不差
        info = self._gitlab.last_commit(project_id, branch)
        if info is None:
            return CommitResult(branch, None, None, message)
        return CommitResult(branch, info.short_id, info.web_url, info.message or message)
