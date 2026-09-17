"""告警分析报告生成器：日志 + 代码 + 数据三方交叉，产出问题定位/根因/修改建议/问题归因。

调用优先级：外挂 Agent → 本地大模型 → 规则兜底，保证卡片始终有分析内容。
报告按【问题定位】【报错分析】【修改建议】【问题归因】分段：前两段 → analysis（卡片 analyze 变量），
第三段 → proposal（卡片 suggest 变量），归因段 → problem_type（归因映射处置状态）。
"""
from __future__ import annotations

import logging
import re

from app.core.fingerprint import (
    PROBLEM_CODE, PROBLEM_DEPENDENCY, PROBLEM_MANUAL_REQUIRED,
)
from app.models.alarm import AlarmAnalyseResult
from app.providers.agent_gateway import TASK_TYPE_ANALYSE, AgentGatewayClient
from app.providers.llm import LlmChatClient

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是一名资深 Java 后端排障专家。你会收到一条服务告警的完整上下文（告警消息、异常堆栈、"
    "出错的源码片段、相关数据库表信息）。请基于这些事实交叉分析，不要臆造上下文中没有的信息。\n"
    "输出使用简体中文，严格按以下结构：\n"
    "【问题定位】出问题的服务、接口、类#方法:行号、涉及的表（依据堆栈和代码）\n"
    "【报错分析】错误是什么、为什么会发生（结合代码片段和库表数据交叉验证；若数据不支持某个猜测请明确说明）\n"
    "【修改建议】给出具体可执行的修改方案，含代码位置和改法（必要时给出关键代码）；"
    "如涉及数据问题给出核查 SQL\n"
    "【问题归因】从以下选项单选输出一个英文枚举值："
    "CODE-本服务代码问题 / DEPENDENCY-上下游依赖问题（第三方服务、下游接口、注册中心、消息队列等异常，"
    "本服务无法通过改码解决） / MANUAL_REQUIRED-需人工介入（SQL 变更、配置修改、权限开通等，自动改码解决不了） / "
    "NOT_A_PROBLEM-确认无需任何处理\n"
    "整体控制在 700 字以内，直击要点。"
)

#: 喂给大模型的上下文总长上限（字符）
USER_CONTENT_MAX_LENGTH = 24_000

#: 归因枚举匹配：长 token 在前，在【问题归因】段内取最先出现者
_PROBLEM_TYPE_PATTERN = re.compile(r"NOT_A_PROBLEM|MANUAL_REQUIRED|DEPENDENCY|CODE")

#: 依赖类异常特征：Feign/超时/连接/域名解析类，规则归因兜底用
_DEPENDENCY_EXCEPTION_PATTERN = re.compile(
    r"(?i)feign|connect\s*refused|connect\s*timed\s*out|sockettimeout|connectexception"
    r"|timeoutexception|read\s*timeout|timed\s*out|unknownhost|restclientexception")


class ReportGenerator:
    def __init__(self, llm: LlmChatClient, gateway: AgentGatewayClient):
        self._llm = llm
        self._gateway = gateway

    def generate(self, result: AlarmAnalyseResult) -> str:
        """生成分析报告，永不为空（两层降级 + 规则兜底）。"""
        user_content = self._build_user_content(result)

        # 1. 外挂 Agent 优先
        if self._gateway.code_read_available or self._gateway.commit_available:
            try:
                report = self._gateway.chat(
                    result.service or "", TASK_TYPE_ANALYSE, SYSTEM_PROMPT, user_content)
                log.info("外挂Agent告警分析完成, service=%s, traceId=%s, length=%s",
                         result.service, result.trace_id, len(report))
                return report
            except Exception as exc:  # noqa: BLE001 - 降级本地 LLM
                log.error("外挂Agent告警分析失败，降级本地大模型, service=%s, traceId=%s, err=%s",
                          result.service, result.trace_id, exc)

        # 2. 本地大模型
        if self._llm.is_available():
            try:
                report = self._llm.chat(SYSTEM_PROMPT, user_content)
                log.info("大模型告警分析完成, service=%s, traceId=%s, length=%s",
                         result.service, result.trace_id, len(report))
                return report
            except Exception as exc:  # noqa: BLE001 - 降级规则报告
                log.error("大模型告警分析失败，使用规则兜底报告, service=%s, traceId=%s, err=%s",
                          result.service, result.trace_id, exc)

        return self._fallback_report(result)

    def generate_and_split(self, result: AlarmAnalyseResult) -> None:
        """生成报告并按段拆分填充 analysis / proposal / problem_type（归因段缺失走规则兜底）。"""
        report = self.generate(result)
        result.analysis = self._extract_section(report, "问题定位", "报错分析")
        result.proposal = self._extract_section(report, "修改建议", None)
        result.problem_type = parse_problem_type(report) or rule_problem_type(result)

    @staticmethod
    def _extract_section(report: str, start_marker: str, end_marker: str | None) -> str | None:
        """截取【start_marker】之后到【end_marker】之前（end 为空取到结尾）；找不到标记返回全文。"""
        if not report:
            return report
        start = report.find(f"【{start_marker}】")
        if start < 0:
            return report.strip()
        start += len(start_marker) + 2
        end = report.find(f"【{end_marker}】", start) if end_marker else -1
        if end < 0:
            end = len(report)
        section = report[start:end].strip()
        return section or None

    @staticmethod
    def _build_user_content(result: AlarmAnalyseResult) -> str:
        """拼装喂给大模型的告警上下文：日志 → 代码 → 数据。"""
        parts: list[str] = ["【告警消息】"]
        for title, content in (
            ("核心内容", result.core_message),
            ("异常类", result.exception_class),
            ("完整堆栈", result.stack_trace),
            ("涉及表", result.tables),
            ("出错代码片段(GitLab)", result.code_context),
            ("数据库上下文", result.db_context),
            ("请求参数", result.request_param),
        ):
            if content:
                parts.append(f"\n【{title}】\n{content.strip()}")
        text = "\n".join(parts)
        if len(text) > USER_CONTENT_MAX_LENGTH:
            text = text[:USER_CONTENT_MAX_LENGTH] + "\n...(内容过长已截断)"
        return text

    @staticmethod
    def _fallback_report(result: AlarmAnalyseResult) -> str:
        """规则兜底报告：大模型不可用时仍给出结构化的基础分析。"""
        lines = [
            "【问题定位】",
            f"- 服务: {result.service or '-'}",
            f"- 接口: {result.request_uri or '-'}",
            f"- 异常: {result.exception_class or '-'}",
        ]
        if result.tables:
            lines.append(f"- 涉及表: {result.tables}")
        lines += [
            "",
            "【报错分析】",
            result.core_message or "无核心消息",
            "",
            "【修改建议】",
            "大模型分析未启用或调用失败，请依据以上堆栈与代码片段人工排查；",
            "或配置 analyse.llm / analyse.external 后重试。",
            "",
            "【问题归因】",
            rule_problem_type(result),
        ]
        return "\n".join(lines)


def parse_problem_type(report: str | None) -> str | None:
    """从报告【问题归因】段提取归因枚举，非法/缺失返回 None（调用方需走规则兜底）。纯函数，便于单测。"""
    if not report or "【问题归因】" not in report:
        return None
    section = ReportGenerator._extract_section(report, "问题归因", None)
    if not section:
        return None
    match = _PROBLEM_TYPE_PATTERN.search(section)
    return match.group() if match else None


def rule_problem_type(result: AlarmAnalyseResult) -> str:
    """规则归因兜底：堆栈/异常类命中依赖类特征（Feign、超时、连接失败等）→ DEPENDENCY；

    堆栈与异常类全空（多为数据/配置类问题，自动改码无从下手）→ MANUAL_REQUIRED；否则 CODE。纯函数，便于单测。
    """
    combined = f"{result.exception_class or ''}\n{result.stack_trace or ''}"
    if _DEPENDENCY_EXCEPTION_PATTERN.search(combined):
        return PROBLEM_DEPENDENCY
    if not result.exception_class and not result.stack_trace:
        return PROBLEM_MANUAL_REQUIRED
    return PROBLEM_CODE


__all__ = ["ReportGenerator", "SYSTEM_PROMPT", "parse_problem_type", "rule_problem_type"]
