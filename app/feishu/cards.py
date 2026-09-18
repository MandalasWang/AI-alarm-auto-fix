"""飞书卡片构建器。

产出模板卡片报文：{type: template, data: {template_id, template_variable: {...}}}。
飞书卡片消息整体限制 30KB（UTF-8 字节）：各变量先按字节预算截断，预算总和最坏约 22KB；
序列化后仍超 28KB 目标时按「堆栈 → param → suggest → analyze」优先级削减。
截断仅作用于卡片展示，完整内容仍落库与缓存。
"""
from __future__ import annotations

import json
import logging

from app.core.fingerprint import REPEAT_RECURRED, problem_type_label
from app.core.text import truncate_chars, truncate_utf8, utf8_len
from app.models.alarm import AlarmAnalyseResult

log = logging.getLogger(__name__)

_PLACEHOLDER = "-"
_STACK_MAX_CHARS = 500
#: 短字段字节预算（title/level/service/interface/trace_id/exception_type/tables/env/time）
_SHORT_FIELD_MAX_BYTES = 512
#: 请求参数字节预算
_PARAM_MAX_BYTES = 2 * 1024
#: LLM 报告段落（analyze/suggest）字节预算：两段共 12KB，加短字段与结构开销仍在 28KB 目标内
_REPORT_SECTION_MAX_BYTES = 6 * 1024
#: 发送内容目标上限：30KB 硬限制留 2KB 余量（含 JSON 结构与模板包装开销）
_CARD_TARGET_MAX_BYTES = 28 * 1024
#: 复发卡片「原修复摘要」字节预算（commit/MR 链接足够，不与主内容争预算）
_RECURRED_SUMMARY_MAX_BYTES = 1024
#: 削减时字段保留的字节下限，防止预算无限减半
_TRIM_FLOOR_BYTES = 256
_STACK_OMITTED_TEXT = "(堆栈过长已省略，完整堆栈见告警日志)"


def build_template_card(template_id: str, variables: dict) -> dict:
    return {
        "type": "template",
        "data": {"template_id": template_id, "template_variable": variables},
    }


def _card_text(value: str | None, max_bytes: int, default: str | None) -> str:
    truncated = truncate_utf8(value, max_bytes) or ""
    return truncated if truncated.strip() else (default or "")


def build_alarm_card(result: AlarmAnalyseResult, template_id: str, jump_url: str,
                     ignore_jump_url: str = "", resolve_jump_url: str = "") -> dict:
    """构建告警模板卡片。

    url/ignor_url/resolve_url 变量为分端差异化链接对象（模板 Url 类型），传字符串会报 200732
    类型不一致；非 http 开头的完整链接会被飞书拒绝（200737），jump_url 未配置时保持空串交由
    模板兜底；ignore/resolve_jump_url 未配置时不输出对应变量（模板可不加按钮），旧模板未引用
    的变量自动忽略。
    """
    variables: dict = {
        "title": _card_text(result.title, _SHORT_FIELD_MAX_BYTES, "系统告警"),
        "level": _card_text(result.level, _SHORT_FIELD_MAX_BYTES, "ERROR"),
        "service": _card_text(result.service, _SHORT_FIELD_MAX_BYTES, _PLACEHOLDER),
        "interface": _card_text(result.request_uri, _SHORT_FIELD_MAX_BYTES, _PLACEHOLDER),
        "trace_id": _card_text(result.trace_id, _SHORT_FIELD_MAX_BYTES, _PLACEHOLDER),
        "param": _card_text(result.request_param, _PARAM_MAX_BYTES, _PLACEHOLDER),
        "exception_type": _card_text(result.exception_class, _SHORT_FIELD_MAX_BYTES, _PLACEHOLDER),
        "tables": _card_text(result.tables, _SHORT_FIELD_MAX_BYTES, _PLACEHOLDER),
        "track": _card_text(truncate_chars(result.stack_trace, _STACK_MAX_CHARS), 10**9, _PLACEHOLDER),
        # analyze=报错分析（问题定位+报错分析），suggest=修改建议；旧数据 analysis 为空时回退整份报告
        "suggest": _card_text(result.proposal, _REPORT_SECTION_MAX_BYTES, _PLACEHOLDER),
        "env": _card_text(result.env, _SHORT_FIELD_MAX_BYTES, None),
        "time": _card_text(result.time, _SHORT_FIELD_MAX_BYTES, _PLACEHOLDER),
    }
    suggest = variables["suggest"]
    variables["analyze"] = _card_text(result.analysis, _REPORT_SECTION_MAX_BYTES, suggest)

    fix_url = (jump_url or "") + (result.trace_id or "")
    variables["url"] = {"url": fix_url, "pc_url": fix_url}

    # 忽略按钮链接：Nacos 配置 ignore_jump_url 后输出 ignor_url；未配置不输出该变量
    if ignore_jump_url:
        ignore_url = ignore_jump_url + (result.trace_id or "")
        variables["ignor_url"] = {"url": ignore_url, "pc_url": ignore_url}

    # 结案按钮链接：配置 resolve_jump_url 后输出 resolve_url（人工处理完成点「已解决」回填终态）
    if resolve_jump_url:
        resolve_url = resolve_jump_url + (result.trace_id or "")
        variables["resolve_url"] = {"url": resolve_url, "pc_url": resolve_url}

    # 归因/复发标注：repeat_label 仅复发时输出（重复告警不推卡片）
    variables["problem_type"] = _card_text(problem_type_label(result.problem_type),
                                           _SHORT_FIELD_MAX_BYTES, _PLACEHOLDER)
    variables["root_id"] = "" if result.root_id is None else str(result.root_id)
    if result.repeat_type == REPEAT_RECURRED:
        variables["repeat_label"] = "⚠️ 修复后复发"
        # 原修复的处理摘要（分支/commit/MR 链接），便于直接核对原修复为何未生效
        if result.recurred_fix_summary:
            variables["recurred_fix_summary"] = _card_text(
                result.recurred_fix_summary, _RECURRED_SUMMARY_MAX_BYTES, _PLACEHOLDER)

    card = build_template_card(template_id, variables)
    _enforce_card_budget(card, variables, result)
    return card


def _enforce_card_budget(card: dict, variables: dict, result: AlarmAnalyseResult) -> None:
    """整体超 28KB 目标时的削减（30KB 为飞书硬限制）：
    ① 堆栈最先省略；② 仍超限则按 param → suggest → analyze 顺序逐段减半。"""
    if _card_bytes(card) <= _CARD_TARGET_MAX_BYTES:
        return
    log.warning("告警卡片超过 %sB 目标，开始按优先级削减, traceId=%s",
                _CARD_TARGET_MAX_BYTES, result.trace_id)
    variables["track"] = _STACK_OMITTED_TEXT

    param_budget = _PARAM_MAX_BYTES
    suggest_budget = _REPORT_SECTION_MAX_BYTES
    analyze_budget = _REPORT_SECTION_MAX_BYTES
    while (_card_bytes(card) > _CARD_TARGET_MAX_BYTES
           and max(param_budget, suggest_budget, analyze_budget) > _TRIM_FLOOR_BYTES):
        if param_budget > _TRIM_FLOOR_BYTES:
            param_budget = max(_TRIM_FLOOR_BYTES, param_budget // 2)
            variables["param"] = _card_text(result.request_param, param_budget, _PLACEHOLDER)
        elif suggest_budget > _TRIM_FLOOR_BYTES:
            suggest_budget = max(_TRIM_FLOOR_BYTES, suggest_budget // 2)
            variables["suggest"] = _card_text(result.proposal, suggest_budget, _PLACEHOLDER)
        else:
            analyze_budget = max(_TRIM_FLOOR_BYTES, analyze_budget // 2)
            variables["analyze"] = _card_text(result.analysis, analyze_budget,
                                              str(variables.get("suggest") or _PLACEHOLDER))


def _card_bytes(card: dict) -> int:
    return utf8_len(json.dumps(card, ensure_ascii=False))


def build_fix_notify_variables(fix_result: str, service: str, interface: str, trace_id: str,
                               fix_time: str, fix_branch: str, fix_summary: str) -> dict:
    """修复结果通知卡片变量（模板 AAqPVdOld0o00，7 变量缺一不可）。"""
    return {
        "fix_result": fix_result or _PLACEHOLDER,
        "service": service or _PLACEHOLDER,
        "interface": interface or _PLACEHOLDER,
        "trace_id": trace_id or _PLACEHOLDER,
        "fix_time": fix_time or _PLACEHOLDER,
        "fix_branch": fix_branch or _PLACEHOLDER,
        # 内嵌 LLM 生成的修复说明可能超长，按 6KB 卡片上限截断
        "fix_summary": truncate_utf8(fix_summary or _PLACEHOLDER, 6 * 1024) or _PLACEHOLDER,
    }


def build_fix_notify_text(variables: dict) -> str:
    """修复通知模板未配置时的文本消息兜底，字段与卡片变量一致。"""
    return (
        f"告警修复结果：{variables['fix_result']}\n"
        f"服务名称：{variables['service']}\n"
        f"请求接口：{variables['interface']}\n"
        f"TRACE_ID：{variables['trace_id']}\n"
        f"修复时间：{variables['fix_time']}\n"
        f"修复分支：{variables['fix_branch']}\n"
        f"修复说明：{variables['fix_summary']}"
    )


def build_manual_notify_variables(handover_type: str, service: str, interface: str,
                                  trace_id: str, handover_time: str, steps: str) -> dict:
    """人工处理/转人工通知卡片变量（模板 fix-notify.manual-card-template-id，未配则文本兜底）。"""
    return {
        "handover_type": handover_type or _PLACEHOLDER,
        "service": service or _PLACEHOLDER,
        "interface": interface or _PLACEHOLDER,
        "trace_id": trace_id or _PLACEHOLDER,
        "handover_time": handover_time or _PLACEHOLDER,
        # 建议步骤（SQL/配置项/排查思路）可能超长，按 6KB 卡片上限截断
        "steps": truncate_utf8(steps or _PLACEHOLDER, 6 * 1024) or _PLACEHOLDER,
    }


def build_manual_notify_text(variables: dict) -> str:
    """人工处理通知模板未配置时的文本消息兜底，字段与卡片变量一致。"""
    return (
        f"告警人工处理通知：{variables['handover_type']}\n"
        f"服务名称：{variables['service']}\n"
        f"请求接口：{variables['interface']}\n"
        f"TRACE_ID：{variables['trace_id']}\n"
        f"通知时间：{variables['handover_time']}\n"
        f"建议步骤：{variables['steps']}"
    )
