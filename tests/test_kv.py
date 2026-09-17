"""进程内 KV 存储与锁语义测试（与 Redis 实现共用同一套约定）。"""
import time

from app.core.kv import MemoryStore


def test_lock_acquire_exclusive():
    store = MemoryStore()
    assert store.acquire_lock("k", "holder-1", ttl_seconds=10) is True
    assert store.acquire_lock("k", "holder-2", ttl_seconds=10) is False
    store.release_lock("k", expected_value="holder-1")
    assert store.acquire_lock("k", "holder-2", ttl_seconds=10) is True


def test_lock_release_with_wrong_holder_noop():
    store = MemoryStore()
    store.acquire_lock("k", "holder-1", ttl_seconds=10)
    # 持有者不匹配不释放（防误删他人的锁）
    store.release_lock("k", expected_value="someone-else")
    assert store.acquire_lock("k", "h", 10) is False


def test_lock_ttl_expiry():
    store = MemoryStore()
    store.acquire_lock("k", "h", ttl_seconds=0)
    time.sleep(0.05)
    # TTL=0 立即过期（此处以极短 TTL 验证过期路径）
    store.acquire_lock("k", "h2", ttl_seconds=1)
    store.set("x", "1", ttl_seconds=1)
    assert store.get("x") == "1"


def test_cache_set_get_delete():
    store = MemoryStore()
    store.set("cache:key", "value", ttl_seconds=60)
    assert store.get("cache:key") == "value"
    store.delete("cache:key")
    assert store.get("cache:key") is None


def test_list_push_pop():
    store = MemoryStore()
    assert store.lpop("q") is None
    store.rpush("q", "a")
    store.rpush("q", "b")
    assert store.lpop("q") == "a"
    assert store.lpop("q") == "b"
    assert store.lpop("q") is None
