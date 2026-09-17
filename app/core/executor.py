"""有界异步执行池：对齐 Java 版 alarmExecutor 语义（core 2 / max 4 / 队列 100 / CallerRuns）。

分析链路含 LLM 调用（分钟级），接口层只做接收与防重校验后立即返回，
重活提交到本线程池。队列满时退化为调用线程执行，保证任务不丢（极端时变慢不失败）。
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from app.core import metrics

log = logging.getLogger(__name__)


class BoundedExecutor:
    def __init__(self, workers: int = 4, queue_size: int = 100, name: str = "alarm"):
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix=f"{name}-")
        # 信号量 = 在途 + 排队 的总容量上限
        self._permits = threading.BoundedSemaphore(max(1, workers) + max(0, queue_size))
        self._name = name
        self._count_lock = threading.Lock()
        self._inflight = 0

    def _mark_inflight(self, delta: int) -> None:
        with self._count_lock:
            self._inflight += delta
        metrics.EXECUTOR_INFLIGHT.set(self._inflight)

    def submit(self, fn: Callable[..., Any], *args: Any) -> bool:
        """提交任务；池满返回 False（调用方决定降级方式）。异常在后台线程内记录，不向外抛。"""
        if not self._permits.acquire(blocking=False):
            log.warning("执行池[%s]已满（在途+排队达上限），任务被拒绝", self._name)
            metrics.EXECUTOR_REJECT.inc()
            return False
        self._mark_inflight(1)

        def _wrapped() -> None:
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 - 后台任务的最后防线：必须记录，不能带崩线程
                log.exception("后台任务执行失败, pool=%s", self._name)
            finally:
                self._permits.release()
                self._mark_inflight(-1)

        self._pool.submit(_wrapped)
        return True

    def run_or_inline(self, fn: Callable[..., Any], *args: Any) -> None:
        """提交失败时在调用线程执行（对齐 Java CallerRunsPolicy：不丢任务，极端时变慢不失败）。"""
        if not self.submit(fn, *args):
            log.warning("执行池[%s]满，退化为调用线程执行", self._name)
            fn(*args)

    def shutdown(self) -> None:
        """优雅停机：取消排队任务，等待在途分析/修复收尾（避免任务被强杀产生半提交状态）。"""
        self._pool.shutdown(wait=True, cancel_futures=True)
