"""告警修复代码生成器：按历史分析结果生成可提交的修复文件内容。

输入：告警分析（出错源码片段或异常堆栈）+ 修改建议；输出 FixOutput：
fixed_file（出错类完整路径）、fixed_content（修复后完整文件）、fix_summary（修复说明）、
fixed（是否成功生成修复）、fix_type（code=代码问题 / non_code=数据配置等无需改码）。

出错文件定位：优先分析片段的头注释（`// 文件: ... (分支 prod, 出错行 N)`）；
片段缺失时从堆栈自救解析业务帧，不依赖分析时是否取到片段。
修复源码一律取自修复基线分支（fix_base_branch，默认 prod），保证与 fix 分支基线一致。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.models.alarm import AlarmAnalyseResult
from app.providers.agent_gateway import TASK_TYPE_FIX, AgentGatewayClient
from app.providers.gitlab import GitLabClient
from app.providers.llm import LlmChatClient

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是一名资深 Java 后端工程师，负责按告警修复建议生成修复代码。\n"
    "你会收到：出错类的完整源码（来自修复基线分支）、出错位置片段或异常堆栈（来自环境分支）和修复建议。\n"
    "严格输出以下 JSON（不要输出任何 JSON 之外的内容，包括 markdown 代码块标记）：\n"
    '{"fixed": true|false, "fix_type": "code|non_code|manual", "fixed_file": "src/main/java/完整路径.java", '
    '"fixed_content": "修复后的完整文件内容（Java 转义）", "fix_summary": "一句话修复说明", '
    '"manual_steps": "fix_type=manual 时的分步人工操作建议"}\n'
    "要求：\n"
    "1. fixed_content 是该文件修复后的【完整内容】，不是 diff，不是片段，且必须基于【完整源码】生成\n"
    "2. 只做修复建议明确涉及的最小改动，不重构、不改格式、不动无关代码、不删除既有注释\n"
    "3. fixed=true 时 fix_type 固定填 code；非代码问题：需要人工操作（SQL 变更/配置修改/权限开通等）才能解决的，"
    "fixed 置 false 且 fix_type 置 manual，manual_steps 按「1. … 2. …」给出 2~5 步可执行操作（含 SQL/配置项/申请路径），"
    "fix_summary 一句话概括；确属无需任何处理才用 non_code，fix_summary 说明原因\n"
    "4. 若【出错位置片段】指向的问题在【完整源码】中不存在（行号或代码对不上，说明该问题仅存在于"
    "环境分支而修复基线分支没有），fixed 置 false、fix_type 置 non_code，"
    "fix_summary 说明该问题需在环境分支走正常开发流程修复\n"
    "5. 确属代码问题但无法给出可靠修复时，fixed 置 false 且 fix_type 置 code，fix_summary 说明原因\n"
    "6. 修复说明控制在 100 字内"
)

#: 从片段头注释解析文件路径与环境分支
SNIPPET_HEADER_PATTERN = re.compile(r"//\s*文件:\s*(\S+)\s*\(分支\s*(\S+?)\s*,")


@dataclass
class FixOutput:
    fixed: bool = False
    fixed_file: str | None = None
    fixed_content: str | None = None
    fix_summary: str | None = None
    manual_steps: str | None = None      # fix_type=manual 时的分步人工操作建议（SQL/配置项/申请路径）
    fix_type: str | None = None          # code / non_code / manual
    base_branch: str | None = None       # 修复基线分支（fix 分支从它拉出）
    original_source: str | None = None   # 修复前完整源码（质量闸对照用，不提交不落库）


class FixGenerator:
    def __init__(self, config, llm: LlmChatClient, gateway: AgentGatewayClient, gitlab: GitLabClient,
                 stack_reader):
        """stack_reader: 提供 resolve_business_file_paths(stack_trace) 的对象（AlarmAnalyseService）。"""
        self._config = config
        self._llm = llm
        self._gateway = gateway
        self._gitlab = gitlab
        self._stack_reader = stack_reader

    def generate(self, result: AlarmAnalyseResult) -> FixOutput | None:
        """生成修复输出；LLM 不可用或解析失败返回 None（视为未修复）。"""
        if not (self._gateway.commit_available or self._llm.is_available()):
            log.info("修复生成跳过：llm 未启用(外挂 Agent 与本地大模型均不可用), traceId=%s", result.trace_id)
            return None

        git = self._config.analyse.git
        fix_base_branch = git.fix_base_branch or "prod"

        # 定位候选文件：优先片段头注释（ref 仅用于说明环境分支，修复源码取自 fixBaseBranch）；
        # 片段缺失/头注释异常时降级从堆栈解析业务帧
        candidates: list[str] = []
        env_branch: str | None = None
        if result.code_context:
            match = SNIPPET_HEADER_PATTERN.search(result.code_context)
            if match:
                candidates.append(match.group(1))
                env_branch = match.group(2)
            else:
                log.warning("源码片段头部缺少文件路径注释，降级从堆栈自救定位, traceId=%s", result.trace_id)
        else:
            log.info("分析片段缺失，从堆栈自救定位修复文件, traceId=%s", result.trace_id)
        if not candidates:
            candidates.extend(self._stack_reader.business_file_paths(
                result.stack_trace, git.max_snippets))

        # 逐个候选拉修复基线分支全文件，首个拉到者胜出
        file_path: str | None = None
        full_source: str | None = None
        for candidate in candidates:
            source = self._fetch_full_file(result.service or "", candidate, fix_base_branch)
            if source:
                file_path, full_source = candidate, source
                break
        if not full_source:
            log.warning("定位出错文件失败(堆栈无业务帧或全文件拉取失败)，无法生成修复, "
                        "candidates=%s, fixBase=%s, traceId=%s", candidates, fix_base_branch, result.trace_id)
            return None

        try:
            # 出错位置：有片段给片段；无片段给完整堆栈（行号基于环境分支，仅作定位参考）
            if env_branch:
                error_location = (f"(来自环境分支 {env_branch}，仅作问题定位参考)\n{result.code_context}")
            else:
                error_location = ("(分析时未取到源码片段，以下为完整异常堆栈，仅作问题定位参考)\n"
                                  + (result.stack_trace or ""))
            user_content = (
                f"【文件】{file_path}"
                f"\n【完整源码】(来自修复基线分支 {fix_base_branch})\n{full_source}"
                f"\n\n【出错位置】{error_location}"
                f"\n\n【修改建议】\n{result.proposal or result.analysis or ''}"
            )
            # 外挂 Agent 优先，失败降级本地 LLM
            if self._gateway.commit_available:
                try:
                    response = self._gateway.chat(result.service or "", TASK_TYPE_FIX,
                                                  SYSTEM_PROMPT, user_content)
                except Exception as exc:  # noqa: BLE001
                    log.error("外挂Agent修复生成失败，降级本地LLM, traceId=%s, err=%s",
                              result.trace_id, exc)
                    response = self._llm.chat(SYSTEM_PROMPT, user_content)
            else:
                response = self._llm.chat(SYSTEM_PROMPT, user_content)

            output = self._parse(response)
            if output and not output.fixed_file:
                output.fixed_file = file_path
            if output and output.fixed:
                output.base_branch = fix_base_branch
                output.original_source = full_source
            log.info("修复代码生成完成, traceId=%s, fixed=%s, file=%s",
                     result.trace_id, output.fixed if output else None,
                     output.fixed_file if output else None)
            return output
        except Exception as exc:  # noqa: BLE001 - LLM 调用失败按未修复处理
            log.error("修复代码生成失败, traceId=%s, err=%s", result.trace_id, exc)
            return None

    def _fetch_full_file(self, service: str, file_path: str, ref: str) -> str | None:
        """读完整文件：网关优先，GitLab API 兜底（修复场景 LLM 需要完整文件才能生成修复版）。"""
        git = self._config.analyse.git
        project_id: str | None = None
        if git.api_ready:
            project_id = git.projects.get(service) or self._gitlab.search_project(service)
        return self._gateway.fetch_file(service, file_path, ref) or (
            self._gitlab.read_file(project_id, file_path, ref) if project_id else None
        )

    @staticmethod
    def _parse(response: str) -> FixOutput | None:
        """解析 LLM 的修复 JSON。

        兼容两种键名：本地 LLM 契约 fixed_file/snake_case，外挂 Agent 网关契约 fixedFile/camelCase；
        网关 fix 档位无 fixed 布尔：有修复内容即视为已修复。
        """
        payload = LlmChatClient.parse_json_object(response)
        if payload is None:
            log.warning("修复输出 JSON 解析失败: %s", response[:200])
            return None
        fixed_file = payload.get("fixed_file") or payload.get("fixedFile")
        fixed_content = payload.get("fixed_content") or payload.get("fixedContent")
        fixed = bool(payload.get("fixed", bool(fixed_file and fixed_content)))
        output = FixOutput(
            fixed_file=fixed_file or None,
            fixed_content=fixed_content or None,
            fix_summary=payload.get("fix_summary") or payload.get("fixSummary"),
            # 人工操作步骤（fix_type=manual 时输出，转人工处理通知用）
            manual_steps=payload.get("manual_steps") or payload.get("manualSteps"),
            fix_type=payload.get("fix_type") or payload.get("fixType"),
        )
        if not fixed or not output.fixed_file or not output.fixed_content:
            output.fixed = False
        else:
            output.fixed = True
        return output
