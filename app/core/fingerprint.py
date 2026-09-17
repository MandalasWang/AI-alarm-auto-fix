"""告警指纹与重复/复发判定、问题归因映射（纯函数，便于单测）。

与 Java 版 util/AlarmFingerprint 对齐（ea7f3e678）：
指纹 = md5(service|requestUri|exceptionClass|首个业务帧)，用于识别同一处错误：
窗口内同指纹且原告警未修复 → REPEATED（重复告警，压卡片）；
原告警已修复后再报 → RECURRED（修复后复发，可手动重修）。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注用，避免运行期依赖
    from app.models.alarm import AlarmAnalyzeRecord

# 堆栈帧：at 全类名.方法名(文件名.java[:行号])——与 extractors.stack 同源，放宽行号可缺省
STACK_FRAME_PATTERN = re.compile(
    r"^\s*at\s+([\w$]+(?:\.[\w$]+)*?)\.([\w$<>]+)\(([\w$]+\.java)(?::\d+)?\)"
)

#: 业务包前缀（与 extractors.stack 的业务帧口径一致）
BUSINESS_PACKAGE_PREFIX = "com.xizhao"

# ---- 问题归因 problem_type ----
PROBLEM_CODE = "CODE"
PROBLEM_DEPENDENCY = "DEPENDENCY"
PROBLEM_MANUAL_REQUIRED = "MANUAL_REQUIRED"
PROBLEM_NOT_A_PROBLEM = "NOT_A_PROBLEM"

# ---- 重复类型 repeat_type ----
REPEAT_NORMAL = "NORMAL"
REPEAT_REPEATED = "REPEATED"
REPEAT_RECURRED = "RECURRED"

# ---- 复发判定/归因映射涉及的 fix_status（与 fixing.service 常量同值）----
FIX_STATUS_FIXED = "FIXED"
FIX_STATUS_NOT_NEEDED = "NOT_NEEDED"
FIX_STATUS_HANDOVER = "HANDOVER"
FIX_STATUS_MANUAL_PENDING = "MANUAL_PENDING"

_PROBLEM_TYPE_LABELS = {
    PROBLEM_CODE: "代码问题",
    PROBLEM_DEPENDENCY: "上下游依赖问题",
    PROBLEM_MANUAL_REQUIRED: "需人工介入",
    PROBLEM_NOT_A_PROBLEM: "无需处理",
}


def fingerprint(service: str | None, request_uri: str | None,
                exception_class: str | None, stack_trace: str | None) -> str | None:
    """计算告警指纹 md5(service|requestUri|exceptionClass|首个业务帧)。

    业务帧取 className.method，不含行号——修复增删代码会平移行号，方法级更稳定。
    四项分量全空返回 None（无法构成指纹，不做重复识别）。
    """
    frame = first_business_frame(stack_trace)
    parts = [service or "", request_uri or "", exception_class or "", frame or ""]
    if not any(part.strip() for part in parts):
        return None
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


def first_business_frame(stack_trace: str | None) -> str | None:
    """堆栈中首个业务帧（com.xizhao.* 类），返回「全类名.方法名」；无业务帧返回 None。"""
    if not stack_trace:
        return None
    for line in stack_trace.splitlines():
        match = STACK_FRAME_PATTERN.match(line)
        if match and match.group(1).startswith(BUSINESS_PACKAGE_PREFIX):
            return f"{match.group(1)}.{match.group(2)}"
    return None


def decide_repeat_type(prior: "AlarmAnalyzeRecord | None", now: datetime) -> str:
    """重复类型判定（纯决策）：无原告警 → NORMAL；

    原告警已修复（FIXED 且修复时间已过）→ RECURRED，说明修复无效；
    其余（未发起/失败/无需处理/转人工/待人工）→ REPEATED。
    """
    if prior is None:
        return REPEAT_NORMAL
    if (prior.fix_status == FIX_STATUS_FIXED
            and prior.fix_time is not None and now >= prior.fix_time):
        return REPEAT_RECURRED
    return REPEAT_REPEATED


def fix_status_of_problem_type(problem_type: str | None) -> str | None:
    """分析阶段归因 → 初始处置状态映射：DEPENDENCY→HANDOVER、MANUAL_REQUIRED→MANUAL_PENDING、

    NOT_A_PROBLEM→NOT_NEEDED；CODE 及未归因返回 None（保持未发起，走自动修复按钮流程）。
    """
    if problem_type == PROBLEM_DEPENDENCY:
        return FIX_STATUS_HANDOVER
    if problem_type == PROBLEM_MANUAL_REQUIRED:
        return FIX_STATUS_MANUAL_PENDING
    if problem_type == PROBLEM_NOT_A_PROBLEM:
        return FIX_STATUS_NOT_NEEDED
    return None


def problem_type_label(problem_type: str | None) -> str:
    """问题归因 → 卡片/日报展示文案，未归因为「-」。"""
    return _PROBLEM_TYPE_LABELS.get(problem_type or "", "-")


__all__ = [
    "PROBLEM_CODE", "PROBLEM_DEPENDENCY", "PROBLEM_MANUAL_REQUIRED", "PROBLEM_NOT_A_PROBLEM",
    "REPEAT_NORMAL", "REPEAT_REPEATED", "REPEAT_RECURRED",
    "FIX_STATUS_FIXED", "FIX_STATUS_NOT_NEEDED", "FIX_STATUS_HANDOVER", "FIX_STATUS_MANUAL_PENDING",
    "fingerprint", "first_business_frame", "decide_repeat_type",
    "fix_status_of_problem_type", "problem_type_label",
]
