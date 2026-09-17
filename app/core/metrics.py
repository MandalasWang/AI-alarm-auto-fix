"""Prometheus 运营指标：告警接收/分析、修复结果、质量闸、MR、飞书发送、LLM 调用、执行池积压。

设计要点：
- prometheus_client 未安装时全量退化为 no-op 桩，服务照常启动（/metrics 返回提示文本）；
- label 只允许有限枚举值（outcome/status/kind 等），traceId/service 等高基数值禁止入 label；
- 打点失败绝不阻断业务链路（Counter 自身不抛业务异常，仅依赖缺失场景走桩）。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
    _AVAILABLE = True
except ImportError:  # 依赖缺失不阻断启动（requirements.txt 已声明，正常部署不会走到）
    _AVAILABLE = False


class _NoopMetric:
    """prometheus_client 缺席时的 no-op 替身：labels() 返回自身，读写均为空操作。"""

    def labels(self, *args: object, **kwargs: object) -> "_NoopMetric":
        return self

    def inc(self, amount: float = 1.0) -> None: ...
    def dec(self, amount: float = 1.0) -> None: ...
    def set(self, value: float) -> None: ...
    def observe(self, value: float) -> None: ...

    def time(self) -> "_NoopTimer":
        return _NoopTimer()


class _NoopTimer:
    def __enter__(self) -> "_NoopTimer":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _counter(name: str, doc: str, labels: list[str] | None = None):
    if not _AVAILABLE:
        return _NoopMetric()
    try:
        return Counter(name, doc, labels or [])
    except ValueError:  # 重复注册（模块热重载才会发生）：旧采集器仍在注册表，本引用退化为 no-op
        log.debug("指标 %s 重复注册，使用 no-op", name)
        return _NoopMetric()


def _gauge(name: str, doc: str):
    if not _AVAILABLE:
        return _NoopMetric()
    try:
        return Gauge(name, doc)
    except ValueError:
        log.debug("指标 %s 重复注册，使用 no-op", name)
        return _NoopMetric()


def _histogram(name: str, doc: str, labels: list[str], buckets: tuple[float, ...]):
    if not _AVAILABLE:
        return _NoopMetric()
    try:
        return Histogram(name, doc, labels, buckets=buckets)
    except ValueError:
        log.debug("指标 %s 重复注册，使用 no-op", name)
        return _NoopMetric()


# ---------------------------------------------------------------- 指标清单

#: 接收告警数（source=告警服务名，基数有限且可枚举）
ALARM_RECEIVED = _counter("alarm_received_total", "接收到的告警数", ["source"])

#: 告警分析终态：ok=完成分析推卡片 / repeated=窗口内重复压卡片 / error=链路异常
ALARM_ANALYSE = _counter("alarm_analyse_total", "告警分析终态数", ["outcome"])

#: 修复/忽略回调受理：accepted=受理异步执行 / rejected=防重、并发、缺数据等拒绝
FIX_ACCEPT = _counter("alarm_fix_accept_total", "修复与忽略回调受理数", ["action", "outcome"])

#: 修复执行终态（落库口径）：FIXED/FAILED/NOT_NEEDED/MANUAL_PENDING
FIX_RESULT = _counter("alarm_fix_result_total", "修复执行终态数", ["fix_status"])

#: 修复质量闸：pass=校验通过 / precheck_failed=语法预检不通过 / selfcheck_failed=LLM 自检不通过 / skipped=未启用
FIX_VALIDATION = _counter("alarm_fix_validation_total", "修复质量闸结果数", ["outcome"])

#: MR 创建：created / exists=已存在复用 / failed / skipped=未启用或项目定位失败
MR_RESULT = _counter("alarm_mr_total", "修复 MR 创建结果数", ["outcome"])

#: 飞书发送（含失败重发），kind=text|interactive（卡片），outcome=ok|fail
FEISHU_SEND = _counter("feishu_send_total", "飞书消息发送数", ["kind", "outcome"])

#: LLM/外挂网关任务耗时（秒）。channel=local|gateway，task=analyse|fix|selfcheck
LLM_LATENCY = _histogram("llm_call_seconds", "LLM 与网关任务耗时", ["channel", "task"],
                         buckets=(1, 5, 15, 30, 60, 120, 300, 600, 900))

#: LLM/网关任务调用结果，outcome=ok|error
LLM_CALL = _counter("llm_call_total", "LLM 与网关任务调用数", ["channel", "task", "outcome"])

#: 本地 LLM token 消耗（网关任务网关侧计费，取不到 usage 不打点），kind=prompt|completion
LLM_TOKENS = _counter("llm_tokens_total", "LLM token 消耗量", ["kind"])

#: 执行池在途+排队任务数（积压深度）
EXECUTOR_INFLIGHT = _gauge("alarm_executor_inflight", "执行池在途与排队任务总数")

#: 执行池满被拒绝的任务数（run_or_inline 退化前的那次拒绝）
EXECUTOR_REJECT = _counter("alarm_executor_reject_total", "执行池满拒绝任务数")


def render() -> tuple[str, bytes]:
    """渲染 Prometheus 文本协议输出；依赖缺失时返回占位说明。"""
    if not _AVAILABLE:
        return "text/plain; charset=utf-8", b"# prometheus_client not installed\n"
    return CONTENT_TYPE_LATEST, generate_latest()


def available() -> bool:
    return _AVAILABLE


__all__ = [
    "ALARM_RECEIVED", "ALARM_ANALYSE", "FIX_ACCEPT", "FIX_RESULT", "FIX_VALIDATION",
    "MR_RESULT", "FEISHU_SEND", "LLM_LATENCY", "LLM_CALL", "LLM_TOKENS",
    "EXECUTOR_INFLIGHT", "EXECUTOR_REJECT", "render", "available",
]
