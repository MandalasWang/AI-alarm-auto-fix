"""文本按 UTF-8 字节预算截断。

飞书卡片消息整体限制 30KB（按 UTF-8 字节数，中文一字 3 字节），
卡片变量在构建时按字节预算截断。截断只影响卡片展示，
完整内容始终落库与缓存，可按 traceId 查询。
"""
from __future__ import annotations

TRUNCATED_MARK = "\n...(内容过长已截断)"


def utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def truncate_utf8(value: str | None, max_bytes: int, mark: str = TRUNCATED_MARK) -> str | None:
    """按 UTF-8 字节预算从头保留内容，超限截断并追加标记。

    逐码点累计字节数，保证在字符边界截断（不会切出半个字符/代理对）。
    """
    if not value or utf8_len(value) <= max_bytes:
        return value
    used = 0
    index = 0
    end = 0
    while index < len(value):
        code_point = ord(value[index])
        if 0xD800 <= code_point <= 0xDBFF and index + 1 < len(value):
            # 代理对：占 4 字节，作为一个整体
            cp_bytes, width = 4, 2
        else:
            width = 1
            cp_bytes = 1 if code_point <= 0x7F else 2 if code_point <= 0x7FF else 3 if code_point <= 0xFFFF else 4
        if used + cp_bytes > max_bytes:
            break
        used += cp_bytes
        index += width
        end = index
    return value[:end] + mark


def truncate_chars(value: str | None, max_chars: int, mark: str = "...") -> str | None:
    """按字符数截断（用于堆栈等纯展示字段，与 Java 版 substring 语义一致）。"""
    if value is None or len(value) <= max_chars:
        return value
    return value[:max_chars] + mark
