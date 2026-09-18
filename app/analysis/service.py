"""告警分析主服务：提取 → 指纹重复/复发识别（重复告警短路）→ 取码 → 查库 → 分析 → 落库 → 卡片 → 缓存。

任一环节失败均降级不阻断（与 Java 版原则一致）：
- 取码/查库/LLM 失败 → 对应上下文为空，报告兜底；
- 落库失败 → 只记日志，卡片照发（但回调修复将不可用）；
- 仅「未配置群/模板」视为致命，抛 ConfigError 由后台任务记录。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.analysis.report import ReportGenerator
from app.config import AppConfig
from app.core import metrics
from app.core.errors import ConfigError
from app.core.fingerprint import (
    PROBLEM_DEPENDENCY, PROBLEM_MANUAL_REQUIRED, REPEAT_REPEATED,
    decide_repeat_type, fingerprint, fix_status_of_problem_type,
)
from app.core.kv import KeyValueStore
from app.core.text import truncate_chars
from app.extractors.stack import class_name_to_path, find_exception_class, parse_business_frames
from app.extractors.tables import extract_tables
from app.feishu.cards import (
    build_alarm_card, build_manual_notify_text, build_manual_notify_variables, build_template_card,
)
from app.feishu.client import FeishuClient
from app.models.alarm import AlarmAnalyseResult, AlarmAnalyzeRecord, AlarmLog
from app.providers.agent_gateway import AgentGatewayClient
from app.providers.db_context import DatabaseContextFetcher
from app.providers.gitlab import GitLabClient
from app.storage.alarm_repository import AlarmRepository

log = logging.getLogger(__name__)

CORE_MESSAGE_MAX_LENGTH = 1500
STACK_FRAMES_IN_CORE = 3
DEFAULT_REDIS_TTL_HOURS = 24

_LEVELS = {"INFO", "WARN", "ERROR", "FATAL"}


class AlarmAnalyseService:
    def __init__(
        self,
        config: AppConfig,
        keys,  # app.config.Keys
        store: KeyValueStore,
        feishu: FeishuClient,
        gitlab: GitLabClient,
        gateway: AgentGatewayClient,
        db_context: DatabaseContextFetcher,
        report_generator: ReportGenerator,
        repository: AlarmRepository | None,
        context_lines: int = 15,
    ):
        self._config = config
        self._keys = keys
        self._store = store
        self._feishu = feishu
        self._gitlab = gitlab
        self._gateway = gateway
        self._db_context = db_context
        self._report_generator = report_generator
        self._repository = repository
        self._context_lines = max(0, context_lines)

    # ------------------------------------------------------------------ 主流程

    def handle_alarm(self, alarm: AlarmLog) -> None:
        """完整分析链路（在后台线程池中执行）。异常计数后原样上抛（由后台任务兜底记录）。"""
        try:
            self._do_handle_alarm(alarm)
        except Exception:
            metrics.ALARM_ANALYSE.labels("error").inc()
            raise

    def _do_handle_alarm(self, alarm: AlarmLog) -> None:
        # 1. 提取核心信息（服务/接口/异常/涉及表）+ 计算告警指纹
        result = self._extract(alarm)
        result.fingerprint = fingerprint(
            result.service, result.request_uri, result.exception_class, alarm.stack_trace)

        # 2. 重复/复发识别（分析前置：重复告警短路，省去取码/查库/LLM 分钟级调用）
        prior = self._find_repeat_prior(result)
        if prior is not None:
            result.repeat_type = decide_repeat_type(prior, datetime.now())
            # 链到真根：原告警自身也是重复时沿用其根，避免链式增长
            result.root_id = prior.root_id if prior.root_id is not None else prior.id
            # 复发时携带原修复的处理摘要（分支/commit/MR），卡片可直链核对原修复为何未生效
            if result.repeat_type == REPEAT_RECURRED and prior.fix_summary:
                result.recurred_fix_summary = truncate_chars(prior.fix_summary, 500)
        if result.repeat_type == REPEAT_REPEATED:
            # 继承原告警归因（已判过的结论沿用），落库计数后直接返回：不推卡片、不发通知、不调 LLM
            result.problem_type = prior.problem_type
            self._save_analyze_result(result)
            metrics.ALARM_ANALYSE.labels("repeated").inc()
            log.info("重复告警已压卡片(窗口内同指纹), rootId=%s, traceId=%s, service=%s",
                     result.root_id, result.trace_id, result.service)
            return

        # 3. 按堆栈取出错代码片段（网关读码优先，GitLab API 兜底；失败不影响主流程）
        result.code_context = self._fetch_code_context(alarm)

        # 4. 查相关数据库上下文（表 DDL/行数/样本数据）
        try:
            result.db_context = self._db_context.fetch_context(alarm)
        except Exception as exc:  # noqa: BLE001
            log.warning("查库上下文获取失败(不阻断), source=%s, err=%s", alarm.source, exc)

        # 5. 生成分析报告并拆段（大模型优先、规则兜底；含【问题归因】）
        self._report_generator.generate_and_split(result)

        # 6. 落库（供卡片回调修复查询；失败不阻断，但修复按钮将不可用）；
        #    归因为依赖/需人工/无需处理时映射初始 fix_status（转人工/待人工/无需处理）
        self._save_analyze_result(result)

        # 7. 解析目标群与模板（请求体可覆盖默认配置）
        chat_id = alarm.chat_id or self._config.feishu.default_chat_id
        if not chat_id:
            raise ConfigError("未配置飞书告警接收群 chat_id (feishu.default_chat_id)")
        template_id = alarm.template_id or self._config.feishu.default_card_template_id
        if not template_id:
            raise ConfigError("未配置飞书告警卡片模板 ID (feishu.default_card_template_id)")

        # 8. 构建卡片并发送
        card = build_alarm_card(result, template_id, self._config.feishu.jump_url,
                                self._config.feishu.ignore_jump_url,
                                self._config.feishu.resolve_jump_url)
        log.info("推送飞书告警卡片, service=%s, level=%s, chatId=%s",
                 result.service, result.level, chat_id)
        message_id = self._feishu.send_card_to_chat(chat_id, card)
        metrics.ALARM_ANALYSE.labels("ok").inc()

        # 9. 归因为上下游依赖/需人工介入时自动修复走不通，补发人工处理通知
        self._send_manual_notify_if_needed(result, chat_id)

        # 10. 以 messageId 为键缓存分析结果，供卡片交互回调关联
        if not message_id:
            log.warning("飞书发送未返回 messageId，告警分析结果未缓存，卡片回调将无法关联")
            return
        ttl_hours = self._config.feishu.redis_key_ttl_hours or DEFAULT_REDIS_TTL_HOURS
        self._store.set(self._keys.analyse_cache + message_id, result.to_json(), ttl_hours * 3600)
        log.info("告警分析结果已缓存, messageId=%s, ttlHours=%s", message_id, ttl_hours)

    # ------------------------------------------------------------------ 重复/复发识别

    def _find_repeat_prior(self, result: AlarmAnalyseResult) -> AlarmAnalyzeRecord | None:
        """窗口内同指纹的最新一条历史记录；无指纹或窗口关闭（repeat.window_hours ≤ 0）返回 None。"""
        if not result.fingerprint or self._repository is None:
            return None
        window_hours = self._config.analyse.repeat.window_hours
        if window_hours <= 0:
            return None
        try:
            return self._repository.latest_by_fingerprint(
                result.fingerprint, datetime.now() - timedelta(hours=window_hours))
        except Exception as exc:  # noqa: BLE001 - 指纹查询失败不阻断主流程（按首次告警处理）
            log.warning("指纹重复识别查询失败(不阻断，按首次告警处理), traceId=%s, err=%s",
                        result.trace_id, exc)
            return None

    def _send_manual_notify_if_needed(self, result: AlarmAnalyseResult, chat_id: str) -> None:
        """归因为上下游依赖（转人工排查）/需人工介入（待人工处理）时，发人工处理通知到告警群：

        模板（fix-notify.manual-card-template-id）优先，未配置发文本兜底；发送失败只记日志不阻断。
        """
        if result.problem_type == PROBLEM_DEPENDENCY:
            handover_type = "已转人工排查（上下游依赖问题）"
        elif result.problem_type == PROBLEM_MANUAL_REQUIRED:
            handover_type = "待人工处理（SQL/配置等，需人工介入）"
        else:
            return
        steps = result.proposal or result.analysis or "-"
        try:
            variables = build_manual_notify_variables(
                handover_type=handover_type,
                service=result.service or "-",
                interface=result.request_uri or "-",
                trace_id=result.trace_id or "-",
                handover_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                steps=steps,
            )
            # 结案按钮链接（feishu.resolve_jump_url 配置后下发 resolve_url 变量）
            resolve_jump = self._config.feishu.resolve_jump_url or ""
            if resolve_jump:
                resolve_url = resolve_jump + (result.trace_id or "")
                variables["resolve_url"] = {"url": resolve_url, "pc_url": resolve_url}
            template_id = self._config.analyse.fix_notify.manual_card_template_id
            if template_id:
                self._feishu.send_card_to_chat(chat_id, build_template_card(template_id, variables))
            else:
                self._feishu.send_text_to_chat(chat_id, build_manual_notify_text(variables))
            log.info("人工处理通知已发送, traceId=%s, handoverType=%s", result.trace_id, handover_type)
        except Exception:  # noqa: BLE001
            log.exception("人工处理通知发送失败, traceId=%s", result.trace_id)

    # ------------------------------------------------------------------ 提取

    def _extract(self, alarm: AlarmLog) -> AlarmAnalyseResult:
        result = AlarmAnalyseResult()
        result.service = alarm.source
        result.system_type = alarm.system_type
        result.level = _normalize_level(alarm.level)
        result.title = alarm.title or f"{result.level} 告警"
        result.trace_id = alarm.trace_id
        result.time = alarm.time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result.raw_message = alarm.message

        # 请求接口：优先报文显式字段，businessKeys.uri 兜底；请求参数：businessKeys.params 兜底
        business = alarm.business_keys or {}
        request_uri = alarm.request_uri or _to_str(business.get("uri"))
        if request_uri:
            result.request_uri = request_uri
            result.url = request_uri
        params = business.get("params")
        if params is not None:
            result.request_param = _to_str(params)

        result.stack_trace = alarm.stack_trace
        result.env = self._config.feishu.env
        result.exception_class = find_exception_class(alarm.message, alarm.stack_trace)
        result.tables = ", ".join(extract_tables(alarm.message, alarm.stack_trace))
        result.core_message = self._build_core_message(alarm, result.exception_class)
        return result

    @staticmethod
    def _build_core_message(alarm: AlarmLog, exception_class: str | None) -> str:
        parts: list[str] = []
        if alarm.message and alarm.message.strip():
            parts.append(alarm.message.strip())
        if alarm.stack_trace:
            appended = 0
            for line in alarm.stack_trace.splitlines():
                if appended >= STACK_FRAMES_IN_CORE:
                    break
                trimmed = line.strip()
                if not trimmed:
                    continue
                parts.append(trimmed)
                appended += 1
        if not parts and exception_class:
            parts.append(exception_class)
        core = "\n".join(parts)
        if len(core) > CORE_MESSAGE_MAX_LENGTH:
            core = core[:CORE_MESSAGE_MAX_LENGTH] + "..."
        return core

    # ------------------------------------------------------------------ 取码

    def _fetch_code_context(self, alarm: AlarmLog) -> str | None:
        """堆栈帧解析业务包类名:行号 → 读源码（网关优先/GitLab 兜底）→ 截取上下文片段。"""
        git = self._config.analyse.git
        gateway_ready = self._gateway.code_read_available
        if not gateway_ready and not (git.enabled and git.api_ready):
            log.info("代码获取不可用(网关读码未启用且 GitLab API 未配置/未启用)，跳过。source=%s", alarm.source)
            return None

        frames = parse_business_frames(alarm.stack_trace, git.max_snippets)
        if not frames:
            log.info("堆栈中未解析到业务代码帧，跳过取码。source=%s", alarm.source)
            return None

        resolved_projects: dict[str, str | None] = {}
        snippets: list[str] = []
        for frame in frames.values():
            # 网关读码不依赖 projectId；API 兜底需要（映射未命中且 search 失败时为 None）
            project_id: str | None = None
            if git.enabled and git.api_ready:
                try:
                    project_id = self._resolve_project_id(alarm.source, frame.class_name, resolved_projects)
                except Exception as exc:  # noqa: BLE001
                    log.warning("GitLab 项目定位失败, class=%s, err=%s", frame.class_name, exc)
            snippet = self._fetch_file_snippet(alarm.source, project_id, frame)
            if snippet:
                snippets.append(snippet)
        return "\n\n".join(snippets) if snippets else None

    def _resolve_project_id(self, source: str, class_name: str,
                            resolved: dict[str, str | None]) -> str | None:
        """定位帧所属 GitLab 项目：projects 显式映射优先，否则类名二级包/服务名 search 兜底。"""
        projects = self._config.analyse.git.projects
        mapped = projects.get(source)
        if mapped:
            return mapped
        # 兜底：com.xizhao.order.xxx.XxxService → order（若该值出现在映射的 value 中说明是项目核心词）
        candidate = source
        parts = class_name.split(".")
        if len(parts) >= 3 and parts[2] in projects.values():
            candidate = parts[2]
        if candidate in resolved:
            return resolved[candidate]
        project_id = self._gitlab.search_project(candidate)
        resolved[candidate] = project_id
        return project_id

    def _fetch_file_snippet(self, service: str, project_id: str | None, frame) -> str | None:
        """逐候选分支（defaultBranch → master → main）读文件，命中即截取出错行上下文。"""
        git = self._config.analyse.git
        file_path = class_name_to_path(frame.class_name)
        for ref in _candidate_branches(git.default_branch):
            content = self._read_source_file(service, project_id, file_path, ref)
            if content:
                return self._build_snippet(file_path, frame.line_number, content, ref)
        return None

    def _read_source_file(self, service: str, project_id: str | None,
                          file_path: str, ref: str) -> str | None:
        """读源文件：网关本地 workspace 优先，GitLab repository files API 兜底。"""
        via_gateway = self._gateway.fetch_file(service, file_path, ref)
        if via_gateway:
            return via_gateway
        git = self._config.analyse.git
        if project_id is None or not git.api_ready:
            return None
        return self._gitlab.read_file(project_id, file_path, ref)

    def _build_snippet(self, file_path: str, error_line: int, content: str, ref: str) -> str:
        """截取错误行前后 contextLines 行并加行号；头注释同时是修复链路的文件定位锚点。"""
        lines = content.splitlines()
        error_line = min(error_line, len(lines)) if lines else error_line
        start = max(1, error_line - self._context_lines)
        end = min(len(lines), error_line + self._context_lines)

        head = f"// 文件: {file_path} (分支 {ref}, 出错行 {error_line})\n"
        body = "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(start, end + 1))
        return head + body + "\n"

    # ------------------------------------------------------------------ 落库

    def _save_analyze_result(self, result: AlarmAnalyseResult) -> bool:
        if self._repository is None:
            return False
        try:
            # 归因非 CODE 时映射初始处置状态（转人工/待人工/无需处理），已完结校验天然拒绝重复修复；
            # 重复告警不映射（处置归属原告警，避免刷屏告警放大转人工统计）
            mapped_status = fix_status_of_problem_type(result.problem_type)
            initial_status = (mapped_status
                              if mapped_status and result.repeat_type != REPEAT_REPEATED else None)
            record_id = self._repository.save(
                result.to_json(), result.trace_id, result.service, result.request_uri,
                problem_type=result.problem_type, fingerprint=result.fingerprint,
                repeat_type=result.repeat_type, root_id=result.root_id,
                fix_status=initial_status, system_type=result.system_type)
            log.info("告警分析结果落库成功, id=%s, traceId=%s, service=%s",
                     record_id, result.trace_id, result.service)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("告警分析结果落库失败(卡片回调修复将不可用)，不影响飞书卡片发送, "
                      "traceId=%s, err=%s", result.trace_id, exc)
            return False


def _normalize_level(level: str | None) -> str:
    if not level or not level.strip():
        return "ERROR"
    normalized = level.strip().upper()
    if normalized == "WARNING":
        return "WARN"
    return normalized if normalized in _LEVELS else "ERROR"


def _to_str(value) -> str | None:
    return None if value is None else str(value)


def _candidate_branches(default_branch: str | None) -> list[str]:
    """候选分支：defaultBranch 打头，补 master/main，保序去重。"""
    branches = [default_branch] if default_branch else []
    for fallback in ("master", "main"):
        if fallback not in branches:
            branches.append(fallback)
    return branches
