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

    # ---------------------------------------------------------------- 看板查询（Web 用）

    #: 列表/扫描场景的显式列（不含大字段也不会误读缺列；analyze_content 仅列表页需要）
    _LIST_COLUMNS = (
        "id, trace_id, analyze_content, service_name, `interface`, create_time, update_time,"
        " fix_status, fix_summary, fix_time, problem_type, fingerprint, repeat_type, root_id"
    )

    def page_alarms(self, start: datetime, end: datetime, service: str | None = None,
                    fix_status: str | None = None, keyword: str | None = None,
                    offset: int = 0, limit: int = 20) -> tuple[list[AlarmAnalyzeRecord], int]:
        """分页查告警（创建时间倒序），返回 (记录列表, 总数)。

        fix_status='NULL' 表示筛选未发起修复的告警；keyword 模糊匹配 traceId / 接口。
        """
        where, args = ["create_time >= %s", "create_time < %s"], [start, end]
        if service:
            where.append("service_name = %s")
            args.append(service)
        if fix_status == "NULL":
            where.append("fix_status IS NULL")
        elif fix_status:
            where.append("fix_status = %s")
            args.append(fix_status)
        if keyword:
            where.append("(trace_id LIKE %s OR `interface` LIKE %s)")
            args.extend([f"%{keyword}%", f"%{keyword}%"])
        clause = " WHERE " + " AND ".join(where)
        with self._connect() as connection:
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute("SELECT COUNT(*) AS total FROM alarm_analyze_result" + clause, args)
                total = int(cursor.fetchone()["total"])
                if total == 0:
                    return [], 0
                cursor.execute(
                    f"SELECT {self._LIST_COLUMNS} FROM alarm_analyze_result{clause}"
                    " ORDER BY create_time DESC, id DESC LIMIT %s OFFSET %s",
                    [*args, limit, offset],
                )
                rows = cursor.fetchall()
        return [_to_record(row) for row in rows], total

    def get_by_id(self, record_id: int) -> AlarmAnalyzeRecord | None:
        with self._connect() as connection:
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute("SELECT * FROM alarm_analyze_result WHERE id = %s", (record_id,))
                row = cursor.fetchone()
        return _to_record(row) if row else None

    def list_services(self, limit: int = 500) -> list[str]:
        """去重服务名列表（看板筛选项）。"""
        with self._connect() as connection:
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    "SELECT DISTINCT service_name FROM alarm_analyze_result"
                    " WHERE service_name IS NOT NULL AND service_name <> ''"
                    " ORDER BY service_name LIMIT %s",
                    (limit,),
                )
                rows = cursor.fetchall()
        return [str(row["service_name"]) for row in rows]

    def dashboard_aggregates(self, start: datetime, service: str | None = None,
                             weekly_start: datetime | None = None,
                             daily_start: datetime | None = None) -> dict:
        """看板聚合（单连接多条聚合 SQL，全部走 idx_create_time 索引）：

        - status / problem_type / repeat_type：分布计数（NULL 归一为 'NULL' 键）；
        - daily：按创建日期的 新增/已修复/重复/无需处理/待人工 计数；
        - daily_fixed：按修复日期（fix_time）的已修复数（趋势图「每日修复」折线）；
        - weekly / weekly_fixed / weekly_repeated：按 ISO 周的 告警数/修复数/重复数
          （键如 "2026-W37"，看板近 12 周对比），传 weekly_start 才统计；
        - daily_start：每日序列的独立统计起点（缺省=start；看板日折线固定近 30 天时传入）。
        """
        filters, args = "WHERE create_time >= %s", [start]
        if service:
            filters += " AND service_name = %s"
            args.append(service)
        daily_filters, daily_args = "WHERE create_time >= %s", [daily_start or start]
        if service:
            daily_filters += " AND service_name = %s"
            daily_args.append(service)
        with self._connect() as connection:
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                aggregates: dict[str, dict] = {}
                for column in ("fix_status", "problem_type", "repeat_type"):
                    cursor.execute(
                        f"SELECT IFNULL({column}, 'NULL') AS k, COUNT(*) AS c"
                        f" FROM alarm_analyze_result {filters} GROUP BY k",
                        args,
                    )
                    aggregates[column] = {row["k"]: int(row["c"]) for row in cursor.fetchall()}
                cursor.execute(
                    "SELECT DATE(create_time) AS d, COUNT(*) AS total,"
                    " SUM(fix_status = 'FIXED') AS fixed, SUM(repeat_type = 'REPEATED') AS repeated,"
                    " SUM(fix_status = 'NOT_NEEDED') AS not_needed,"
                    " SUM(fix_status IN ('HANDOVER', 'MANUAL_PENDING')) AS manual"
                    f" FROM alarm_analyze_result {daily_filters} GROUP BY d ORDER BY d",
                    daily_args,
                )
                daily = {row["d"].isoformat(): {"total": int(row["total"]),
                                                "fixed": int(row["fixed"] or 0),
                                                "repeated": int(row["repeated"] or 0),
                                                "not_needed": int(row["not_needed"] or 0),
                                                "manual": int(row["manual"] or 0)}
                         for row in cursor.fetchall()}
                cursor.execute(
                    "SELECT DATE(fix_time) AS d, COUNT(*) AS c FROM alarm_analyze_result"
                    f" {daily_filters} AND fix_status = 'FIXED' AND fix_time IS NOT NULL"
                    " GROUP BY d ORDER BY d",
                    daily_args,
                )
                daily_fixed = {row["d"].isoformat(): int(row["c"]) for row in cursor.fetchall()}
                weekly: dict[str, int] = {}
                weekly_fixed: dict[str, int] = {}
                weekly_repeated: dict[str, int] = {}
                if weekly_start is not None:
                    weekly_filters, weekly_args = "WHERE create_time >= %s", [weekly_start]
                    if service:
                        weekly_filters += " AND service_name = %s"
                        weekly_args.append(service)
                    cursor.execute(
                        # %% 转义：PyMySQL 对带参 SQL 做 Python % 格式化，字面 % 须双写
                        "SELECT DATE_FORMAT(create_time, '%%x-%%v') AS w, COUNT(*) AS c"
                        f" FROM alarm_analyze_result {weekly_filters} GROUP BY w ORDER BY w",
                        weekly_args,
                    )
                    weekly = {row["w"]: int(row["c"]) for row in cursor.fetchall()}
                    cursor.execute(
                        "SELECT DATE_FORMAT(fix_time, '%%x-%%v') AS w, COUNT(*) AS c"
                        " FROM alarm_analyze_result"
                        f" {weekly_filters} AND fix_status = 'FIXED' AND fix_time IS NOT NULL"
                        " GROUP BY w ORDER BY w",
                        weekly_args,
                    )
                    weekly_fixed = {row["w"]: int(row["c"]) for row in cursor.fetchall()}
                    cursor.execute(
                        "SELECT DATE_FORMAT(create_time, '%%x-%%v') AS w, COUNT(*) AS c"
                        " FROM alarm_analyze_result"
                        f" {weekly_filters} AND repeat_type = 'REPEATED' GROUP BY w ORDER BY w",
                        weekly_args,
                    )
                    weekly_repeated = {row["w"]: int(row["c"]) for row in cursor.fetchall()}
                cursor.execute(
                    "SELECT service_name AS svc, COUNT(*) AS total,"
                    " SUM(fix_status = 'FIXED') AS fixed, SUM(fix_status IS NULL) AS pending,"
                    " SUM(fix_status = 'FAILED') AS failed"
                    f" FROM alarm_analyze_result {filters}"
                    " GROUP BY service_name ORDER BY total DESC LIMIT 10",
                    args,
                )
                by_service = [{"service": row["svc"], "total": int(row["total"]),
                               "fixed": int(row["fixed"] or 0), "pending": int(row["pending"] or 0),
                               "failed": int(row["failed"] or 0)}
                              for row in cursor.fetchall()]
        return {"fix_status": aggregates["fix_status"], "problem_type": aggregates["problem_type"],
                "repeat_type": aggregates["repeat_type"], "daily": daily, "daily_fixed": daily_fixed,
                "weekly": weekly, "weekly_fixed": weekly_fixed, "weekly_repeated": weekly_repeated,
                "by_service": by_service}

    def list_fixed_since(self, start: datetime, limit: int = 50) -> list[AlarmAnalyzeRecord]:
        """窗口内按修复时间倒序的已修复记录（看板合并状态扫描用，不含 analyze_content 大字段）。"""
        with self._connect() as connection:
            with connection.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(
                    f"SELECT {self._LIST_COLUMNS} FROM alarm_analyze_result"
                    " WHERE fix_status = 'FIXED' AND fix_time IS NOT NULL AND fix_time >= %s"
                    " ORDER BY fix_time DESC LIMIT %s",
                    (start, limit),
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
