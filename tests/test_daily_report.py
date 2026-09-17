"""日报统计口径测试（与 Java 版数字语义一致）。"""
from datetime import date, datetime

from app.models.alarm import AlarmAnalyzeRecord
from app.reporting.daily_report import (_exception_class_of, _fix_status_label,
                                        _month_start_suffix, _repeat_marker,
                                        _short_trace_id, _stats_line, _stats_of,
                                        _week_number)


def _row(fix_status: str | None, analyze_content: str | None = None,
         repeat_type: str | None = None) -> AlarmAnalyzeRecord:
    return AlarmAnalyzeRecord(fix_status=fix_status, analyze_content=analyze_content,
                              repeat_type=repeat_type,
                              create_time=datetime(2026, 9, 2, 10, 0))


def test_stats_success_rate_excludes_not_needed():
    # 成功率 = 成功/(错误告警-无需处理-转人工-待人工-重复) = 2/(4-1) = 66.7%
    rows = [_row("FIXED"), _row("FIXED"), _row("NOT_NEEDED"), _row("FAILED")]
    stats = _stats_of(rows)
    assert stats.total == 4
    assert stats.fix_notified == 4
    assert stats.fixed == 2
    assert stats.failed == 1
    assert stats.not_needed == 1
    assert abs(stats.success_rate - 66.666) < 0.01


def test_stats_counts_new_categories():
    # 8 条 = 2 成功 + 1 失败 + 1 无需 + 1 转人工 + 1 待人工 + 1 重复(NULL) + 1 复发(FIXED) + 1 未发起
    rows = [_row("FIXED"), _row("FIXED", repeat_type="RECURRED"), _row("FAILED"),
            _row("NOT_NEEDED"), _row("HANDOVER"), _row("MANUAL_PENDING"),
            _row(None, repeat_type="REPEATED"), _row(None)]
    stats = _stats_of(rows)
    assert stats.handover == 1
    assert stats.manual_pending == 1
    assert stats.repeated == 1
    assert stats.recurred == 1
    assert stats.fix_notified == 6
    # 分母剔除 无需/转人工/待人工/重复 → 2/(8-1-1-1-1) = 50%
    assert abs(stats.success_rate - 50.0) < 0.01


def test_stats_zero_when_no_alarm():
    stats = _stats_of([])
    assert stats.total == 0 and stats.success_rate == 0
    assert _stats_line(stats) == "🔴 错误告警 0 条"


def test_stats_all_not_needed_rate_100():
    # 全部无需处理 = 零失败，记 100（记 0 会被误读为全部修复失败）
    stats = _stats_of([_row("NOT_NEEDED"), _row("NOT_NEEDED")])
    assert stats.success_rate == 100


def test_stats_all_handover_rate_100():
    stats = _stats_of([_row("HANDOVER"), _row("MANUAL_PENDING")])
    assert stats.success_rate == 100


def test_stats_repeated_excluded_from_denominator():
    # 1 成功 + 2 重复 + 1 未发起 → 1/(4-2) = 50%，而非被刷屏稀释成 25%
    rows = [_row("FIXED"), _row(None, repeat_type="REPEATED"),
            _row(None, repeat_type="REPEATED"), _row(None)]
    stats = _stats_of(rows)
    assert stats.repeated == 2
    assert abs(stats.success_rate - 50.0) < 0.01


def test_stats_line_contains_all_sections():
    line = _stats_line(_stats_of([_row("FIXED")]))
    for token in ("错误告警 1 条", "修复通知 1 条", "成功 1 条", "成功率 100.0%"):
        assert token in line
    # 普通期（无重复/复发/转人工等）单行
    assert "\n" not in line


def test_stats_line_extra_segments_only_nonzero():
    rows = [_row("FIXED"), _row("HANDOVER"), _row(None, repeat_type="REPEATED"),
            _row(None, repeat_type="RECURRED")]
    line = _stats_line(_stats_of(rows))
    first, extra = line.split("\n")
    assert "错误告警 4 条" in first and "成功 1 条" in first and "成功率 50.0%" in first
    assert "🚫" not in extra and "⏳" not in extra
    assert "🔁 重复 1 条・↩️ 复发 1 条・👥 转人工 1 条" == extra


def test_month_start_suffix():
    early = AlarmAnalyzeRecord(create_time=datetime(2026, 9, 1, 8, 0))
    late = AlarmAnalyzeRecord(create_time=datetime(2026, 9, 2, 8, 0))
    assert _month_start_suffix([early]) == ""
    assert _month_start_suffix([late]) == "，从 09-02 起算"
    assert _month_start_suffix([]) == ""


def test_short_trace_id():
    assert _short_trace_id("ccd-design-project-5290") == "5290"
    assert _short_trace_id("plain") == "plain"
    assert _short_trace_id(None) == "-"


def test_fix_status_label():
    assert _fix_status_label(None) == "暂无修复通知"
    assert _fix_status_label("FIXED") == "已修复"
    assert _fix_status_label("NOT_NEEDED") == "无需修复"
    assert _fix_status_label("FAILED") == "失败"
    assert _fix_status_label("HANDOVER") == "已转人工"
    assert _fix_status_label("MANUAL_PENDING") == "待人工处理"


def test_repeat_marker():
    assert _repeat_marker("REPEATED") == "（重复）"
    assert _repeat_marker("RECURRED") == "（复发）"
    assert _repeat_marker(None) == ""
    assert _repeat_marker("NORMAL") == ""


def test_week_number_iso():
    # 2026-09-10 是 ISO 第 37 周（与 Java 版 WeekFields.ISO 一致）
    assert _week_number(date(2026, 9, 10)) == 37


def test_exception_class_from_json():
    content = '{"exceptionClass":"java.lang.NPE","service":"a"}'
    assert _exception_class_of(_row(None, content)) == "java.lang.NPE"
    assert _exception_class_of(_row(None, "not-json")) is None
    assert _exception_class_of(_row(None, None)) is None
