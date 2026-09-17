"""alarm_analyze_result 表仓储。

严谨性：全部 SQL 参数化（%s），列名白名单常量，杜绝拼接；未配置 storage.database 时
仓储为 None，由调用方降级（分析照常推送卡片，仅回调修复/日报不可用），与 Java 版
「落库失败不阻断主流程」语义一致。
"""
from __future__ import annotations

import logging
from datetime import datetime

import pymysql

from app.config import DatabaseStoreConfig
from app.core.errors import ConfigError
from app.models.alarm import AlarmAnalyzeRecord

log = logging.getLogger(__name__)


class AlarmRepository:
    def __init__(self, config: DatabaseStoreConfig):
        host, port, database = config.as_pymysql_params()
        self._params = (host, port, database)
        self._username = config.username
        self._password = config.password

    @classmethod
    def create_if_configured(cls, config: DatabaseStoreConfig) -> "AlarmRepository | None":
        if not config.configured:
            log.warning("未配置 storage.database，分析结果不落库（卡片回调修复与日报将不可用）")
            return None
        try:
            repo = cls(config)
        except ConfigError as exc:
            log.error("业务库配置非法，分析结果不落库: %s", exc)
            return None
        log.info("业务库仓储就绪: %s:%s/%s", repo._params[0], repo._params[1], repo._params[2])
        return repo

    # ------------------------------------------------------------------ 写

    def save(self, result_json: str, trace_id: str | None, service: str | None,
             interface_uri: str | None, problem_type: str | None = None,
             fingerprint: str | None = None, repeat_type: str | None = None,
             root_id: int | None = None, fix_status: str | None = None) -> int | None:
        now = datetime.now()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO alarm_analyze_result"
                    " (trace_id, analyze_content, service_name, `interface`, create_time, update_time,"
                    "  problem_type, fingerprint, repeat_type, root_id, fix_status)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (trace_id, result_json, service, interface_uri, now, now,
                     problem_type, fingerprint, repeat_type, root_id, fix_status),
                )
                return cursor.lastrowid

    def update_fix_result(self, record_id: int, fix_status: str, fix_summary: str) -> None:
        now = datetime.now()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE alarm_analyze_result"
                    " SET fix_status = %s, fix_summary = %s, fix_time = %s, update_time = %s"
                    " WHERE id = %s",
                    (fix_status, fix_summary[:1000], now, now, record_id),
                )

    # ------------------------------------------------------------------ 读

    def latest_by_trace_id(self, trace_id: str) -> AlarmAnalyzeRecord | None:
        with self._connect() as connection:
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    "SELECT * FROM alarm_analyze_result WHERE trace_id = %s"
                    " ORDER BY create_time DESC LIMIT 1",
                    (trace_id,),
                )
                row = cursor.fetchone()
        return _to_record(row) if row else None

    def latest_by_fingerprint(self, fingerprint: str, since: datetime) -> AlarmAnalyzeRecord | None:
        """窗口内同指纹最新一条历史记录（重复/复发识别用，依赖 idx_fingerprint_time 索引）。"""
        with self._connect() as connection:
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    "SELECT * FROM alarm_analyze_result"
                    " WHERE fingerprint = %s AND create_time >= %s"
                    " ORDER BY create_time DESC LIMIT 1",
                    (fingerprint, since),
                )
                row = cursor.fetchone()
        return _to_record(row) if row else None

    def list_between(self, start: datetime, end: datetime) -> list[AlarmAnalyzeRecord]:
        """按创建时间区间查询（左闭右开），按时间升序（日报统计用）。"""
        with self._connect() as connection:
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    "SELECT * FROM alarm_analyze_result"
                    " WHERE create_time >= %s AND create_time < %s ORDER BY create_time ASC",
                    (start, end),
                )
                rows = cursor.fetchall()
        return [_to_record(row) for row in rows]

    # ------------------------------------------------------------------ 内部

    def ping(self) -> bool:
        """连通性探活（readyz 用）；任何异常按不可用报告。"""
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    return cursor.fetchone() is not None
        except Exception:  # noqa: BLE001
            return False

    def _connect(self) -> pymysql.connections.Connection:
        host, port, database = self._params
        return pymysql.connect(
            host=host, port=port, database=database,
            user=self._username, password=self._password,
            charset="utf8mb4", connect_timeout=5, read_timeout=10, write_timeout=10,
            autocommit=True,
        )


def _to_record(row: dict) -> AlarmAnalyzeRecord:
    return AlarmAnalyzeRecord(
        id=row.get("id"),
        trace_id=row.get("trace_id"),
        analyze_content=row.get("analyze_content"),
        service_name=row.get("service_name"),
        interface_uri=row.get("interface"),
        create_time=row.get("create_time"),
        update_time=row.get("update_time"),
        fix_status=row.get("fix_status"),
        fix_summary=row.get("fix_summary"),
        fix_time=row.get("fix_time"),
        problem_type=row.get("problem_type"),
        fingerprint=row.get("fingerprint"),
        repeat_type=row.get("repeat_type"),
        root_id=row.get("root_id"),
    )
