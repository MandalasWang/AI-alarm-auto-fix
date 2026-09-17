"""OpenAI 兼容大模型客户端（chat/completions 协议）。

provider 切换 deepseek / openai / qwen / dashscope / custom，
默认值注册表见 app.config.LLM_PROVIDER_DEFAULTS（新增服务商加一行即可）。
增强点：content 为空时抛 LlmEmptyContentError，携带 finish_reason 与响应体摘要
（Java 版此处只抛「content 为空」，思考型模型被 max_tokens 截断时无法排障）。
"""
from __future__ import annotations

import json
import logging
import time

from app.config import LlmConfig
from app.core import metrics
from app.core.errors import LlmEmptyContentError, UpstreamError
from app.core.http import HttpClient

log = logging.getLogger(__name__)


class LlmChatClient:
    def __init__(self, http: HttpClient, config: LlmConfig):
        self._http = http
        self._config = config

    @property
    def provider_desc(self) -> str:
        return f"llm[{self._config.provider}/{self._config.resolved_model}]"

    def is_available(self) -> bool:
        """开关开启且 apiKey 已配置；baseUrl/model 缺省按 provider 默认补齐。"""
        return self._config.enabled and bool(self._config.api_key)

    def chat(self, system_prompt: str, user_content: str, task: str = "chat") -> str:
        """单轮对话补全，返回模型回复文本；失败抛 UpstreamError 由上层兜底降级。

        task 标注用途（analyse/fix/selfcheck），仅用于指标维度拆分。
        """
        cfg = self._config
        body = {
            "model": cfg.resolved_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
        }
        started = time.monotonic()
        try:
            response = self._http.fetch(
                "POST",
                cfg.endpoint,
                headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
                json_body=body,
                timeout_ms=cfg.timeout_ms,
                max_retries=cfg.max_retries,
            )
            if response.status_code != 200:
                raise UpstreamError("LLM", f"{self.provider_desc} 调用失败 status={response.status_code} "
                                           f"body={(response.text or '')[:500]}")

            payload = response.json()
            choices = payload.get("choices") or []
            if not choices:
                raise UpstreamError("LLM", f"{self.provider_desc} 返回无 choices: "
                                           f"{(response.text or '')[:500]}")
            message = choices[0].get("message") or {}
            content = message.get("content")
            if not content or not str(content).strip():
                raise LlmEmptyContentError(
                    self.provider_desc,
                    payload.get("usage", {}).get("finish_reason") or choices[0].get("finish_reason"),
                    (response.text or "")[:300],
                )
        except Exception:
            metrics.LLM_CALL.labels("local", task, "error").inc()
            raise
        finally:
            metrics.LLM_LATENCY.labels("local", task).observe(time.monotonic() - started)
        usage = payload.get("usage") or {}
        metrics.LLM_TOKENS.labels("prompt").inc(_positive_int(usage.get("prompt_tokens")))
        metrics.LLM_TOKENS.labels("completion").inc(_positive_int(usage.get("completion_tokens")))
        metrics.LLM_CALL.labels("local", task, "ok").inc()
        return str(content).strip()

    @staticmethod
    def strip_code_fence(text: str) -> str:
        """剥离 LLM 输出可能包裹的 markdown 代码块标记。"""
        stripped = text.strip()
        if stripped.startswith("```"):
            start = stripped.find("\n")
            end = stripped.rfind("```")
            if start > 0 and end > start:
                return stripped[start + 1:end].strip()
        return stripped

    @staticmethod
    def parse_json_object(text: str) -> dict | None:
        """解析 LLM 输出中的 JSON 对象（容错：剥代码块、截取首尾大括号）。"""
        cleaned = LlmChatClient.strip_code_fence(text)
        try:
            parsed = json.loads(cleaned)
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            # 兜底：取第一个 { 到最后一个 } 之间的内容
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if 0 <= start < end:
                try:
                    parsed = json.loads(cleaned[start:end + 1])
                    return parsed if isinstance(parsed, dict) else None
                except ValueError:
                    return None
            return None


def _positive_int(value) -> int:
    """token 用量取非负整数；响应未给/给错时按 0 处理（指标不打断业务）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return 0
    return int(value)
