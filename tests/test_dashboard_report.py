"""告警看板测试：日/周趋势折线 spec（VChart）、月告警分析、每日一次锁、日报精简与 detail_url。

对齐 Java 版 AlarmDashboardReportService / AlarmDailyReportService 的行为契约。
不出网：飞书/LLM/仓储/Redis(KV) 全部用假实现。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.config import AppConfig
from app.core.kv import MemoryStore
from app.models.alarm import AlarmAnalyzeRecord
from app.reporting import stats
from app.reporting.dashboard_report import (ANNALY_VAR, CHART_VAR_DAY, CHART_VAR_WEEK,
                                            DAY_CHART_DAYS, DETAIL_URL_VAR,
                                            AlarmDashboardService, build_content,
                                            count_by_week, day_chart_spec,
                                            day_chart_summary, line_chart_spec,
                                            parse_insight_lines, week_chart_spec,
                                            week_chart_summary)
from app.reporting.daily_report import DailyReportService

TODAY = date(2026, 9, 17)  # 周四


def _row(day: date, fix_status: str | None = "FIXED") -> AlarmAnalyzeRecord:
    return AlarmAnalyzeRecord(fix_status=fix_status,
                              create_time=datetime(day.year, day.month, day.day, 10, 0))


# ------------------------------------------------------------------ 日告警趋势图

def test_day_chart_spec_window_ends_yesterday():
    # 近 30 天、截止昨天：today=09-17 → 窗口 08-18~09-16（30 个点，不含当天）
    rows = [_row(date(2026, 9, 2)), _row(date(2026, 9, 2)), _row(date(2026, 9, 6)),
            _row(date(2026, 9, 6)), _row(date(2026, 9, 6)), _row(date(2026, 9, 10)),
            _row(date(2026, 9, 15)), _row(date(2026, 9, 17))]  # 09-17 当天不画
    spec = day_chart_spec(rows, TODAY)
    assert spec["type"] == "line"
    assert spec["xField"] == "time" and spec["yField"] == "value"
    assert spec["line"]["style"]["curveType"] == "monotoneX"
    assert spec["title"]["text"] == "日告警趋势图"
    values = spec["data"]["values"]
    assert len(values) == DAY_CHART_DAYS == 30
    assert values[0]["time"] == "08-18" and values[-1]["time"] == "09-16"
    assert sum(point["value"] for point in values) == 7  # 当天 1 条被排除
    assert values[19] == {"time": "09-06", "value": 3}


def test_day_chart_summary_and_empty():
    rows = [_row(date(2026, 9, 6)), _row(date(2026, 9, 6)), _row(date(2026, 9, 6))]
    summary = day_chart_summary(rows, TODAY)
    assert summary == "峰值 09-06（3 条）・日均 0.1 条"
    assert day_chart_summary([], TODAY) == "近 30 天暂无告警"


# ------------------------------------------------------------------ 周告警趋势图

def test_week_chart_spec_and_count_by_week():
    week_start = stats.week_start(TODAY)  # 2026-09-14（周一）
    rows = [_row(week_start), _row(week_start + timedelta(days=1)),
            _row(week_start - timedelta(weeks=1))]
    counts = count_by_week(rows, week_start)
    assert len(counts) == 12
    items = list(counts.items())
    assert items[-1][1] == 2 and items[-2][1] == 1 and items[-3][1] == 0
    spec = week_chart_spec(counts)
    assert spec["title"]["text"] == "周告警趋势图"
    assert spec["data"]["values"][-1] == {"time": "09-14", "value": 2}


def test_line_chart_spec_shape():
    spec = line_chart_spec("t", ["09-01", "09-02"], [0, 5])
    assert spec == {"type": "line", "title": {"text": "t"},
                    "data": {"values": [{"time": "09-01", "value": 0},
                                        {"time": "09-02", "value": 5}]},
                    "xField": "time", "yField": "value",
                    "line": {"style": {"curveType": "monotoneX"}}}


def test_week_chart_summary():
    week_start = stats.week_start(TODAY)
    counts = count_by_week([], week_start)
    assert week_chart_summary(counts) == "近 12 周暂无告警"
    counts[week_start] = 4
    summary = week_chart_summary(counts)
    assert summary.startswith("06-29=0") and summary.endswith("09-14=4")  # 12 桶全列出（含零桶）


# ------------------------------------------------------------------ 看板文本与变量

def _fake_repository(rows):
    class _Repo:
        def list_between(self, start, end):
            # 与真实存储约定一致：库里是 naive 北京墙钟，边界转 naive 再比较
            return [row for row in rows
                    if start.replace(tzinfo=None) <= row.create_time < end.replace(tzinfo=None)]
    return _Repo()


class _FakeFeishu:
    def __init__(self):
        self.cards: list[tuple[str, dict]] = []

    def send_card_to_chat(self, chat_id, card):
        self.cards.append((chat_id, card))

    def send_text_to_chat(self, chat_id, content):  # pragma: no cover - 模板已配置不走文本
        raise AssertionError("模板已配置不应走文本兜底")


class _FakeLlm:
    def is_available(self):
        return False  # 规则洞察路径，测试确定性


def _make_service(rows, detail_url="", template_id="TPL_DASH"):
    config = AppConfig()
    config.analyse.daily_report.chat_ids = ["oc_test"]
    config.analyse.daily_report.card_template_id = "TPL_DAILY"
    config.analyse.dashboard.card_template_id = template_id
    config.analyse.dashboard.detail_url = detail_url
    feishu = _FakeFeishu()
    service = AlarmDashboardService(config, feishu, _FakeLlm(),
                                    _fake_repository(rows), MemoryStore())
    return config, feishu, service


def test_dashboard_send_variables():
    rows = [_row(date(2026, 9, 6)), _row(date(2026, 9, 10))]
    _, feishu, service = _make_service(rows, detail_url="http://host/dashboard")
    content = service.send_dashboard_report()
    assert content and "📊 归口业务小助手 - 告警看板" in content
    assert "📈 日告警趋势图" in content and "📈 周告警趋势图" in content
    chat_id, card = feishu.cards[0]
    assert chat_id == "oc_test"
    data = card["data"]
    assert data["template_id"] == "TPL_DASH"
    variables = data["template_variable"]
    assert variables["content"] == content
    assert variables[CHART_VAR_DAY]["title"]["text"] == "日告警趋势图"
    assert variables[CHART_VAR_WEEK]["title"]["text"] == "周告警趋势图"
    assert variables[DETAIL_URL_VAR] == {"url": "http://host/dashboard",
                                         "pc_url": "http://host/dashboard"}
    # LLM 不可用 → 规则洞察兜底，annaly 非空且以「- 」条目组成
    annaly_lines = variables[ANNALY_VAR].split("\n")
    assert annaly_lines and all(line.startswith("- ") for line in annaly_lines)


def test_dashboard_detail_url_absent_when_unconfigured():
    _, feishu, service = _make_service([])
    service.send_dashboard_report()
    variables = feishu.cards[0][1]["data"]["template_variable"]
    assert DETAIL_URL_VAR not in variables
    assert ANNALY_VAR in variables  # 无告警也有规则洞察文案


def test_dashboard_template_fallback_to_daily():
    config, feishu, _ = _make_service([], template_id="")
    config.analyse.dashboard.card_template_id = ""  # 空则回退日报模板
    service = AlarmDashboardService(config, feishu, _FakeLlm(),
                                    _fake_repository([]), MemoryStore())
    service.send_dashboard_report()
    assert feishu.cards[0][1]["data"]["template_id"] == "TPL_DAILY"


def test_dashboard_daily_send_lock():
    _, _, service = _make_service([])
    store = service._store
    assert service.try_acquire_daily_send_lock() is True
    assert service.try_acquire_daily_send_lock() is False  # 当日幂等
    service.release_daily_send_lock()
    assert service.try_acquire_daily_send_lock() is True  # 失败释放后可重试
    assert store  # MemoryStore 注入生效


# ------------------------------------------------------------------ 日报精简 + detail_url

def test_daily_report_content_yesterday_only():
    config = AppConfig()
    config.analyse.daily_report.bot_join_time = "2026-08-31 15:18"
    service = DailyReportService(config, _FakeFeishu(), _FakeLlm(), None)
    today = date(2026, 9, 17)
    yesterday_rows = [_row(today - timedelta(days=1)), _row(today - timedelta(days=1),
                                                           fix_status=None)]
    content = service._build_report_content(today, yesterday_rows)
    assert "📅 昨日（9 月 16 日）" in content
    assert "错误告警 2 条" in content
    # 本周/本月统计与关键洞察均已移除（洞察移交看板 annaly）
    assert "本周" not in content and "本月" not in content and "关键洞察" not in content


def test_daily_report_send_includes_detail_url():
    config = AppConfig()
    config.analyse.daily_report.chat_ids = ["oc_test"]
    config.analyse.daily_report.card_template_id = "TPL_DAILY"
    config.analyse.dashboard.detail_url = "http://host/dashboard"
    feishu = _FakeFeishu()
    today = datetime.now(stats.ZONE).date()
    rows = [_row(today - timedelta(days=1))]

    class _Repo:
        def list_between(self, start, end):
            return rows

    service = DailyReportService(config, feishu, _FakeLlm(), _Repo())
    content = service.send_daily_report()
    assert content and "告警日报" in content
    _, card = feishu.cards[0]
    variables = card["data"]["template_variable"]
    assert variables["content"] == content
    assert variables["detail_url"] == {"url": "http://host/dashboard",
                                       "pc_url": "http://host/dashboard"}


# ------------------------------------------------------------------ 洞察解析

def test_parse_insight_lines():
    text = "- 第一条\n* 第二条\n\n普通行\n- 第三条"
    lines = parse_insight_lines(text, 4)
    assert lines == ["- 第一条", "- 第二条", "- 普通行", "- 第三条"]
    assert parse_insight_lines("", 4) == []
