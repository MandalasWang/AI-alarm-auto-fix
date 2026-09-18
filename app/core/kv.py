"""KV 存储抽象：分布式锁 + 缓存 + 失败重发队列。

灵活性设计：Redis 未配置时自动退化为进程内实现（本地开发/单实例部署零依赖），
接口不变，生产多实例只需配 storage.redis_url。
默认 key 与 Java 版保持一致，可与现有 Redis 数据互通。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Protocol

log = logging.getLogger(__name__)


class KeyValueStore(Protocol):
    def acquire_lock(self, key: str, value: str, ttl_seconds: int) -> bool: ...
    def release_lock(self, key: str, expected_value: str | None = None) -> None: ...
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...
    def delete(self, key: str) -> None: ...
    def incr(self, key: str, ttl_seconds: int) -> int: ...
    def rpush(self, list_key: str, value: str) -> None: ...
    def lpop(self, list_key: str) -> str | None: ...
    def ping(self) -> bool: ...


class RedisStore:
    """Redis 实现。锁释放用 Lua 比较删除，避免误删他人的锁（Java 版为直接 DEL，此处更严谨）。"""

    # KEYS[1]=锁 key, ARGV[1]=期望值：值匹配才删除
    _RELEASE_LUA = (
        'if redis.call("get", KEYS[1]) == ARGV[1] then '
        'return redis.call("del", KEYS[1]) else return 0 end'
    )

    def __init__(self, url: str):
        import redis  # 延迟导入：未配 Redis 的部署不强制安装可用

        self._redis = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5)

    def acquire_lock(self, key: str, value: str, ttl_seconds: int) -> bool:
        return bool(self._redis.set(key, value, nx=True, ex=ttl_seconds))

    def release_lock(self, key: str, expected_value: str | None = None) -> None:
        try:
            if expected_value is None:
                self._redis.delete(key)
            else:
                self._redis.eval(self._RELEASE_LUA, 1, key, expected_value)
        except Exception as exc:  # Redis 抖动不阻断主流程（锁靠 TTL 兜底过期）
            log.warning("释放锁失败(等待 TTL 过期), key=%s, err=%s", key, exc)

    def get(self, key: str) -> str | None:
        return self._redis.get(key)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._redis.set(key, value, ex=ttl_seconds)

    def delete(self, key: str) -> None:
        self._redis.delete(key)

    def incr(self, key: str, ttl_seconds: int) -> int:
        """原子自增（计数器场景，如 LLM 预算）：key 不存在时从 0 起并设置 TTL。"""
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl_seconds)
        return int(pipe.execute()[0])

    def rpush(self, list_key: str, value: str) -> None:
        self._redis.rpush(list_key, value)

    def lpop(self, list_key: str) -> str | None:
        return self._redis.lpop(list_key)

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except Exception:  # noqa: BLE001 - 探活只报告不抛
            return False


class MemoryStore:
    """进程内实现：单实例部署 / 本地开发用。TTL 靠惰性检查。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[str, float]] = {}
        self._lists: dict[str, deque[str]] = {}

    def _alive(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at and expires_at < time.monotonic():
            del self._data[key]
            return None
        return value

    def acquire_lock(self, key: str, value: str, ttl_seconds: int) -> bool:
        with self._lock:
            if self._alive(key) is not None:
                return False
            self._data[key] = (value, time.monotonic() + ttl_seconds)
            return True

    def release_lock(self, key: str, expected_value: str | None = None) -> None:
        with self._lock:
            current = self._alive(key)
            if expected_value is None or current == expected_value:
                self._data.pop(key, None)

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._alive(key)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        with self._lock:
            self._data[key] = (value, time.monotonic() + ttl_seconds)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def incr(self, key: str, ttl_seconds: int) -> int:
        """进程内原子自增：新键（或已过期）从 1 起并刷新 TTL，已有键只加值不动 TTL。"""
        with self._lock:
            current = self._alive(key)
            if current is None:
                self._data[key] = ("1", time.monotonic() + ttl_seconds)
                return 1
            value = int(current) + 1
            self._data[key] = (str(value), self._data[key][1])
            return value

    def rpush(self, list_key: str, value: str) -> None:
        with self._lock:
            self._lists.setdefault(list_key, deque()).append(value)

    def lpop(self, list_key: str) -> str | None:
        with self._lock:
            queue = self._lists.get(list_key)
            return queue.popleft() if queue else None

    def ping(self) -> bool:
        return True


def create_store(redis_url: str | None) -> KeyValueStore:
    """工厂：redis_url 非空走 Redis，否则进程内实现（打警告提醒多实例部署需配 Redis）。"""
    if redis_url:
        store: KeyValueStore = RedisStore(redis_url)
        log.info("KV 存储使用 Redis")
        return store
    log.warning("未配置 storage.redis_url，锁与缓存退化为进程内实现（仅单实例部署可用）")
    return MemoryStore()
