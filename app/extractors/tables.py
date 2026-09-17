"""从告警日志/堆栈中提取涉及的数据库表名（对应 Java SqlTableExtractor）。"""
from __future__ import annotations

import re

# SQL 语句中的表名：insert into xxx / update xxx set / from xxx / join xxx on
_SQL_TABLE_PATTERN = re.compile(
    r"(?i)(?:insert\s+into|update|delete\s+from|from|join)\s+[`\"]?([a-zA-Z_][a-zA-Z0-9_]{1,63})[`\"]?"
)

# SQL 异常消息里的表名，如 Table 'order_db.t_order' doesn't exist
_SQL_ERROR_TABLE_PATTERN = re.compile(
    r"(?i)table\s+[`\"']?([a-zA-Z0-9_]{1,63})\.[`\"']?([a-zA-Z_][a-zA-Z0-9_]{1,63})[`\"']?"
)

_MAX_TABLES = 5

# 过滤 SQL 关键字误判（from 后跟 select 之类的子查询）
_SQL_KEYWORDS = {"SELECT", "WHERE", "GROUP", "ORDER", "LIMIT", "DUAL", "SET", "VALUES", "ON", "AS"}


def extract_tables(message: str | None, stack_trace: str | None) -> list[str]:
    """提取去重后的表名，按出现顺序保序，最多 5 个。"""
    text = (message or "") + "\n" + (stack_trace or "")
    if not text.strip():
        return []
    tables: list[str] = []

    # 优先取 SQL 异常消息里 db.table 形式的表
    for match in _SQL_ERROR_TABLE_PATTERN.finditer(text):
        if len(tables) >= _MAX_TABLES:
            break
        name = match.group(2)
        if name not in tables:
            tables.append(name)

    # 再补 SQL 关键字后的表名
    for match in _SQL_TABLE_PATTERN.finditer(text):
        if len(tables) >= _MAX_TABLES:
            break
        candidate = match.group(1)
        if candidate.upper() not in _SQL_KEYWORDS and candidate not in tables:
            tables.append(candidate)
    return tables
