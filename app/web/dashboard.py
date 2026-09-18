"""Web 告警看板：只读页面 + JSON API。

- 页面：GET /dashboard（单文件模板 app/web/templates/dashboard.html，原生 JS 自绘图表，无前端构建链）；
- API：/api/dashboard/overview | /alarms | /alarms/{id} | /services，统一 {code, message, data}；
- 「是否解决」取 fix_status 五态；「是否合并」以 commit 对比 GitLab 为准：
  从 fix_summary 解析 commit shortId，查该 commit 已被哪些分支包含（含修复基线分支即已合并），
  辅以 MR 状态兜底（squash merge 会换 sha），进程内 TTL 缓存避免高频打 GitLab；
- 鉴权：dashboard.username/password 同时配置才启用 HTTP Basic；业务库未配置时 API 返回明确错误。
"""
from __future__ import annotations

import base64
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.config import AppConfig
from app.container import Container
from app.core.fingerprint import problem_type_label
from app.fixing.committer import build_branch_name
from app.fixing.service import fix_status_label
from app.models.alarm import AlarmAnalyseResult, AlarmAnalyzeRecord
from app.providers.gitlab import GitLabClient
from app.storage.alarm_repository import AlarmRepository

log = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"

#: 合并状态最多扫描最近 N 条已修复记录（首屏耗时可控；超出部分指标上带 truncated 标记）
_MERGE_SCAN_LIMIT = 50

_NULL_KEY = "NULL"  # dashboard_aggregates 里 IFNULL 归一后的空值键

_REPEAT_LABELS = {"NORMAL": "首次", "REPEATED": "重复", "RECURRED": "复发",
                   "NULL": "未识别（历史数据）"}

_MR_URL_PATTERN = re.compile(r"MR:\s*(https?://\S+)")
_COMMIT_URL_PATTERN = re.compile(r"查看改动:\s*(https?://\S+)")
_COMMIT_ID_PATTERN = re.compile(r"commit:\s*([0-9a-f]{7,40})", re.IGNORECASE)
_BRANCH_LINE_PATTERN = re.compile(r"分支:\s*(\S+)")


def _ok(data: object = True) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _fail(message: str) -> dict:
    return {"code": 500, "message": message, "data": None}


def _fix_status_label_of(fix_status: str | None) -> str:
    """终态用 fixing.service 的文案，NULL（未发起）与未知值在此兜底。"""
    return fix_status_label(fix_status) if fix_status and fix_status != _NULL_KEY else "未处理"


# ------------------------------------------------------------------ 纯函数（单测覆盖）

def parse_mr_url(fix_summary: str | None) -> str | None:
    """从 fix_summary 的「MR: <url>」行解析 MR 链接。"""
    if not fix_summary:
        return None
    match = _MR_URL_PATTERN.search(fix_summary)
    return match.group(1) if match else None


def parse_commit_url(fix_summary: str | None) -> str | None:
    """从 fix_summary 的「查看改动: <url>」行解析 commit 链接。"""
    if not fix_summary:
        return None
    match = _COMMIT_URL_PATTERN.search(fix_summary)
    return match.group(1) if match else None


def parse_commit_id(fix_summary: str | None) -> str | None:
    """从 fix_summary 的「commit: <shortId>」行解析 commit shortId。"""
    if not fix_summary:
        return None
    match = _COMMIT_ID_PATTERN.search(fix_summary)
    return match.group(1) if match else None


def parse_branch_name(fix_summary: str | None) -> str | None:
    """从 fix_summary 的「分支: <name>」行解析实际 fix 分支名。

    撞分支重试后分支名带时分秒后缀，按 traceId+时间重算不可靠，故优先读落库的实际分支；
    旧记录（该行存在前）返回 None，回退重算。
    """
    if not fix_summary:
        return None
    match = _BRANCH_LINE_PATTERN.search(fix_summary)
    return match.group(1) if match else None


def window_start(days: int, now: datetime | None = None) -> datetime:
    """统计窗口起点：N 天前的零点（含当天，如 days=14 覆盖今天与前 13 天）。"""
    moment = now or datetime.now()
    return moment.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)


def resolve_rate(total: int, fixed: int, not_needed: int,
                 manual: int = 0, repeated: int = 0, manual_resolved: int = 0) -> float | None:
    """解决率口径与日报一致：(AI修复+人工解决) / (告警总数 − 无需处理 − 转人工 − 待人工 − 重复)。

    人工已解决（MANUAL_RESOLVED）是真实解决，计入分子、留在分母；
    重复只剔除 REPEATED（复发 RECURRED 是新问题，保留在分母）；分母非正返回 None（不展示）。
    """
    denominator = total - not_needed - manual - repeated
    if denominator <= 0:
        return None
    return round((fixed + manual_resolved) / denominator * 100, 1)


def build_daily_series(start: datetime, aggregates: dict,
                       today: datetime | None = None) -> list[dict]:
    """零填充每日序列：date / total(按创建日新增) / fixed_by_time(按修复日解决) / repeated /
    not_needed / manual / manual_resolved（后三者供「当日解决率」按日报口径剔除或计入）。"""
    daily = aggregates.get("daily") or {}
    daily_fixed = aggregates.get("daily_fixed") or {}
    day, last = start.date(), (today or datetime.now()).date()
    series: list[dict] = []
    while day <= last:
        key = day.isoformat()
        bucket = daily.get(key) or {}
        series.append({"date": key,
                       "total": bucket.get("total", 0),
                       "fixed": bucket.get("fixed", 0),  # 当日创建且已修复
                       "fixed_by_time": daily_fixed.get(key, 0),  # 当日完成修复的（含此前创建）
                       "repeated": bucket.get("repeated", 0),
                       "not_needed": bucket.get("not_needed", 0),
                       "manual": bucket.get("manual", 0),
                       "manual_resolved": bucket.get("manual_resolved", 0)})
        day += timedelta(days=1)
    return series


def weekly_start(moment: datetime, weeks: int = 12) -> datetime:
    """近 N 周统计起点：当前 ISO 周的周一往前推 weeks-1 周（零点）。"""
    monday = moment.date() - timedelta(days=moment.date().weekday())
    return datetime.combine(monday - timedelta(weeks=weeks - 1), datetime.min.time())


def _parse_week_key(key) -> tuple[int, int] | None:
    """周键 → (ISO 年, 周数)，兼容 "2026-W37"（Python）与 "2026-37"（MySQL %x-%v）。"""
    parts = str(key).split("-")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1].lstrip("Ww"))
    except ValueError:
        return None


def build_weekly_series(moment: datetime, weekly: dict[str, int],
                        weeks: int = 12, weekly_fixed: dict[str, int] | None = None,
                        weekly_repeated: dict[str, int] | None = None) -> list[dict]:
    """零填充近 N 周 ISO 周序列：week/label/total/fixed_by_time/repeated。

    字段形状与 build_daily_series 对齐（前端同一折线渲染器复用）；
    匹配按 (ISO 年, 周数) 数值进行，与来源键的拼写（是否带 W/补零）无关。
    """
    monday = moment.date() - timedelta(days=moment.date().weekday())
    first = monday - timedelta(weeks=weeks - 1)
    by_key: dict[tuple[int, int], int] = {}
    fixed_by_key: dict[tuple[int, int], int] = {}
    repeated_by_key: dict[tuple[int, int], int] = {}
    for key, count in (weekly or {}).items():
        parsed = _parse_week_key(key)
        if parsed:
            by_key[parsed] = count
    for key, count in (weekly_fixed or {}).items():
        parsed = _parse_week_key(key)
        if parsed:
            fixed_by_key[parsed] = count
    for key, count in (weekly_repeated or {}).items():
        parsed = _parse_week_key(key)
        if parsed:
            repeated_by_key[parsed] = count
    series: list[dict] = []
    for i in range(weeks):
        start = first + timedelta(weeks=i)
        iso_year, iso_week, _ = start.isocalendar()
        series.append({"week": f"{iso_year}-W{iso_week:02d}",
                       "label": start.strftime("%m-%d"),
                       "total": by_key.get((iso_year, iso_week), 0),
                       "fixed_by_time": fixed_by_key.get((iso_year, iso_week), 0),
                       "repeated": repeated_by_key.get((iso_year, iso_week), 0)})
    return series


# ------------------------------------------------------------------ 合并状态解析

class MergeStateResolver:
    """「是否合并」解析器（进程内 TTL 缓存，线程安全）。

    判定（任一成立即已合并）：
    1. commit 对比（主）：fix_summary 里的 commit shortId 查 GitLab「包含该 commit 的分支」，
       修复基线分支（fix_base_branch）在其中 ⇒ 已合并；
    2. MR 状态（兜底）：按 fix 分支名查任意状态 MR，state=merged ⇒ 已合并（兼容 squash merge 换 sha）。
    GitLab 未配置 / 项目定位失败 / 查询失败 / 记录缺 commit 信息 ⇒ known=False，展示为未知。
    """

    def __init__(self, config: AppConfig, gitlab: GitLabClient | None, ttl_seconds: int = 300):
        self._git = config.analyse.git
        self._gitlab = gitlab
        self._ttl = max(30, ttl_seconds)
        self._cache: dict[tuple[str, str, str], tuple[float, dict]] = {}
        self._projects: dict[str, tuple[float, str | None]] = {}
        self._lock = threading.Lock()

    def resolve(self, service: str | None, trace_id: str | None, fix_time: datetime | None,
                fix_summary: str | None) -> dict:
        """解析一条已修复记录的合并状态；返回值字段见 _base_state。

        分支名优先读 fix_summary 落库的「分支: x」行（撞分支重试后名字带后缀，重算不可靠），
        旧记录缺失时回退按 traceId+修复时间重算。
        """
        branch = parse_branch_name(fix_summary) or build_branch_name(trace_id, fix_time)
        commit_id = parse_commit_id(fix_summary)
        base = {
            "branch": branch, "commit_id": commit_id, "commit_url": parse_commit_url(fix_summary),
            "mr_url": parse_mr_url(fix_summary), "mr_state": None,
            "commit_merged": None, "merged": False, "known": False,
        }
        if not self._git.api_ready or self._gitlab is None:
            return base
        project_id = self._project_id(service or "")
        if not project_id:
            return base
        state = self._lookup(project_id, branch, commit_id or "")
        commit_merged = state.get("commit_merged")
        mr_state = state.get("mr_state")
        # 可判定（known）语义：commit 在基线分支中、或 MR 状态明确（merged/opened/closed）、
        # 或「有 commit 且确认不在基线 + 确认无 MR」；GitLab 查询失败且无法交叉确认 ⇒ 未知
        if commit_merged is True or mr_state == "merged":
            merged, known = True, True
        elif mr_state in ("opened", "closed"):
            merged, known = False, True
        elif commit_merged is not None:  # 有 commit 且确认不在基线，亦无 MR ⇒ 确认未合并
            merged, known = False, True
        else:
            merged, known = False, False
        return {**base,
                "known": known,
                "merged": merged,
                "commit_merged": commit_merged,
                "mr_state": mr_state,
                "mr_url": state.get("mr_url") or base["mr_url"]}

    # ------------------------------------------------------------------ 内部

    def _lookup(self, project_id: str, branch: str, commit_id: str) -> dict:
        key = (project_id, branch, commit_id)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
        state = self._fetch_state(project_id, branch, commit_id)
        with self._lock:
            self._cache[key] = (time.monotonic() + self._ttl, state)
            if len(self._cache) > 2000:  # 兜底防长期运行膨胀；正常量级触达不到
                deadline = time.monotonic()
                self._cache = {k: v for k, v in self._cache.items() if v[0] > deadline}
        return state

    def _fetch_state(self, project_id: str, branch: str, commit_id: str) -> dict:
        state: dict = {"commit_merged": None, "mr_state": None, "mr_url": None}
        if commit_id:
            refs = self._gitlab.commit_branch_refs(project_id, commit_id)
            if refs is not None:  # None=查询失败，保持未知；[]/列表=确认结果
                base_branch = self._git.fix_base_branch or self._git.default_branch
                state["commit_merged"] = base_branch in refs
        mr = self._gitlab.merge_request_by_source_branch(project_id, branch)
        if mr is not None:  # None=查询失败；{}=确认无 MR
            state["mr_state"] = mr.get("state")
            state["mr_url"] = mr.get("web_url")
        return state

    def _project_id(self, service: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            cached = self._projects.get(service)
            if cached and cached[0] > now:
                return cached[1]
        mapped = self._git.projects.get(service) or self._gitlab.search_project(service)
        with self._lock:
            self._projects[service] = (time.monotonic() + self._ttl, mapped)
        return mapped


# ------------------------------------------------------------------ 组装逻辑（单测覆盖）

def build_overview(repository: AlarmRepository, resolver: MergeStateResolver, env: str,
                   days: int, service: str | None, now: datetime | None = None,
                   system_type: str | None = None) -> dict:
    """看板总览：KPI + 状态/归因/服务分布 + 每日（固定近 30 天）与每周（近 12 周）趋势。"""
    moment = now or datetime.now()
    start = window_start(days, moment)
    daily_start = window_start(30, moment)  # 日折线固定近 30 天，不随 KPI 窗口缩放
    agg = repository.dashboard_aggregates(start, service,
                                          weekly_start=weekly_start(moment),
                                          daily_start=daily_start,
                                          system_type=system_type)
    status = agg["fix_status"]
    total = sum(status.values())
    fixed = status.get("FIXED", 0)
    not_needed = status.get("NOT_NEEDED", 0)
    repeat = agg["repeat_type"]
    manual = status.get("HANDOVER", 0) + status.get("MANUAL_PENDING", 0)

    merged, merge_open, merge_checked, merge_scanned, merge_truncated = _merge_scan(
        repository, resolver, start)

    daily_series = build_daily_series(daily_start, agg, moment)
    today_bucket = daily_series[-1] if daily_series else None
    today = None
    if today_bucket is not None:
        today = {
            "total": today_bucket["total"],
            "fixed": today_bucket["fixed"],
            "fixed_by_time": today_bucket["fixed_by_time"],
            "rate": resolve_rate(today_bucket["total"], today_bucket["fixed"],
                                 today_bucket["not_needed"], today_bucket["manual"],
                                 today_bucket["repeated"],
                                 today_bucket.get("manual_resolved", 0)),
        }

    return {
        "env": env,
        "window_days": days,
        "window_start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "today": today,
        "kpi": {
            "total": total,
            "fixed": fixed,
            "not_needed": not_needed,
            "failed": status.get("FAILED", 0),
            "pending": status.get(_NULL_KEY, 0),
            "manual": manual,
            "manual_resolved": status.get("MANUAL_RESOLVED", 0),
            "repeated": repeat.get("REPEATED", 0) + repeat.get("RECURRED", 0),
            "resolve_rate": resolve_rate(total, fixed, not_needed,
                                         manual, repeat.get("REPEATED", 0),
                                         status.get("MANUAL_RESOLVED", 0)),
            "merged": merged,
            "merge_open": merge_open,
            "merge_checked": merge_checked,
            "merge_scanned": merge_scanned,
            "merge_truncated": merge_truncated,
            "merge_rate": round(merged / merge_checked * 100, 1) if merge_checked else None,
        },
        "status_distribution": [
            {"status": key, "label": _fix_status_label_of(None if key == _NULL_KEY else key),
             "count": count}
            for key, count in status.items()],
        "problem_distribution": [
            {"type": key, "label": problem_type_label(None if key == _NULL_KEY else key),
             "count": count}
            for key, count in agg["problem_type"].items()],
        "repeat_distribution": [
            {"type": key, "label": _REPEAT_LABELS.get(key, key), "count": count}
            for key, count in agg["repeat_type"].items()],
        "services": agg.get("by_service") or [],
        "daily": daily_series,
        "weekly": build_weekly_series(moment, agg.get("weekly") or {},
                                      weekly_fixed=agg.get("weekly_fixed") or {},
                                      weekly_repeated=agg.get("weekly_repeated") or {}),
    }


def _merge_scan(repository: AlarmRepository, resolver: MergeStateResolver,
                start: datetime) -> tuple[int, int, int, int, bool]:
    """扫描窗口内已修复记录的合并状态。

    返回 (已合并, 待合并(MR打开), GitLab 可判定数, 扫描数, 是否截断)。
    """
    records = repository.list_fixed_since(start, limit=_MERGE_SCAN_LIMIT + 1)
    truncated = len(records) > _MERGE_SCAN_LIMIT
    merged = merge_open = checked = 0
    for record in records[:_MERGE_SCAN_LIMIT]:
        info = resolver.resolve(record.service_name, record.trace_id,
                                record.fix_time, record.fix_summary)
        if not info["known"]:
            continue
        checked += 1
        if info["merged"]:
            merged += 1
        elif info.get("mr_state") == "opened":
            merge_open += 1
    scanned = min(len(records), _MERGE_SCAN_LIMIT)
    return merged, merge_open, checked, scanned, truncated


def _content_of(record: AlarmAnalyzeRecord) -> dict:
    """analyze_content JSON → camelCase 字段 dict（解析失败返回空 dict）。"""
    parsed = AlarmAnalyseResult.from_json(record.analyze_content or "")
    return parsed.model_dump(by_alias=True, exclude_none=True) if parsed else {}


def _list_item(record: AlarmAnalyzeRecord, content: dict) -> dict:
    level = content.get("level")
    return {
        "id": record.id,
        "trace_id": record.trace_id,
        "service": record.service_name,
        "system_type": record.system_type,
        "interface": record.interface_uri,
        "title": content.get("title") or f"{level or ''} 告警".strip(),
        "level": level,
        "problem_type": record.problem_type,
        "problem_label": problem_type_label(record.problem_type),
        "repeat_type": record.repeat_type,
        "fix_status": record.fix_status,
        "fix_label": _fix_status_label_of(record.fix_status),
        "fix_time": record.fix_time,
        "create_time": record.create_time,
    }


# ------------------------------------------------------------------ 路由

def build_dashboard_router(container: Container) -> APIRouter:
    router = APIRouter()
    cfg = container.config
    dash = cfg.dashboard
    resolver = MergeStateResolver(cfg, container.gitlab, dash.merge_cache_seconds)

    def basic_auth_guard(request: Request) -> None:
        """可选 HTTP Basic 鉴权：dashboard.username/password 同时非空才启用。"""
        if not (dash.username and dash.password):
            return
        header = request.headers.get("Authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except (ValueError, UnicodeDecodeError):
                user, password = "", ""
            ok = (secrets.compare_digest(user, dash.username)
                  and secrets.compare_digest(password, dash.password))
        if not ok:
            raise HTTPException(status_code=401,
                                headers={"WWW-Authenticate": "Basic realm=alarm-dashboard"})

    def _require_repo() -> dict | None:
        if container.repository is None:
            return _fail("未配置业务库(storage.database)，看板数据不可用")
        return None

    @router.get("/dashboard", include_in_schema=False, summary="Web 告警看板页面",
                dependencies=[Depends(basic_auth_guard)])
    def dashboard_page() -> HTMLResponse:
        if not TEMPLATE_PATH.is_file():
            return HTMLResponse("<h1>看板模板缺失</h1>"
                                "<p>app/web/templates/dashboard.html 不存在</p>", status_code=500)
        return HTMLResponse(TEMPLATE_PATH.read_text(encoding="utf-8"))

    @router.get("/api/dashboard/overview", summary="看板总览：KPI + 状态/归因分布 + 每日趋势",
                dependencies=[Depends(basic_auth_guard)])
    def overview(days: int = Query(14, ge=1, le=90), service: str = "",
                 system: str = "") -> dict:
        blocked = _require_repo()
        if blocked:
            return blocked
        try:
            return _ok(build_overview(container.repository, resolver, cfg.feishu.env,
                                      days, service.strip() or None,
                                      system_type=system.strip() or None))
        except Exception as exc:  # noqa: BLE001 - 只读看板：查询失败返回明确错误而非 500 堆栈
            log.exception("看板总览查询失败")
            return _fail(f"看板数据查询失败: {exc}")

    @router.get("/api/dashboard/alarms", summary="看板告警列表（分页 + 筛选，含已修复记录合并状态）",
                dependencies=[Depends(basic_auth_guard)])
    def alarms(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
               days: int = Query(0, ge=0, le=365, description="最近 N 天；0=不限"),
               service: str = "", status: str = "", q: str = "", system: str = "") -> dict:
        blocked = _require_repo()
        if blocked:
            return blocked
        start = window_start(days) if days else datetime(2000, 1, 1)
        end = datetime.now() + timedelta(minutes=1)
        try:
            records, total = container.repository.page_alarms(
                start, end, service=service.strip() or None,
                fix_status=status.strip() or None, keyword=q.strip() or None,
                offset=(page - 1) * size, limit=size,
                system_type=system.strip() or None)
        except Exception as exc:  # noqa: BLE001
            log.exception("看板告警列表查询失败")
            return _fail(f"看板数据查询失败: {exc}")
        items = []
        for record in records:
            item = _list_item(record, _content_of(record))
            if record.fix_status == "FIXED":
                # 走 resolver 缓存：总览扫描已覆盖近期记录，通常为缓存命中
                merge = resolver.resolve(record.service_name, record.trace_id,
                                         record.fix_time, record.fix_summary)
                item["merge"] = {"known": merge["known"], "merged": merge["merged"],
                                 "mr_state": merge["mr_state"]}
            items.append(item)
        return _ok({"total": total, "page": page, "size": size, "items": items})

    @router.get("/api/dashboard/alarms/{record_id}", summary="看板告警详情（分析内容 + 处理/合并状态）",
                dependencies=[Depends(basic_auth_guard)])
    def detail(record_id: int) -> dict:
        blocked = _require_repo()
        if blocked:
            return blocked
        try:
            record = container.repository.get_by_id(record_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("看板告警详情查询失败, id=%s", record_id)
            return _fail(f"看板数据查询失败: {exc}")
        if record is None:
            return _fail(f"未找到告警记录 id={record_id}")
        merge = None
        if record.fix_status == "FIXED":
            merge = resolver.resolve(record.service_name, record.trace_id,
                                     record.fix_time, record.fix_summary)
        return _ok({
            "id": record.id,
            "trace_id": record.trace_id,
            "service": record.service_name,
            "system_type": record.system_type,
            "interface": record.interface_uri,
            "create_time": record.create_time,
            "update_time": record.update_time,
            "fix_status": record.fix_status,
            "fix_label": _fix_status_label_of(record.fix_status),
            "fix_time": record.fix_time,
            "fix_summary": record.fix_summary,
            "problem_type": record.problem_type,
            "problem_label": problem_type_label(record.problem_type),
            "repeat_type": record.repeat_type,
            "root_id": record.root_id,
            "fingerprint": record.fingerprint,
            "analyze": _content_of(record),
            "merge": merge,
        })

    @router.get("/api/dashboard/services", summary="看板筛选项：服务名列表（可按系统分类过滤）",
                dependencies=[Depends(basic_auth_guard)])
    def services(system: str = "") -> dict:
        blocked = _require_repo()
        if blocked:
            return blocked
        try:
            return _ok(container.repository.list_services(system_type=system.strip() or None))
        except Exception as exc:  # noqa: BLE001
            log.exception("看板服务列表查询失败")
            return _fail(f"看板数据查询失败: {exc}")

    @router.get("/api/dashboard/systems", summary="看板筛选项：系统分类列表",
                dependencies=[Depends(basic_auth_guard)])
    def systems() -> dict:
        blocked = _require_repo()
        if blocked:
            return blocked
        try:
            return _ok(container.repository.list_system_types())
        except Exception as exc:  # noqa: BLE001
            log.exception("看板系统分类查询失败")
            return _fail(f"看板数据查询失败: {exc}")

    return router


__all__ = ["build_dashboard_router", "build_daily_series", "build_overview",
           "build_weekly_series", "parse_branch_name", "parse_commit_id", "parse_commit_url",
           "parse_mr_url", "resolve_rate", "window_start", "MergeStateResolver"]
