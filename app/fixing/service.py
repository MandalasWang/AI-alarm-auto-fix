"""修复执行服务：LLM 生成修复 → 提交 fix 分支 → 群发结果通知 → 结果落库（含忽略）。

修复状态（落库 alarm_analyze_result.fix_status，效果看板统计用）：
- FIXED          已提交修复 commit（永久防重，不允许再次修复）
- NOT_NEEDED     确认无需任何处理（LLM 判定 non_code 或人工忽略），不计为失败（永久防重）
- MANUAL_PENDING 待人工处理（SQL 变更/配置/权限等，附操作步骤），永久防重
- HANDOVER       已转人工排查（上下游依赖问题，分析归因落库即定），永久防重
- FAILED         失败（生成/提交/执行异常），不标记完结，允许重试

并发语义（Redis 两把锁，均由异步任务 finally 释放）：
- 执行锁 fix:lock:<traceId>（10min）：防同告警双击/并发触发两轮 LLM 生成；
- 服务串行锁 fix:service-lock:<service>（15min，value=持有者 traceId）：fix_bug_yyyyMMdd 分支
  按服务共用，同服务并发修复会在提交环节分支竞争，快速失败提示稍后再试。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.config import Keys
from app.core import metrics
from app.core.fingerprint import (
    FIX_STATUS_HANDOVER, FIX_STATUS_MANUAL_PENDING, FIX_STATUS_NOT_NEEDED,
)
from app.core.kv import KeyValueStore
from app.core.text import truncate_chars
from app.feishu.cards import (
    build_fix_notify_text, build_fix_notify_variables, build_manual_notify_text,
    build_manual_notify_variables, build_template_card,
)
from app.feishu.client import FeishuClient
from app.fixing.committer import FixCommitter
from app.fixing.generator import FixGenerator, FixOutput
from app.models.alarm import AlarmAnalyseResult
from app.storage.alarm_repository import AlarmRepository

log = logging.getLogger(__name__)

FIX_STATUS_FIXED = "FIXED"
FIX_STATUS_FAILED = "FAILED"
FIX_TYPE_MANUAL = "manual"
# NOT_NEEDED / HANDOVER / MANUAL_PENDING 由 app.core.fingerprint 导入（同值单一来源）

#: 执行锁 TTL：需覆盖一轮 LLM 生成（分钟级）
EXEC_LOCK_TTL_SECONDS = 10 * 60
#: 服务串行锁 TTL：大于执行锁，兜底执行中异常中断后锁泄漏
SERVICE_LOCK_TTL_SECONDS = 15 * 60

_FIX_SUMMARY_MAX_BYTES = 6 * 1024

_FIX_STATUS_LABELS = {
    FIX_STATUS_FIXED: "已修复",
    FIX_STATUS_NOT_NEEDED: "无需处理",
    FIX_STATUS_FAILED: "未修复",
    FIX_STATUS_HANDOVER: "已转人工排查",
    FIX_STATUS_MANUAL_PENDING: "待人工处理",
}

_DONE_REJECT_MESSAGES = {
    FIX_STATUS_NOT_NEEDED: "该告警已忽略（判定无需处理），请勿重复点击",
    FIX_STATUS_HANDOVER: "该告警已转人工排查（上下游依赖问题），请勿重复点击",
    FIX_STATUS_MANUAL_PENDING: "该告警已转人工处理，请勿重复点击",
    FIX_STATUS_FIXED: "该告警已成功修复，请勿重复点击",
}


def is_done_status(fix_status: str | None) -> bool:
    """告警是否已完结（终态，修复/忽略均不再受理）：已修复/已忽略(无需处理)/已转人工/待人工。"""
    return fix_status in (FIX_STATUS_FIXED, FIX_STATUS_NOT_NEEDED,
                          FIX_STATUS_HANDOVER, FIX_STATUS_MANUAL_PENDING)


def done_fix_reject_message(done_status: str) -> str:
    """已完结告警的重复修复/忽略拒绝文案（按完结状态区分提示）。"""
    return _DONE_REJECT_MESSAGES.get(done_status, _DONE_REJECT_MESSAGES[FIX_STATUS_FIXED])


def fix_status_label(fix_status: str) -> str:
    return _FIX_STATUS_LABELS.get(fix_status, fix_status)


def fix_service_key(service: str | None) -> str:
    """服务串行锁的 key 后缀（服务名缺失时用 unknown 兜底，保证加锁/释放路径一致）。"""
    return (service or "").strip() or "unknown"


@dataclass(frozen=True)
class FixAcceptance:
    """修复受理结果：accepted=False 时 message 为给调用方的提示；
    accepted=True 时携带异步执行所需的上下文。"""

    accepted: bool
    message: str
    result: AlarmAnalyseResult | None = None
    record_id: int | None = None


class FixService:
    def __init__(self, config, keys: Keys, store: KeyValueStore, feishu: FeishuClient,
                 generator: FixGenerator, committer: FixCommitter,
                 repository: AlarmRepository | None,
                 validator=None, merger=None):
        """validator: FixValidator 质量闸（None=不校验直接提交）；merger: MergeRequestService（None=不建 MR）。"""
        self._config = config
        self._keys = keys
        self._store = store
        self._feishu = feishu
        self._generator = generator
        self._committer = committer
        self._repository = repository
        self._validator = validator
        self._merger = merger

    # ------------------------------------------------------------------ 受理（接口层同步部分）

    def try_accept(self, trace_id: str) -> FixAcceptance:
        """修复回调的同步校验：防重锁 → 查历史分析 → 已完结校验 → 服务串行锁。

        受理成功后修复在后台异步执行（:meth:`execute_fix`），实际结果经修复通知卡片反馈。
        """
        exec_lock = self._keys.fix_lock + trace_id
        # 执行锁：防止双击/并发触发两轮 LLM 生成（由异步任务结束时释放）
        if not self._store.acquire_lock(exec_lock, "1", EXEC_LOCK_TTL_SECONDS):
            log.info("修复回调正在执行中，忽略。traceId=%s", trace_id)
            return FixAcceptance(False, "修复正在执行中，请稍候")

        # 1. 查历史分析结果（快速操作，同步校验给调用方即时反馈）
        if self._repository is None:
            self._store.release_lock(exec_lock)
            return FixAcceptance(False, "未配置业务库，修复功能不可用")
        try:
            record = self._repository.latest_by_trace_id(trace_id)
        except Exception as exc:  # noqa: BLE001 - 业务库不可用时友好降级，不向接口层抛 500
            self._store.release_lock(exec_lock)
            log.error("查询历史分析结果失败(业务库不可用), traceId=%s, err=%s", trace_id, exc)
            return FixAcceptance(False, "查询分析结果失败（业务库不可用），请稍后重试")
        if record is None:
            self._store.release_lock(exec_lock)
            log.warning("未找到告警分析结果, traceId=%s", trace_id)
            return FixAcceptance(False, "未找到告警分析结果")

        result = AlarmAnalyseResult.from_json(record.analyze_content or "") or AlarmAnalyseResult(
            trace_id=trace_id)
        if not result.service:
            result.service = record.service_name
        if not result.request_uri:
            result.request_uri = record.interface_uri

        # 已完结校验：已修复/已忽略(无需处理)/已转人工/待人工 落库后即永久防重（FAILED 可重试）
        if is_done_status(record.fix_status):
            self._store.release_lock(exec_lock)
            log.info("该告警已完结(fixStatus=%s)，拒绝重复修复。traceId=%s", record.fix_status, trace_id)
            return FixAcceptance(False, done_fix_reject_message(record.fix_status))

        # 同服务串行：同服务并发修复会在提交环节分支竞争，快速失败提示稍后再试
        service_key = fix_service_key(result.service)
        service_lock = self._keys.fix_service_lock + service_key
        if not self._store.acquire_lock(service_lock, trace_id, SERVICE_LOCK_TTL_SECONDS):
            holder = self._store.get(service_lock)
            self._store.release_lock(exec_lock)
            log.info("该服务已有修复在执行中(持有者=%s)，拒绝并发修复。service=%s, traceId=%s",
                     holder, service_key, trace_id)
            return FixAcceptance(False, "该服务正在修复其他告警，请稍后再试")

        log.info("修复任务已提交异步执行, traceId=%s, service=%s", trace_id, service_key)
        return FixAcceptance(True, "受理成功", result=result, record_id=record.id)

    # ------------------------------------------------------------------ 执行（后台异步部分）

    def execute_fix(self, trace_id: str, result: AlarmAnalyseResult, result_id: int | None) -> None:
        """修复执行体：生成 → 提交 → 通知 → 落库，finally 释放两把锁。"""
        fix_status = FIX_STATUS_FAILED
        fix_summary: str | None = None
        branch: str | None = None
        try:
            # 1. 生成修复代码
            fix = self._generator.generate(result)

            # 1.5 质量闸：语法预检 + LLM 自检，不过不提交（转 FAILED，允许重试）
            if fix is not None and fix.fixed and self._validator is not None:
                reason = self._validator.validate(fix, result.service)
                if reason:
                    log.warning("修复质量闸未通过，不提交, traceId=%s, reason=%s", trace_id, reason)
                    fix = FixOutput(fixed=False, fix_type="code",
                                    fix_summary=f"修复自检未通过：{reason}",
                                    original_source=fix.original_source)

            # 2. 提交到 fix_bug_yyyyMMdd 分支（基线=修复基线分支，默认 prod）
            commit = None
            if fix is not None and fix.fixed:
                commit_message = f"fix: 自动修复告警 {trace_id or ''}"
                if fix.fix_summary:
                    commit_message += f" - {fix.fix_summary}"
                commit = self._committer.commit_fix(
                    result.service or "", fix.fixed_file or "", fix.fixed_content or "",
                    commit_message, fix.base_branch, trace_id)

            if commit is not None:
                fix_status = FIX_STATUS_FIXED
                branch = commit.branch
                # 2.5 自动建 MR（合拢最后一公里）：失败不阻断，fix_summary 附 MR 链接
                mr_url = self._create_merge_request(result, trace_id, commit.branch, fix)
                # fix_summary 与 GitLab 提交记录对齐：commit message 原文 + shortId + 链接
                summary = commit.message or f"[AI] fix: 自动修复告警 {trace_id}"
                if commit.short_id:
                    summary += f"\ncommit: {commit.short_id}"
                if commit.web_url:
                    summary += f"\n查看改动: {commit.web_url}"
                if mr_url:
                    summary += f"\nMR: {mr_url}"
                fix_summary = summary
            elif fix is not None and (fix.fix_type or "").lower() == "non_code":
                # 确认无需任何处理，属正确结果，不计为修复失败
                fix_status = FIX_STATUS_NOT_NEEDED
                fix_summary = fix.fix_summary
            elif fix is not None and (fix.fix_type or "").lower() == FIX_TYPE_MANUAL:
                # 需人工介入（SQL 变更/配置/权限等）：转人工处理并附操作步骤，不计为修复失败
                fix_status = FIX_STATUS_MANUAL_PENDING
                fix_summary = fix.manual_steps or fix.fix_summary
            else:
                fix_summary = (fix.fix_summary if fix is not None
                               else "未能生成修复代码（源码片段不足或大模型不可用）")
            log.info("告警修复完成, traceId=%s, fixStatus=%s, branch=%s", trace_id, fix_status, branch)

            # 3. 群发修复结果通知（与告警同群）
            self._send_fix_notify(result, trace_id, fix_status_label(fix_status), branch, fix_summary)

            # 4. 转人工/待人工时补发人工处理通知（附操作步骤）
            if fix_status in (FIX_STATUS_MANUAL_PENDING, FIX_STATUS_HANDOVER):
                self._send_manual_notify(result, trace_id, fix_status, fix_summary)

        except Exception as exc:  # noqa: BLE001
            log.exception("告警修复异步执行失败, traceId=%s", trace_id)
            fix_summary = f"修复执行异常：{exc}"
        finally:
            # 4. 结果落库（含失败，效果看板按 fix_status 统计）；写库失败只记日志不影响主流程
            metrics.FIX_RESULT.labels(fix_status).inc()
            if self._repository is not None and result_id is not None:
                try:
                    self._repository.update_fix_result(
                        result_id, fix_status, truncate_chars(fix_summary or "-", 1000) or "-")
                except Exception:  # noqa: BLE001
                    log.exception("修复结果落库失败, traceId=%s, fixStatus=%s", trace_id, fix_status)
            # 无论成败都释放执行锁与服务串行锁；失败不标记 done，允许用户重试
            self._store.release_lock(self._keys.fix_lock + trace_id)
            self._store.release_lock(self._keys.fix_service_lock + fix_service_key(result.service),
                                     expected_value=trace_id)

    # ------------------------------------------------------------------ MR 合拢

    def _create_merge_request(self, result: AlarmAnalyseResult, trace_id: str,
                              branch: str | None, fix) -> str | None:
        """提交成功后自动建 MR；返回 MR 链接，未启用/失败返回 None（只记日志不阻断）。"""
        if self._merger is None or not branch:
            return None
        try:
            return self._merger.create_for_fix(
                service=result.service or "", trace_id=trace_id, branch=branch,
                base_branch=fix.base_branch if fix else None,
                fix_summary=fix.fix_summary if fix else None,
                proposal=result.proposal or result.analysis)
        except Exception:  # noqa: BLE001 - 建 MR 失败不影响修复结果反馈
            log.exception("自动创建 MR 失败, traceId=%s, branch=%s", trace_id, branch)
            metrics.MR_RESULT.labels("failed").inc()
            return None

    # ------------------------------------------------------------------ 通知

    def _send_fix_notify(self, result: AlarmAnalyseResult, trace_id: str, fix_result: str,
                         branch: str | None, fix_summary: str | None) -> None:
        """发送修复结果通知卡片（模板未配置时文本兜底）；发送失败只记日志。"""
        chat_id = self._config.feishu.default_chat_id
        if not chat_id:
            log.warning("未配置飞书群 chat_id(feishu.default_chat_id)，修复结果通知未发送")
            return

        variables = build_fix_notify_variables(
            fix_result=fix_result,
            service=result.service or "-",
            interface=result.request_uri or "-",
            trace_id=trace_id or "-",
            fix_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            fix_branch=branch or "-",
            fix_summary=fix_summary or "-",
        )
        template_id = self._config.analyse.fix_notify.card_template_id
        try:
            if template_id:
                self._feishu.send_card_to_chat(chat_id, build_template_card(template_id, variables))
            else:
                self._feishu.send_text_to_chat(chat_id, build_fix_notify_text(variables))
            log.info("修复结果通知已发送, traceId=%s, fixResult=%s", trace_id, fix_result)
        except Exception:  # noqa: BLE001
            log.exception("修复结果通知发送失败, traceId=%s", trace_id)

    # ------------------------------------------------------------------ 忽略

    def try_ignore(self, trace_id: str) -> tuple[bool, str]:
        """忽略告警（人工判定无需修复）：落库 fix_status=NOT_NEEDED 永久防重。

        已完结（已修复/已忽略/转人工/待人工）拒绝并按状态提示；自动修复执行中拒绝
        （忽略会落 NOT_NEEDED，执行中的修复完成后会覆盖掉）。
        """
        if self._repository is None:
            return False, "未配置业务库，忽略功能不可用"
        try:
            record = self._repository.latest_by_trace_id(trace_id)
        except Exception as exc:  # noqa: BLE001 - 业务库不可用时友好降级，不向接口层抛 500
            log.error("查询历史分析结果失败(业务库不可用), traceId=%s, err=%s", trace_id, exc)
            return False, "查询分析结果失败（业务库不可用），请稍后重试"
        if record is None:
            log.warning("未找到告警分析结果, traceId=%s", trace_id)
            return False, "未找到告警分析结果"

        # 已完结校验：已修复/已忽略/已转人工/待人工 不再受理忽略
        if is_done_status(record.fix_status):
            log.info("该告警已完结(fixStatus=%s)，忽略无效。traceId=%s", record.fix_status, trace_id)
            return False, done_fix_reject_message(record.fix_status)

        # 修复执行中不允许忽略：否则忽略结果会被执行中的修复覆盖
        if self._store.get(self._keys.fix_lock + trace_id):
            log.info("该告警修复执行中，忽略被拒绝。traceId=%s", trace_id)
            return False, "该告警正在自动修复中，请等待修复结果后再忽略"

        try:
            self._repository.update_fix_result(record.id, FIX_STATUS_NOT_NEEDED, "人工忽略：判定无需修复")
        except Exception as exc:  # noqa: BLE001
            log.exception("忽略落库失败, traceId=%s", trace_id)
            return False, "忽略失败（落库异常），请稍后重试"
        log.info("告警已忽略(原状态=%s), traceId=%s", record.fix_status, trace_id)
        return True, "已忽略（判定无需处理）"

    # ------------------------------------------------------------------ 人工通知

    def _send_manual_notify(self, result: AlarmAnalyseResult, trace_id: str,
                            fix_status: str, steps: str | None) -> None:
        """发送人工处理通知（转人工排查/待人工处理，附操作步骤）；模板未配置时文本兜底。"""
        chat_id = self._config.feishu.default_chat_id
        if not chat_id:
            log.warning("未配置飞书群 chat_id(feishu.default_chat_id)，人工处理通知未发送")
            return
        handover_type = ("已转人工排查（上下游依赖问题）" if fix_status == FIX_STATUS_HANDOVER
                         else "待人工处理（SQL/配置等，需人工介入）")
        variables = build_manual_notify_variables(
            handover_type=handover_type,
            service=result.service or "-",
            interface=result.request_uri or "-",
            trace_id=trace_id or "-",
            handover_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            steps=steps or "-",
        )
        template_id = self._config.analyse.fix_notify.manual_card_template_id
        try:
            if template_id:
                self._feishu.send_card_to_chat(chat_id, build_template_card(template_id, variables))
            else:
                self._feishu.send_text_to_chat(chat_id, build_manual_notify_text(variables))
            log.info("人工处理通知已发送, traceId=%s, handoverType=%s", trace_id, handover_type)
        except Exception:  # noqa: BLE001
            log.exception("人工处理通知发送失败, traceId=%s", trace_id)


__all__ = ["FixService", "FixAcceptance", "fix_status_label", "fix_service_key",
           "is_done_status", "done_fix_reject_message"]
