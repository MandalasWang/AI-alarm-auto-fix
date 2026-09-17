"""飞书消息客户端：tenant_access_token 缓存、群文本/卡片消息发送、失败重发。

对应 Java 版 FeishuMessageServiceImpl 中告警模块用到的 chat 场景（phone 系列不在本模块范围）。
失败重发队列与 Java 版同 key（system:feishusendfailed:list）：发送失败入队，
后台线程重试最多 3 次后丢弃（防机器人被移出群等不可恢复失败无限重试）。
"""
from __future__ import annotations

import json
import logging
import threading
import time

from app.core import metrics
from app.core.errors import UpstreamError
from app.core.http import HttpClient
from app.core.kv import KeyValueStore

log = logging.getLogger(__name__)

_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_PATH = "/open-apis/im/v1/messages"

#: token 提前量：过期前 5 分钟刷新
_TOKEN_REFRESH_MARGIN_SECONDS = 300
_EMPTY_RETRY_INTERVAL_SECONDS = 5 * 60
_HAS_DATA_RETRY_INTERVAL_SECONDS = 1
_MAX_RETRY_COUNT = 3


class FeishuClient:
    def __init__(self, http: HttpClient, store: KeyValueStore, retry_list_key: str,
                 base_url: str, app_id: str, app_secret: str):
        self._http = http
        self._store = store
        self._retry_list_key = retry_list_key
        self._base_url = base_url.rstrip("/")
        self._app_id = app_id
        self._app_secret = app_secret
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._token_lock = threading.Lock()
        self._retry_thread: threading.Thread | None = None
        self._retry_stop = threading.Event()

    # ------------------------------------------------------------------ 对外发送

    def send_text_to_chat(self, chat_id: str, text: str) -> None:
        """发送群文本消息；失败入重发队列并抛出（调用方决定是否感知）。"""
        try:
            self._send(chat_id, "text", json.dumps({"text": text}, ensure_ascii=False))
        except UpstreamError:
            self._enqueue_retry(chat_id, "text", text, 0)
            raise

    def send_card_to_chat(self, chat_id: str, card: dict) -> str | None:
        """发送群模板卡片消息，返回 message_id（缓存分析结果用）。失败入队并抛出。"""
        content = json.dumps(card, ensure_ascii=False)
        try:
            data = self._send(chat_id, "interactive", content)
            return data.get("message_id")
        except UpstreamError:
            self._enqueue_retry(chat_id, "card", content, 0)
            raise

    # ------------------------------------------------------------------ 协议实现

    def _send(self, chat_id: str, msg_type: str, content: str) -> dict:
        try:
            response = self._http.fetch(
                "POST",
                self._base_url + _MESSAGE_PATH,
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {self._token()}",
                         "Content-Type": "application/json"},
                json_body={"receive_id": chat_id, "msg_type": msg_type, "content": content},
                timeout_ms=10_000,
            )
            payload = _parse_json(response, "飞书")
            if response.status_code != 200 or payload.get("code") != 0:
                raise UpstreamError("飞书", f"消息发送失败 code={payload.get('code')} "
                                            f"msg={payload.get('msg')} chat_id={chat_id}")
        except Exception:
            metrics.FEISHU_SEND.labels(msg_type, "fail").inc()
            raise
        metrics.FEISHU_SEND.labels(msg_type, "ok").inc()
        return payload.get("data") or {}

    def _token(self) -> str:
        """获取并缓存 tenant_access_token（提前 5 分钟刷新）。

        注意缓存字段名用 _access_token：若叫 _token 会遮蔽本方法（e2e 实测踩坑）。
        """
        with self._token_lock:
            if self._access_token and time.monotonic() < self._access_token_expires_at:
                return self._access_token
            response = self._http.fetch(
                "POST",
                self._base_url + _TOKEN_PATH,
                headers={"Content-Type": "application/json"},
                json_body={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout_ms=10_000,
            )
            payload = _parse_json(response, "飞书")
            if response.status_code != 200 or payload.get("code") != 0:
                raise UpstreamError("飞书", f"获取 tenant_access_token 失败 "
                                            f"code={payload.get('code')} msg={payload.get('msg')}")
            token = payload.get("tenant_access_token")
            if not token:
                raise UpstreamError("飞书", "获取 tenant_access_token 返回为空")
            expire = int(payload.get("expire") or 7200)
            self._access_token = str(token)
            self._access_token_expires_at = time.monotonic() + max(60, expire - _TOKEN_REFRESH_MARGIN_SECONDS)
            return self._access_token

    # ------------------------------------------------------------------ 失败重发队列

    def _enqueue_retry(self, chat_id: str, kind: str, content: str, retry_count: int) -> None:
        """入队原始内容（text 为原文、card 为序列化 JSON），重发时避免二次包装。"""
        record = json.dumps({"chat_id": chat_id, "kind": kind, "content": content, "retry_count": retry_count},
                            ensure_ascii=False)
        try:
            self._store.rpush(self._retry_list_key, record)
        except Exception:  # noqa: BLE001 - 重发队列不可用只记日志
            log.exception("飞书失败消息入队失败")

    def start_retry_loop(self) -> None:
        self._retry_thread = threading.Thread(target=self._retry_loop, name="feishu-retry-message", daemon=True)
        self._retry_thread.start()
        log.info("飞书消息失败重发任务已启动")

    def stop_retry_loop(self) -> None:
        self._retry_stop.set()

    def _retry_loop(self) -> None:
        while not self._retry_stop.is_set():
            try:
                has_data = self._retry_one()
                self._retry_stop.wait(
                    _HAS_DATA_RETRY_INTERVAL_SECONDS if has_data else _EMPTY_RETRY_INTERVAL_SECONDS
                )
            except Exception:  # noqa: BLE001
                log.exception("飞书消息重发线程异常")
                self._retry_stop.wait(_EMPTY_RETRY_INTERVAL_SECONDS)

    def _retry_one(self) -> bool:
        record_text = self._store.lpop(self._retry_list_key)
        if record_text is None:
            return False
        try:
            record = json.loads(record_text)
        except ValueError:
            log.warning("重发飞书消息，数据解析异常，丢弃: %s", record_text)
            return True
        chat_id, kind, content = record.get("chat_id"), record.get("kind"), record.get("content")
        if not chat_id or not content:
            log.warning("重发飞书消息，数据不完整，丢弃: %s", record_text)
            return True
        retry_count = int(record.get("retry_count") or 0)
        if retry_count >= _MAX_RETRY_COUNT:
            log.warning("重发飞书消息，已重试 %s 次仍失败，不再重发, chat_id=%s", retry_count, chat_id)
            return True
        log.info("重发飞书消息(%s/%s), chat_id=%s, kind=%s", retry_count + 1, _MAX_RETRY_COUNT, chat_id, kind)
        try:
            # 直接走原始发送：重发失败在这里以 retry_count+1 重新入队，
            # 不能走公共发送方法（那会以 0 计数入队，导致计数永不增长、无限重试）
            if kind == "card":
                self._send(chat_id, "interactive", content)
            else:
                self._send(chat_id, "text", json.dumps({"text": content}, ensure_ascii=False))
        except (UpstreamError, ValueError):
            self._enqueue_retry(chat_id, str(kind), str(content), retry_count + 1)
        return True


def _parse_json(response, upstream: str) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstreamError(upstream, f"响应非 JSON: status={response.status_code} "
                                      f"body={(response.text or '')[:200]}") from exc
    return payload if isinstance(payload, dict) else {}
