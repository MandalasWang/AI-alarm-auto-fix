"""按告警中提取的表名查库收集上下文（对应 Java JdbcAlarmContextFetcher）。

数据源按告警服务名解析，优先级：Nacos 动态解析（该服务自己的库）→
service_datasources 静态映射 → 名为 default 的数据源兜底。
对每张表（经 information_schema 校验真实存在）：DDL、行数、按 businessKeys
匹配列名的样本行。仅执行只读查询，任何单表异常不影响其余表与主流程。

严谨性增强：样本行查询用参数化 SQL（%s 占位），杜绝 Java 版手工转义单引号的
注入风险面；每次采集用短连接（低频场景，免长连接失效维护）。
"""
from __future__ import annotations

import json
import logging
import re

import pymysql

from app.config import AppConfig
from app.models.alarm import AlarmLog
from app.providers.nacos import NacosConfigResolver
from app.extractors.tables import extract_tables

log = logging.getLogger(__name__)

#: 单表样本行的单字段值最大长度
VALUE_MAX_LENGTH = 200
#: 上下文总长度上限
CONTEXT_MAX_LENGTH = 6000

_TABLE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")

# ---- 敏感数据掩码（analyse.db.mask_sensitive=true 时作用于样本行字符串值）----
# 手机号：保留前 3 后 4（13x-19x 号段，前后不能紧邻数字防误切长号码）
_PHONE_PATTERN = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
# 18 位身份证：保留前 4 后 3（末位可含 X/x）；15 位纯数字老证不与手机号冲突场景少，从简不单列
_ID_CARD_PATTERN = re.compile(r"(?<!\d)(\d{4})\d{10}(\d{3}[\dXx])(?!\d)")
# 邮箱：本地部分保留前 2 位（@ 与域名原样，域名多为企业内部已知信息）
_EMAIL_PATTERN = re.compile(r"(?<![\w.%-])([\w]{1,2})[\w.%-]*@([\w-]+\.[\w.-]+)")

_MASK_PATTERNS = (
    (_ID_CARD_PATTERN, r"\1***********\2"),
    (_PHONE_PATTERN, r"\1****\2"),
    (_EMAIL_PATTERN, r"\1***@\2"),
)


def mask_sensitive_text(text: str) -> str:
    """对文本中的 手机号/身份证/邮箱 做部分掩码（纯函数，单测锁定）。

    掩码目标：样本行数据进 LLM 上下文/卡片/看板前降低敏感数据出库面，
    同时保留首尾几位供排障比对（如用脱敏后的值反查业务日志）。
    """
    masked = text
    for pattern, replacement in _MASK_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


class DatabaseContextFetcher:
    def __init__(self, config: AppConfig, nacos_resolver: NacosConfigResolver):
        self._db_config = config.analyse.db
        self._nacos_resolver = nacos_resolver

    def fetch_context(self, alarm: AlarmLog) -> str | None:
        if not self._db_config.enabled:
            log.info("告警查库未启用(analyse.db.enabled=false)，跳过。source=%s", alarm.source)
            return None
        tables = extract_tables(alarm.message, alarm.stack_trace)
        if not tables:
            log.info("告警中未提取到表名，跳过查库。source=%s", alarm.source)
            return None

        connection = self._open_connection(alarm.source)
        if connection is None:
            return None
        try:
            parts: list[str] = []
            for table in tables:
                section = self._table_context(connection, table, alarm)
                if section:
                    parts.append(section)
            context = "\n".join(parts)
            return _truncate(context, CONTEXT_MAX_LENGTH) if context else None
        finally:
            connection.close()

    # ------------------------------------------------------------------ 连接

    def _open_connection(self, source: str):
        """按服务名解析数据源并建立连接；解析失败返回 None。"""
        db = self._db_config
        db_source = self._nacos_resolver.resolve_datasource(source)
        if db_source is None:
            ds_name = db.service_datasources.get(source) or "default"
            db_source = db.datasources.get(ds_name)
        if db_source is None and db.nacos.enabled:
            # Nacos 开着但没解析到（服务名不在 Nacos / 未配数据源），提示便于排查
            log.warning("Nacos 未解析到服务数据源，且无静态配置兜底, source=%s", source)
        if db_source is None or not db_source.url:
            log.warning("告警查库未匹配到数据源, source=%s", source)
            return None
        try:
            from app.config import ConfigError

            host, port, database = db_source.as_pymysql_params()
            timeout_s = db.query_timeout_seconds
            connection = pymysql.connect(
                host=host, port=port, database=database,
                user=db_source.username, password=db_source.password,
                charset="utf8mb4",
                connect_timeout=timeout_s,
                read_timeout=timeout_s,
                write_timeout=timeout_s,
            )
            return connection
        except ConfigError as exc:
            log.error("数据源 URL 非法, source=%s, err=%s", source, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("告警查库获取连接失败, source=%s, err=%s", source, exc)
            return None

    # ------------------------------------------------------------------ 单表采集

    def _table_context(self, connection, table: str, alarm: AlarmLog) -> str | None:
        try:
            # 表名白名单校验防注入：仅允许字母数字下划线
            if not _TABLE_NAME_PATTERN.match(table):
                return None
            # 表必须真实存在于当前库，避免告警文本里的普通单词被当表名
            if not self._table_exists(connection, table):
                return None

            parts = [f"## 表 {table}"]
            ddl = self._query_scalar(connection, f"SHOW CREATE TABLE `{table}`", column=1)
            if ddl:
                parts.append(f"DDL: {ddl}")
            count = self._query_scalar(connection, f"SELECT COUNT(*) FROM `{table}`")
            if count is not None:
                parts.append(f"行数: {count}")
            sample = self._sample_rows(connection, table, alarm)
            if sample:
                parts.append(f"样本数据:\n{sample}")
            return "\n".join(parts) + "\n"
        except Exception as exc:  # noqa: BLE001
            log.warning("告警查库单表上下文获取失败, table=%s, err=%s", table, exc)
            return None

    @staticmethod
    def _table_exists(connection, table: str) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
                (connection.db.decode() if isinstance(connection.db, bytes) else connection.db, table),
            )
            count = cursor.fetchone()[0]
        return str(count) != "0"

    def _sample_rows(self, connection, table: str, alarm: AlarmLog) -> str | None:
        """按 businessKeys 中的值查样本行（列名与键名一致时命中），未命中退化为 LIMIT 采样。"""
        limit = max(1, self._db_config.sample_limit)
        sql = self._biz_key_query(connection, table, alarm) or f"SELECT * FROM `{table}` LIMIT {limit}"
        try:
            with connection.cursor(pymysql.cursors.Cursor) as cursor:
                cursor.execute(sql)  # 参数化或经白名单校验的静态 SQL
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchmany(limit)
        except Exception as exc:  # noqa: BLE001
            log.warning("样本行查询失败, table=%s, err=%s", table, exc)
            return None
        lines = []
        for row in rows:
            record = {
                column: self._cell_text(value) for column, value in zip(columns, row)
            }
            lines.append(json.dumps(record, ensure_ascii=False, default=str))
        return "\n".join(lines)

    def _cell_text(self, value) -> str | None:
        """单格值渲染：截断 + 敏感掩码（mask_sensitive=true 时，手机号/身份证/邮箱部分打码）。"""
        if value is None:
            return None
        text = _truncate(str(value), VALUE_MAX_LENGTH)
        if self._db_config.mask_sensitive:
            text = mask_sensitive_text(text)
        return text

    def _biz_key_query(self, connection, table: str, alarm: AlarmLog) -> str | None:
        """businessKeys 的键与表列名匹配时构造等值查询（参数化），任一键可用即返回。"""
        business_keys = alarm.business_keys
        if not business_keys:
            return None
        columns = self._load_columns(connection, table)
        for key in business_keys:
            value = business_keys[key]
            if key.lower() in columns and value is not None and re.fullmatch(r"[a-zA-Z0-9_]+", key):
                return f"SELECT * FROM `{table}` WHERE `{key}` = {_quote_literal(str(value))} LIMIT 1"
        return None

    @staticmethod
    def _load_columns(connection, table: str) -> set[str]:
        columns: set[str] = set()
        try:
            db_name = connection.db.decode() if isinstance(connection.db, bytes) else connection.db
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
                    (db_name, table),
                )
                columns = {str(row[0]).lower() for row in cursor.fetchall()}
        except Exception as exc:  # noqa: BLE001
            log.warning("加载表列名失败, table=%s, err=%s", table, exc)
        return columns

    def _query_scalar(self, connection, sql: str, column: int = 0):
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql)
                row = cursor.fetchone()
                return row[column] if row else None
        except Exception as exc:  # noqa: BLE001
            log.warning("标量查询失败, sql=%s, err=%s", sql, exc)
            return None


def _quote_literal(value: str) -> str:
    """SQL 字符串字面量转义（键名/表名已白名单校验，值在此处转义）。"""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _truncate(text: str, max_length: int) -> str:
    return text if len(text) <= max_length else text[:max_length] + "..."
