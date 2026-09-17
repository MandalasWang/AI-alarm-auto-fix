"""Java 堆栈解析：业务帧提取、类名→文件路径、异常类识别。

被两条链路共用：分析取码（定位出错文件行号）与修复自救（片段缺失时从堆栈重定位文件）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 堆栈帧：at 全类名.方法名(文件名:行号)。类名部分非贪婪，依文件名锚定真实类边界，
# 避免把「类名.方法名」整体吞进类名。
STACK_FRAME_PATTERN = re.compile(
    r"^\s*at\s+([\w$]+(?:\.[\w$]+)*?)\.([\w$<>]+)\(([\w$]+\.java):(\d+)\)"
)

# 匹配异常/错误类名，如 java.lang.NullPointerException
EXCEPTION_PATTERN = re.compile(r"([\w.$]+(?:Exception|Error))(?::|\s|$)")

#: 业务包前缀，优先取这些帧而非 spring/jdk 框架帧
BUSINESS_PACKAGE_PREFIX = "com.xizhao"


@dataclass(frozen=True)
class StackFrame:
    class_name: str
    method: str
    file_name: str
    line_number: int


def find_exception_class(message: str | None, stack_trace: str | None) -> str | None:
    """从堆栈（缺省退化为 message）中提取第一个异常类名。"""
    text = stack_trace or message or ""
    match = EXCEPTION_PATTERN.search(text)
    return match.group(1) if match else None


def parse_business_frames(
    stack_trace: str | None, max_frames: int, business_prefix: str = BUSINESS_PACKAGE_PREFIX
) -> dict[str, StackFrame]:
    """解析堆栈中的业务帧，保序去重（同类多帧只取第一处），最多 max_frames 个。

    帧校验：Java 堆栈里文件名即类简名，类名必须以「文件名去 .java」结尾
    （内部类 Foo$Bar.java 匹配 Foo$Bar）；lambda 合成帧等解析异常的帧跳过。
    """
    frames: dict[str, StackFrame] = {}
    if not stack_trace:
        return frames
    for line in stack_trace.splitlines():
        if len(frames) >= max_frames:
            break
        match = STACK_FRAME_PATTERN.match(line)
        if not match:
            continue
        class_name, method, file_name, line_no = match.groups()
        simple_name = file_name[:-5] if file_name.endswith(".java") else file_name
        if not (class_name.endswith("." + simple_name) or class_name == simple_name):
            continue
        if not class_name.startswith(business_prefix):
            continue
        frames.setdefault(class_name, StackFrame(class_name, method, file_name, int(line_no)))
    return frames


def class_name_to_path(class_name: str) -> str:
    """类名转仓库内文件路径（标准 Maven 布局）。

    test 包（类名含 .test. 或以 Test/Tests/IT 结尾）对应 src/test/java，
    其余在 src/main/java 下。
    """
    package_path = class_name.replace(".", "/") + ".java"
    is_test = (
        ".test." in class_name
        or class_name.endswith("Test")
        or class_name.endswith("Tests")
        or class_name.endswith("IT")
    )
    return ("src/test/java/" if is_test else "src/main/java/") + package_path


def business_file_paths(stack_trace: str | None, max_frames: int) -> list[str]:
    """按堆栈顺序解析业务帧并转为仓库文件路径（修复自救用）。"""
    return [class_name_to_path(name) for name in parse_business_frames(stack_trace, max_frames)]
