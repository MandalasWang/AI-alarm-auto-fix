"""统一异常体系。

设计原则：链路内任何外部依赖失败都不允许裸抛到接口层，
要么被分析/修复主流程捕获降级，要么归一为这里的明确类型便于排障。
"""
from __future__ import annotations


class AlarmError(Exception):
    """模块内所有自定义异常的基类。"""


class ConfigError(AlarmError):
    """配置缺失/非法（启动时快速失败，避免运行期才暴露）。"""


class UpstreamError(AlarmError):
    """外部系统（GitLab / Agent 网关 / 飞书 / Nacos / LLM）调用失败。

    :param detail: 脱敏后的错误摘要（调用方负责不含 token/密钥）。
    """

    def __init__(self, upstream: str, detail: str):
        self.upstream = upstream
        self.detail = detail
        super().__init__(f"[{upstream}] {detail}")


class LlmEmptyContentError(UpstreamError):
    """LLM 返回 content 为空（常见于思考型模型 max_tokens 被思维链耗尽）。

    携带 finish_reason 与响应体摘要，弥补 Java 版排障信息丢失的问题
    （Java 版 LlmChatClient:86 只抛「LLM 返回 content 为空」，无法定位是限流还是截断）。
    """

    def __init__(self, provider_desc: str, finish_reason: str | None, body_snippet: str):
        self.finish_reason = finish_reason
        self.body_snippet = body_snippet
        super().__init__(
            "LLM",
            f"{provider_desc} 返回 content 为空 (finish_reason={finish_reason}, body={body_snippet})",
        )
