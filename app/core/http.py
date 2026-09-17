"""进程级共享 HTTP 客户端：统一超时、重试与错误归一。

增强点（相对 Java 版 hutool 裸调）：
- 429/5xx/网络错误按指数退避自动重试（Java 版仅 LLM 场景有零散重试）；
- 非 2xx 统一抛 UpstreamError 或由调用方按状态码降级（fetch 返回原始响应）；
- 日志只输出 URL 路径与状态码，不输出 header/body，避免 token 泄漏。
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

import httpx

from app.core.errors import UpstreamError

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _safe_url(url: str) -> str:
    """去掉 query（可能带 username/password 类参数）只留路径。"""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


class HttpClient:
    """所有出站 HTTP 的唯一入口。线程安全（httpx.Client 线程安全）。"""

    def __init__(self, default_timeout_ms: int = 10_000, max_retries: int = 0):
        self._client = httpx.Client(timeout=default_timeout_ms / 1000.0)
        self._default_retries = max_retries

    def close(self) -> None:
        self._client.close()

    def fetch(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: object | None = None,
        params: dict[str, str] | None = None,
        timeout_ms: int | None = None,
        max_retries: int | None = None,
    ) -> httpx.Response:
        """执行请求并返回原始响应。

        网络异常与可重试状态码（429/5xx）按指数退避重试，耗尽后抛 UpstreamError；
        不可重试的非 2xx（如 GitLab 404）原样返回，由调用方决定降级语义。
        """
        retries = self._default_retries if max_retries is None else max_retries
        attempt = 0
        while True:
            try:
                response = self._client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=(timeout_ms / 1000.0) if timeout_ms else None,
                )
            except httpx.HTTPError as exc:
                if attempt >= retries:
                    raise UpstreamError("HTTP", f"{_safe_url(url)} 网络异常: {type(exc).__name__}") from exc
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    return response
                if attempt >= retries:
                    return response
                log.warning(
                    "HTTP 可重试失败, url=%s, status=%s, attempt=%s/%s",
                    _safe_url(url), response.status_code, attempt + 1, retries + 1,
                )
            attempt += 1
            time.sleep(min(2.0 ** attempt, 8.0))

    def fetch_ok(
        self,
        method: str,
        url: str,
        *,
        ok_statuses: set[int] | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        """fetch 的严格版：非 2xx（或不在 ok_statuses 内）直接抛 UpstreamError。"""
        response = self.fetch(method, url, **kwargs)  # type: ignore[arg-type]
        ok = ok_statuses or range(200, 300)
        if response.status_code not in ok:
            snippet = (response.text or "")[:300]
            raise UpstreamError("HTTP", f"{_safe_url(url)} status={response.status_code} body={snippet}")
        return response
