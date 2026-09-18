"""Web 告警看板测试：纯函数、合并状态解析器、HTTP API（假仓储全装配冒烟）。

不出网：GitLab 用假客户端，业务库用假仓储注入容器；鉴权、降级、KPI 口径均有断言。
"""
from __future__ import annotations

import textwrap
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.fixing.committer import build_branch_name
from app.main import create_app
from app.models.alarm import AlarmAnalyzeRecord
from app.web.dashboard import (
    MergeStateResolver,
    build_daily_series,
    build_overview,
    build_weekly_series,
    parse_commit_id,
    parse_commit_url,
    parse_mr_url,
    resolve_rate,
    window_start,
)

# ------------------------------------------------------------------ 纯函数


def test_parse_mr_url():
    summary = ("[AI] fix: 修复空指针\ncommit: abc1234\n"
               "查看改动: https://git.example/g/p/-/commit/abc1234\n"
               "MR: https://git.example/g/p/-/merge_requests/9")
    assert parse_mr_url(summary) == "https://git.example/g/p/-/merge_requests/9"
    assert parse_commit_url(summary) == "https://git.example/g/p/-/commit/abc1234"
    assert parse_commit_id(summary) == "abc1234"


def test_parse_fields_missing():
    assert parse_mr_url(None) is None
    assert parse_commit_id("只有一句话") is None
    assert parse_commit_url("") is None


def test_window_start_covers_today():
    now = datetime(2026, 9, 17, 15, 30)
    start = window_start(14, now)
    assert (start.year, start.month, start.day) == (2026, 9, 4)
    assert (start.hour, start.minute) == (0, 0)


def test_resolve_rate_excludes_not_needed():
    assert resolve_rate(total=54, fixed=8, not_needed=19) == 22.9
    assert resolve_rate(total=0, fixed=0, not_needed=0) is None
    assert resolve_rate(total=5, fixed=0, not_needed=5) is None  # 分母非正不展示


def test_resolve_rate_matches_daily_report_formula():
    # 口径与日报一致：成功 / (总数 − 无需处理 − 转人工 − 待人工 − 重复)
    assert resolve_rate(total=10, fixed=3, not_needed=2, manual=2, repeated=2) == 75.0
    assert resolve_rate(total=3, fixed=1, not_needed=1, manual=0, repeated=2) is None  # 分母 3−1−2=0


def test_build_daily_series_zero_filled():
    aggregates = {
        "daily": {"2026-09-16": {"total": 3, "fixed": 1, "repeated": 1,
                                 "not_needed": 1, "manual": 0}},
        "daily_fixed": {"2026-09-17": 2},
    }
    series = build_daily_series(datetime(2026, 9, 15), aggregates,
                                today=datetime(2026, 9, 17))
    assert [point["date"] for point in series] == ["2026-09-15", "2026-09-16", "2026-09-17"]
    assert series[0] == {"date": "2026-09-15", "total": 0, "fixed": 0, "fixed_by_time": 0,
                         "repeated": 0, "not_needed": 0, "manual": 0}
    assert series[1]["total"] == 3 and series[1]["repeated"] == 1
    assert series[1]["fixed"] == 1
    assert series[1]["not_needed"] == 1
    assert series[2]["fixed_by_time"] == 2


def test_overview_today_success_rate():
    """当日解决率：取今天创建的告警，口径与日报一致（剔除无需处理/转人工/待人工/重复）。"""
    repo = FakeRepository()
    data = build_overview(repo, MergeStateResolver(AppConfig(), None),
                          "test", 14, None, now=datetime(2026, 9, 17, 12, 0))
    # 假数据里 2026-09-17：新增 4，已解决 1，无需处理 1，重复 1 → 1/(4-1-0-1)=50%
    assert data["today"] == {"total": 4, "fixed": 1, "fixed_by_time": 1, "rate": 50.0}


def test_build_weekly_series_zero_filled():
    weekly = {date(2026, 9, 14).strftime("%G-W%V"): 9,  # Python 风格键（带 W 补零）
              "2026-37": 5}                             # MySQL 风格键（无 W 不补零）
    weekly_fixed = {"2026-37": 3}
    weekly_repeated = {"2026-37": 1}
    series = build_weekly_series(datetime(2026, 9, 18), weekly, weeks=12,
                                 weekly_fixed=weekly_fixed, weekly_repeated=weekly_repeated)
    assert len(series) == 12
    assert series[-1] == {"week": "2026-W38", "label": "09-14", "total": 9,
                          "fixed_by_time": 0, "repeated": 0}
    assert series[-2]["total"] == 5 and series[-2]["fixed_by_time"] == 3  # 两种键都能匹配
    assert series[-2]["repeated"] == 1
    assert series[0]["total"] == 0 and series[0]["label"] == "06-29"  # 11 周前的周一


# ------------------------------------------------------------------ 合并状态解析器


class FakeGitLab:
    def __init__(self, refs=None, mr=None):
        self.refs, self.mr = refs, mr
        self.refs_calls = 0

    def commit_branch_refs(self, project_id, sha):
        self.refs_calls += 1
        return self.refs

    def merge_request_by_source_branch(self, project_id, branch):
        return self.mr


def _resolver(refs, mr, base_branch="prod") -> tuple[MergeStateResolver, AppConfig]:
    config = AppConfig()
    config.analyse.git.base_url = "https://gitlab.example.com"
    config.analyse.git.token = "token"
    config.analyse.git.projects = {"svc": "42"}
    config.analyse.git.fix_base_branch = base_branch
    return MergeStateResolver(config, FakeGitLab(refs, mr), ttl_seconds=60), config


def _resolve(resolver, summary):
    fix_time = datetime(2026, 9, 17, 10, 0)
    return resolver.resolve("svc", "t-1", fix_time, summary)


def test_resolver_commit_in_base_branch_means_merged():
    resolver, _ = _resolver(refs=["prod", "fix_bug_20260917_t-1"], mr=None)
    info = _resolve(resolver, "x\ncommit: abc1234")
    assert info["known"] and info["merged"] and info["commit_merged"] is True
    assert info["branch"] == build_branch_name("t-1", datetime(2026, 9, 17, 10, 0))


def test_resolver_open_mr_means_not_merged():
    resolver, _ = _resolver(refs=["fix_bug_20260917_t-1"],
                            mr={"state": "opened", "web_url": "http://g/mr/1"})
    info = _resolve(resolver, "x\ncommit: abc1234")
    assert info["known"] and not info["merged"] and info["mr_state"] == "opened"


def test_resolver_squash_merged_via_mr_state():
    # squash merge 后原 commit 不在基线分支（refs 为空），靠 MR state=merged 兜底判定
    resolver, _ = _resolver(refs=[], mr={"state": "merged", "web_url": "http://g/mr/2"})
    info = _resolve(resolver, "x\ncommit: abc1234")
    assert info["known"] and info["merged"]


def test_resolver_confirmed_not_merged_without_mr():
    # 有 commit 且确认不在基线 + 确认无 MR ⇒ 可判定为未合并
    resolver, _ = _resolver(refs=["fix_bug_20260917_t-1"], mr={})
    info = _resolve(resolver, "x\ncommit: abc1234")
    assert info["known"] and not info["merged"] and info["mr_state"] is None


def test_resolver_unknown_when_gitlab_unreachable():
    resolver, _ = _resolver(refs=None, mr=None)
    info = _resolve(resolver, "x\ncommit: abc1234")
    assert not info["known"] and not info["merged"]


def test_resolver_degrades_without_git_config():
    resolver = MergeStateResolver(AppConfig(), None)
    info = _resolve(resolver, "x\ncommit: abc1234\nMR: http://g/mr/3")
    assert not info["known"]
    assert info["mr_url"] == "http://g/mr/3"  # fix_summary 里的链接兜底展示
    assert info["branch"].startswith("fix_bug_")


def test_resolver_caches_results():
    resolver, _ = _resolver(refs=["prod"], mr=None)
    _resolve(resolver, "x\ncommit: abc1234")
    _resolve(resolver, "x\ncommit: abc1234")
    assert resolver._gitlab.refs_calls == 1  # 同一 (project, branch, commit) 只查一次 GitLab


# ------------------------------------------------------------------ API（假仓储）


def _record(rid, trace_id, fix_status, summary=None, content=None, fix_time=None):
    return AlarmAnalyzeRecord(
        id=rid, trace_id=trace_id, analyze_content=content,
        service_name="svc-a", interface_uri="/api/x", create_time=datetime(2026, 9, 17, 9, 0),
        fix_status=fix_status, fix_summary=summary, fix_time=fix_time,
        problem_type="CODE" if fix_status else None, repeat_type="NORMAL",
    )


class FakeRepository:
    """与看板用到的 AlarmRepository 方法同形的假仓储。"""

    def __init__(self):
        self.fixed_summary = ("[AI] fix: 修复空指针\ncommit: abc1234\n"
                              "查看改动: https://git.example/p/-/commit/abc1234\n"
                              "MR: https://git.example/p/-/merge_requests/9")
        self.content = ('{"title":"数据库查询异常","level":"ERROR","analysis":"分析内容",'
                        '"proposal":"修复建议","service":"svc-a"}')

    def dashboard_aggregates(self, start, service=None, weekly_start=None, daily_start=None):
        assert service in (None, "svc-a")
        monday = date.today() - timedelta(days=date.today().weekday())  # 当前 ISO 周周一
        return {
            "fix_status": {"FIXED": 1, "NOT_NEEDED": 1, "FAILED": 1, "NULL": 1},
            "problem_type": {"CODE": 3, "NULL": 1},
            "repeat_type": {"NORMAL": 3, "REPEATED": 1},
            "daily": {"2026-09-17": {"total": 4, "fixed": 1, "repeated": 1,
                                     "not_needed": 1, "manual": 0}},
            "daily_fixed": {"2026-09-17": 1},
            "weekly": {monday.strftime("%G-W%V"): 4},
            "weekly_fixed": {monday.strftime("%G-W%V"): 1},
            "weekly_repeated": {monday.strftime("%G-W%V"): 1},
            "by_service": [{"service": "svc-a", "total": 4, "fixed": 1,
                            "pending": 1, "failed": 1}],
        }

    def page_alarms(self, start, end, service=None, fix_status=None, keyword=None,
                    offset=0, limit=20):
        records = [
            _record(1, "t-1", "FIXED", self.fixed_summary, self.content,
                    fix_time=datetime(2026, 9, 17, 10, 0)),
            _record(2, "t-2", "FAILED", "生成失败", self.content),
            _record(3, "t-3", None, None, self.content),
        ]
        if fix_status == "FIXED":
            records = records[:1]
        elif fix_status == "NULL":
            records = records[2:]
        if keyword:
            records = [r for r in records if keyword in (r.trace_id or "")]
        return records[offset:offset + limit], len(records)

    def get_by_id(self, record_id):
        if record_id != 1:
            return None
        return _record(1, "t-1", "FIXED", self.fixed_summary, self.content,
                       fix_time=datetime(2026, 9, 17, 10, 0))

    def list_services(self, limit=500):
        return ["svc-a", "svc-b"]

    def list_fixed_since(self, start, limit=50):
        return [_record(1, "t-1", "FIXED", self.fixed_summary, self.content,
                        fix_time=datetime(2026, 9, 17, 10, 0))]


_MIN_CONFIG = textwrap.dedent("""
    feishu:
      enable: false
    analyse:
      daily_report:
        enabled: false
    storage:
      redis_url: ""
      database:
        url: ""
    server:
      log_level: WARNING
""")

_AUTH_CONFIG = _MIN_CONFIG + textwrap.dedent("""
    dashboard:
      username: alice
      password: s3cret
""")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    config_path = tmp_path_factory.mktemp("cfg") / "config.yaml"
    config_path.write_text(_MIN_CONFIG, encoding="utf-8")
    import app.main as main_module

    app = create_app(str(config_path))
    main_module.container.repository = FakeRepository()  # 注入假仓储（路由闭包引用同一容器）
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def auth_client(tmp_path_factory):
    config_path = tmp_path_factory.mktemp("cfg_auth") / "config.yaml"
    config_path.write_text(_AUTH_CONFIG, encoding="utf-8")
    import app.main as main_module

    app = create_app(str(config_path))
    main_module.container.repository = FakeRepository()
    with TestClient(app) as test_client:
        yield test_client


def test_overview_kpi_math(client):
    data = client.get("/api/dashboard/overview", params={"days": 14}).json()["data"]
    kpi = data["kpi"]
    assert kpi["total"] == 4
    assert kpi["fixed"] == 1 and kpi["not_needed"] == 1 and kpi["failed"] == 1
    assert kpi["pending"] == 1 and kpi["manual"] == 0
    assert kpi["resolve_rate"] == 50.0  # 1 / (4 − 1 无需处理 − 1 重复)，口径与日报一致
    assert kpi["merge_checked"] == 0 and kpi["merge_rate"] is None  # 测试配置无 GitLab
    assert data["env"] == "test"
    seventeenth = next(p for p in data["daily"] if p["date"] == "2026-09-17")
    assert seventeenth["total"] == 4 and seventeenth["fixed_by_time"] == 1
    assert len(data["daily"]) == 30  # 日折线固定近 30 天（与 KPI 窗口解耦）
    assert len(data["weekly"]) == 12  # 近 12 周（零填充）
    monday = date.today() - timedelta(days=date.today().weekday())
    current_week = next(w for w in data["weekly"] if w["label"] == monday.strftime("%m-%d"))
    assert current_week["total"] == 4 and current_week["fixed_by_time"] == 1
    assert data["services"] == [{"service": "svc-a", "total": 4, "fixed": 1,
                                 "pending": 1, "failed": 1}]
    assert {"type": "REPEATED", "label": "重复", "count": 1} in data["repeat_distribution"]


def test_alarms_list_with_level_title_extracted(client):
    data = client.get("/api/dashboard/alarms", params={"page": 1, "size": 10}).json()["data"]
    assert data["total"] == 3
    first = data["items"][0]
    assert first["title"] == "数据库查询异常" and first["level"] == "ERROR"
    assert first["fix_label"] == "已修复"
    assert first["merge"] == {"known": False, "merged": False, "mr_state": None}


def test_alarms_filter_by_status(client):
    data = client.get("/api/dashboard/alarms", params={"status": "NULL"}).json()["data"]
    assert data["total"] == 1 and data["items"][0]["fix_status"] is None
    assert data["items"][0]["fix_label"] == "未处理"


def test_detail_contains_analysis_and_merge(client):
    body = client.get("/api/dashboard/alarms/1").json()
    assert body["code"] == 0
    data = body["data"]
    assert data["analyze"]["analysis"] == "分析内容"
    assert data["fix_summary"].startswith("[AI] fix:")
    assert data["merge"]["branch"].startswith("fix_bug_")


def test_detail_not_found(client):
    body = client.get("/api/dashboard/alarms/999").json()
    assert body["code"] == 500 and "未找到" in body["message"]


def test_services_list(client):
    assert client.get("/api/dashboard/services").json()["data"] == ["svc-a", "svc-b"]


def test_dashboard_page_served(client):
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "告警看板" in response.text and "text/html" in response.headers["content-type"]


def test_degraded_without_repository(tmp_path_factory):
    config_path = tmp_path_factory.mktemp("cfg_bare") / "config.yaml"
    config_path.write_text(_MIN_CONFIG, encoding="utf-8")
    app = create_app(str(config_path))  # 不注入仓储：storage.database 未配置
    with TestClient(app) as bare:
        body = bare.get("/api/dashboard/overview").json()
        assert body["code"] == 500 and "业务库" in body["message"]
        assert bare.get("/api/dashboard/alarms").json()["code"] == 500
        assert bare.get("/dashboard").status_code == 200  # 页面本身仍可访问


def test_basic_auth_enforced(auth_client):
    denied = auth_client.get("/api/dashboard/overview")
    assert denied.status_code == 401
    assert "WWW-Authenticate" in denied.headers
    assert auth_client.get("/dashboard").status_code == 401  # 页面同样受保护
    ok = auth_client.get("/api/dashboard/overview", auth=("alice", "s3cret"))
    assert ok.status_code == 200 and ok.json()["code"] == 0
    bad = auth_client.get("/api/dashboard/overview", auth=("alice", "wrong"))
    assert bad.status_code == 401
