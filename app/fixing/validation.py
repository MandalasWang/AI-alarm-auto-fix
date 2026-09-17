"""修复质量闸：LLM 生成的修复文件在提交前做两道校验，任一不过即不提交（转 FAILED 可重试）。

背景：修复链路把 LLM 生成的整文件直接推 fix 分支即标 FIXED，无人保证它能编译；
一次语法错误的「修复」也会群发成功卡片。本模块按性价比落地两道闸：

1. 语法预检（确定性、零成本、零依赖）：package 与文件路径一致、顶层类型名与文件名一致、
   屏蔽字符串/注释后三类括号配对、内容以 } 结束（截断检测）、与原文件不同（no-op 检测）。
2. LLM 自检（llm_self_check 开启时）：第二遍模型对照修改前后全文，只判编译级错误
   （语法、括号引号、缺失 import、引用不存在的成员、明显截断），输出 JSON 结论。
   通道与生成一致：外挂 Agent 网关优先，失败降级本地 LLM。

容错原则：质量闸自身故障（自检调用失败/输出解析不出）按通过处理——闸坏了不该把好修复误杀。
"""
from __future__ import annotations

import logging
import re

from app.config import FixValidationConfig
from app.core import metrics
from app.fixing.generator import FixOutput
from app.providers.agent_gateway import TASK_TYPE_FIX, AgentGatewayClient
from app.providers.llm import LlmChatClient

log = logging.getLogger(__name__)

SELF_CHECK_SYSTEM_PROMPT = (
    "你是一名严格的 Java 代码评审员。你会收到同一 Java 文件的【修改前全文】和【修改后全文】。\n"
    "只判断修改后版本是否存在【会导致编译失败】的问题：语法错误、括号/引号不配对、缺失 import、"
    "引用了不存在的方法或字段、类型不匹配、内容明显被截断。不要评价代码风格、设计与逻辑优劣。\n"
    "严格输出以下 JSON（不要输出任何 JSON 之外的内容，包括 markdown 代码块标记）：\n"
    '{"valid": true|false, "reason": "valid=false 时用 100 字内说明具体哪里编译不过；valid=true 填空串"}'
)

_TYPE_DECLARATION_PATTERN = re.compile(r"\b(?:class|interface|enum)\s+(\w+)")
_RECORD_DECLARATION_PATTERN = re.compile(r"\brecord\s+(\w+)\s*\(")

#: 源码标准根目录（package 推导时剥掉的前缀）
_SOURCE_ROOTS = ("src/main/java/", "src/test/java/")


def _mask_literals(source: str) -> str:
    """屏蔽字符串/字符/文本块/注释，返回只剩代码骨架的文本（供括号统计与类型名匹配）。

    逐字符状态机：处理 // 与 /* */ 注释、"..." 与 '...' 字面量（含 \\ 转义）与 Java 15+ \"\"\" 文本块；
    换行保留（package 用 (?m) 行锚匹配）。不追求完整 Java 词法，目标是常见误判免疫。
    """
    out: list[str] = []
    i, n = 0, len(source)
    NORMAL, LINE, BLOCK, STR, CHAR, TEXT_BLOCK = range(6)
    state = NORMAL
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if state == NORMAL:
            if c == "/" and nxt == "/":
                state = LINE
                i += 2
                out.append("  ")
                continue
            if c == "/" and nxt == "*":
                state = BLOCK
                i += 2
                out.append("  ")
                continue
            if c == '"' and source[i:i + 3] == '"""':
                state = TEXT_BLOCK
                i += 3
                out.append("   ")
                continue
            if c == '"':
                state = STR
                i += 1
                out.append(" ")
                continue
            if c == "'":
                state = CHAR
                i += 1
                out.append(" ")
                continue
            out.append(c)
            i += 1
            continue
        if state == LINE:
            if c == "\n":
                state = NORMAL
                out.append(c)
            else:
                out.append(" ")
            i += 1
            continue
        if state == BLOCK:
            if c == "*" and nxt == "/":
                state = NORMAL
                i += 2
                out.append("  ")
            else:
                out.append(c if c == "\n" else " ")
                i += 1
            continue
        if state == TEXT_BLOCK:
            if c == '"' and source[i:i + 3] == '"""':
                state = NORMAL
                i += 3
                out.append("   ")
            else:
                out.append(c if c == "\n" else " ")
                i += 1
            continue
        # STR / CHAR：转义吞两字符，结束引号回 NORMAL
        if c == "\\":
            out.append("  ")
            i += 2
            continue
        if (state == STR and c == '"') or (state == CHAR and c == "'"):
            state = NORMAL
        out.append(c if c == "\n" else " ")
        i += 1
    return "".join(out)


def _expected_package(file_path: str) -> str | None:
    """从文件路径推导期望 package（剥 src/main/java 前缀、去文件名、/ 换 .）。"""
    path = file_path.replace("\\", "/")
    for prefix in _SOURCE_ROOTS:
        if prefix in path:
            path = path.split(prefix, 1)[1]
            break
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    return directory.replace("/", ".") or None


def _expected_type_name(file_path: str) -> str | None:
    base = file_path.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base or None


def syntax_precheck(fixed_file: str | None, fixed_content: str | None,
                    original_source: str | None) -> str | None:
    """确定性语法预检。返回不通过原因，None=通过。"""
    content = (fixed_content or "").strip()
    if not content:
        return "修复内容为空"
    if not content.endswith("}"):
        return "内容未以 } 结束，疑似输出截断"

    masked = _mask_literals(content)
    for open_ch, close_ch, label in (("{", "}", "花括号"), ("(", ")", "圆括号"), ("[", "]", "方括号")):
        if masked.count(open_ch) != masked.count(close_ch):
            return f"{label}不配对（{masked.count(open_ch)} 对 {masked.count(close_ch)}），疑似语法错误或截断"

    package = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", content)
    if not package:
        return "缺少 package 声明"
    if fixed_file:
        expected = _expected_package(fixed_file)
        if expected and package.group(1) != expected:
            return f"package {package.group(1)} 与文件路径不匹配（期望 {expected}）"
        type_name = (_first_match(_TYPE_DECLARATION_PATTERN, masked)
                     or _first_match(_RECORD_DECLARATION_PATTERN, masked))
        base_name = _expected_type_name(fixed_file)
        if type_name and base_name and base_name != "package-info" and type_name != base_name:
            return f"顶层类型名 {type_name} 与文件名 {base_name}.java 不一致"

    if original_source and content == original_source.strip():
        return "修复内容与原文件完全相同（未做任何修改）"
    return None


def _first_match(pattern: re.Pattern, text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1) if match else None


class FixValidator:
    """两道校验的编排：语法预检（enabled 时必跑）→ LLM 自检（llm_self_check 时跑）。"""

    def __init__(self, config: FixValidationConfig, llm: LlmChatClient,
                 gateway: AgentGatewayClient):
        self._config = config
        self._llm = llm
        self._gateway = gateway

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def validate(self, fix: FixOutput, service: str | None = None) -> str | None:
        """校验待提交的修复输出；返回不通过原因，None=通过（可提交）。"""
        if not self._config.enabled:
            metrics.FIX_VALIDATION.labels("skipped").inc()
            return None

        reason = syntax_precheck(fix.fixed_file, fix.fixed_content, fix.original_source)
        if reason:
            metrics.FIX_VALIDATION.labels("precheck_failed").inc()
            return reason

        if self._config.llm_self_check:
            reason = self._llm_self_check(fix, service)
            if reason:
                metrics.FIX_VALIDATION.labels("selfcheck_failed").inc()
                return reason

        metrics.FIX_VALIDATION.labels("pass").inc()
        return None

    # ------------------------------------------------------------------ LLM 自检

    def _llm_self_check(self, fix: FixOutput, service: str | None) -> str | None:
        user_content = (
            f"【文件】{fix.fixed_file or '-'}\n"
            f"【修改前全文】\n{fix.original_source or '（未提供原文件，仅按修改后版本判断）'}\n\n"
            f"【修改后全文】\n{fix.fixed_content or ''}"
        )
        try:
            response = self._ask(service, user_content)
        except Exception as exc:  # noqa: BLE001 - 质量闸自身故障按通过处理，不误杀
            log.warning("修复自检调用失败，按通过处理, err=%s", exc)
            return None
        payload = LlmChatClient.parse_json_object(response)
        if payload is None:
            log.warning("修复自检输出解析失败，按通过处理: %s", response[:200])
            return None
        if payload.get("valid") is True:
            return None
        if payload.get("valid") is False:
            return str(payload.get("reason") or "LLM 自检判定存在编译级问题")[:200]
        log.warning("修复自检输出 valid 字段缺失，按通过处理: %s", response[:200])
        return None

    def _ask(self, service: str | None, user_content: str) -> str:
        """自检通道：与生成一致（网关优先），本地 LLM 未启用且网关不可用时报错由上层按通过处理。"""
        if self._gateway.commit_available:
            try:
                return self._gateway.chat(service or "", TASK_TYPE_FIX,
                                          SELF_CHECK_SYSTEM_PROMPT, user_content)
            except Exception as exc:  # noqa: BLE001 - 降级本地 LLM
                log.warning("修复自检网关通道失败，降级本地 LLM, err=%s", exc)
        if not self._llm.is_available():
            raise RuntimeError("自检通道不可用（网关未启用且本地 LLM 未启用）")
        return self._llm.chat(SELF_CHECK_SYSTEM_PROMPT, user_content, task="selfcheck")


__all__ = ["FixValidator", "syntax_precheck", "_mask_literals"]
