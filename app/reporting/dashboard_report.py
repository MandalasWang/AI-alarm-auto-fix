"""飞书告警看板：统计本周/本月告警与修复数据，附日/周告警趋势两张折线与月告警分析，
经日报同一飞书卡片模板推送配置的飞书群。对应 Java 版 `AlarmDashboardReportService`。

与日报（daily_report，APScheduler 定时触发）的区别：仅展示周/月两期数据（无昨日），
并新增两张折线图与月告警分析（annaly 变量，即原日报「关键洞察」，基于本月明细由大模型生成、
失败降级规则洞察）。由 `GET /inner/feishu/alarm/dashboard/report` 按需触发（异步执行）；
接口被卡片「查看详情」按钮指向、按钮可被反复点击，故**每自然日仅放行一次推送**
（Redis 按日期幂等，推送失败释放锁允许当日重试）。

图表经模板图表（Chart）类型变量下发：变量值为完整 VChart spec（飞书卡片图表组件基于 VChart），
变量名 CHART_VAR_DAY/CHART_VAR_WEEK。需在看板模板中放两个图表组件并绑定这两个变量；
复用日报模板时（未配 dashboard.card_template_id）模板无图表组件，变量被忽略只渲染文本。
统计口径与推送逻辑在 stats.py（与日报共用）。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from app.config import AppConfig, DailyReportConfig
from app.core.text import truncate_utf8
from app.models.alarm import AlarmAnalyseResult
from app.providers.llm import LlmChatClient
from app.reporting import stats
from app.storage.alarm_repository import AlarmRepository

log = logging.getLogger(__name__)

#: 周告警趋势图覆盖的周数（含当前周）：近 12 周约一个季度，趋势可读且点不至于太密
WEEK_CHART_WEEKS = 12
#: 日告警趋势图覆盖的天数（截止昨天）：固定近 30 天窗口避免月初点位过少；不画当天——
#: 当天数据可能尚未统计完，尾点会假性走低误导趋势
DAY_CHART_DAYS = 30

#: 图表变量名：日告警趋势图（模板里绑定到图表组件）
CHART_VAR_DAY = "day_chart"
#: 图表变量名：周告警趋势图（模板里绑定到图表组件）
CHART_VAR_WEEK = "week_chart"
#: 详情链接变量名（模板 Url 类型变量，模板「查看详情」按钮用；dashboard.detail_url 配置后才输出）
DETAIL_URL_VAR = "detail_url"
#: 月告警分析变量名（模板文本变量，值为洞察分析，每条一行「- 」；源自原日报关键洞察）
ANNALY_VAR = "annaly"

#: 每日一次推送锁 key 前缀（key=北京日期，与 Java 版一致；TTL 仅作清理，跨日自然换 key 不冲突）
SEND_LOCK_KEY = "system:feishu:alarm:dashboard:send-lock:"
#: 推送锁 TTL（25 小时，仅作 key 清理）
_SEND_LOCK_TTL_SECONDS = 25 * 3600

INSIGHTS_SYSTEM_PROMPT = (
    "你是告警运维日报助手。根据给出的本期告警统计概览与告警明细，提炼关键洞察。\n"
    "输出要求：\n"
    "1. 输出 2~4 条，每条一行、以「- 」开头，只输出条目本身，不要任何前言、编号或总结。\n"
    "2. 内容优先级：① 告警集中的服务与失败原因归类（如数据库死锁、JSON 解析异常等，引用告警编号）；"
    "② 暂无修复通知/修复失败、需人工跟进的告警；③ 值得注意的趋势。\n"
    "3. 简体中文，每条不超过 80 字，只基于给定事实，不要臆造。"
)

#: 洞察单条字节预算（中文一字 3 字节，约 130 字）
INSIGHT_LINE_MAX_BYTES = 400
#: 喂给大模型的明细总长上限（字符），防止 prompt 超长
INSIGHTS_USER_CONTENT_MAX_LENGTH = 20_000
#: 明细里修复说明的字节预算
ROW_SUMMARY_MAX_BYTES = 600


class AlarmDashboardService:
    """告警看板服务：统计 + 两张折线 + 月告警分析，推送与每日一次幂等锁。"""

    def __init__(self, config: AppConfig, feishu, llm: LlmChatClient,
                 repository: AlarmRepository | None, store):
        self._config = config
        self._daily: DailyReportConfig = config.analyse.daily_report
        self._dashboard = config.analyse.dashboard
        self._feishu = feishu
        self._llm = llm
        self._repository = repository
        self._store = store

    # ------------------------------------------------------------------ 每日一次防重

    def try_acquire_daily_send_lock(self) -> bool:
        """尝试获取当日推送锁（HTTP 入口防重）：key 含日期，跨日自动放行；
        False=今日已推送过。"""
        key = SEND_LOCK_KEY + datetime.now(stats.ZONE).date().isoformat()
        return self._store.acquire_lock(key, "1", _SEND_LOCK_TTL_SECONDS)

    def release_daily_send_lock(self) -> None:
        """释放当日推送锁（异步推送失败时调用，允许当日重试；成功不释放保持当日幂等）。"""
        key = SEND_LOCK_KEY + datetime.now(stats.ZONE).date().isoformat()
        self._store.delete(key)

    # ------------------------------------------------------------------ 入口

    def send_dashboard_report(self) -> str | None:
        """统计并推送告警看板（HTTP 接口异步调用）。未配置业务库时跳过返回 None。"""
        if self._repository is None:
            log.warning("未配置业务库，告警看板无法统计，跳过推送")
            return None

        today = datetime.now(stats.ZONE).date()
        month_start = today.replace(day=1)
        week_start = stats.week_start(today)

        month_rows = self._list_by_range(month_start, stats.add_months(month_start, 1))
        # 日告警趋势：近 30 天（截止昨天），可能跨月故独立于本月区间查询
        day_rows = self._list_by_range(today - timedelta(days=DAY_CHART_DAYS), today)
        week_rows = self._list_by_range(week_start, week_start + timedelta(days=7))
        # 周告警趋势：一次查近 12 周（含本周），内存按周聚合，省去 12 次区间查询
        trend_rows = self._list_by_range(
            week_start - timedelta(weeks=WEEK_CHART_WEEKS - 1), week_start + timedelta(days=7))
        week_counts = count_by_week(trend_rows, week_start)

        # 月告警分析：原日报关键洞察（大模型优先，失败降级规则洞察），输出到模板 annaly 变量
        week_stats = stats.stats_of(week_rows)
        month_stats = stats.stats_of(month_rows)
        annaly = "\n".join(generate_insights(self._daily, self._llm,
                                             month_rows, month_stats, week_stats))

        content = build_content(self._daily, today, month_rows, month_stats, week_stats,
                                day_chart_summary(day_rows, today), week_counts)
        # 模板变量全量下发：文本 + 两张折线图（VChart spec）+ 详情链接 + 月告警分析。
        # 模板 ID 优先看板专属配置，空则回退日报模板
        template_id = (self._dashboard.card_template_id or "").strip() \
            or (self._daily.card_template_id or "").strip()
        variables: dict = {
            "content": content,
            CHART_VAR_DAY: day_chart_spec(day_rows, today),
            CHART_VAR_WEEK: week_chart_spec(week_counts),
            ANNALY_VAR: annaly,
        }
        # 详情链接：dashboard.detail_url 配置后输出 detail_url（Url 类型须传 {url, pc_url} 对象，
        # 传字符串报 200732）；未配置不输出该变量（模板可不加按钮）
        detail_url = (self._dashboard.detail_url or "").strip()
        if detail_url:
            variables[DETAIL_URL_VAR] = {"url": detail_url, "pc_url": detail_url}
        stats.send_to_chats(self._feishu, template_id,
                            stats.resolve_chat_ids(self._daily.chat_ids,
                                                   self._config.feishu.default_chat_id),
                            variables, content, "告警看板")
        log.info("告警看板统计完成, 本周=%s 条, 本月=%s 条, 洞察=%s 条",
                 len(week_rows), len(month_rows), 0 if not annaly else annaly.count("\n") + 1)
        return content

    # ------------------------------------------------------------------ 落库查询

    def _list_by_range(self, start: date, end: date):
        start_dt = datetime(start.year, start.month, start.day, tzinfo=stats.ZONE)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=stats.ZONE)
        return self._repository.list_between(start_dt, end_dt)


# ------------------------------------------------------------------ 文本组装

def build_content(daily: DailyReportConfig, today: date, month_rows, month_stats,
                  week_stats, day_summary: str, week_counts: dict) -> str:
    """头部 → 本周 → 本月 → 两张趋势图的数字摘要（折线图本体经图表变量渲染，此处兜底）→ 页脚。"""
    week_start = stats.week_start(today)
    bot_name = daily.bot_name or "归口业务小助手"

    lines = [
        f"📊 {bot_name} - 告警看板",
        f"⏰ 报告时间：{datetime.now(stats.ZONE):%Y-%m-%d %H:%M} 北京时间",
        stats.MAJOR_SEPARATOR,
        f"📅 本周（W{stats.week_number(today)}，"
        f"{week_start.month} 月 {week_start.day} 日～"
        f"{(week_start + timedelta(days=6)).month} 月 {(week_start + timedelta(days=6)).day} 日）",
        stats.stats_line(week_stats),
        stats.MINOR_SEPARATOR,
        f"📅 本月（{today.year} 年 {today.month} 月）" + stats.month_start_suffix(month_rows),
        stats.stats_line(month_stats),
        stats.MINOR_SEPARATOR,
        f"📈 日告警趋势图：{day_summary}",
        f"📈 周告警趋势图：{week_chart_summary(week_counts)}",
        "",
        stats.MAJOR_SEPARATOR,
        f"🤖 {bot_name}"
        + (f" | ⏰ 入群：{daily.bot_join_time.strip()}" if daily.bot_join_time else ""),
        f"📡 数据源：{daily.data_source_name or '-'}",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ VChart 折线 spec

def day_chart_spec(day_rows, today: date) -> dict:
    """日告警趋势图（图表变量）：近 30 天逐日告警数 VChart 折线 spec，截止昨天。"""
    start = today - timedelta(days=DAY_CHART_DAYS)
    counts = day_counts(day_rows, start)
    x_values = [(start + timedelta(days=i)).strftime("%m-%d") for i in range(DAY_CHART_DAYS)]
    return line_chart_spec("日告警趋势图", x_values, counts)


def week_chart_spec(week_counts: dict) -> dict:
    """周告警趋势图（图表变量）：近 12 周（含本周）逐周告警数 VChart 折线 spec，周一日期为横轴标签。"""
    x_values = [week_start.strftime("%m-%d") for week_start in week_counts]
    counts = [week_counts[week_start] or 0 for week_start in week_counts]
    return line_chart_spec("周告警趋势图", x_values, counts)


def line_chart_spec(title: str, x_values: list[str], y_values: list[int]) -> dict:
    """VChart 折线图 spec（飞书图表组件 chart_spec/图表变量格式，参考官方折线图模板）。

    字段名用官方示例的 time/value；curveType=monotoneX 平滑曲线且不过冲。
    """
    return {
        "type": "line",
        "title": {"text": title},
        "data": {"values": [{"time": x, "value": y} for x, y in zip(x_values, y_values)]},
        "xField": "time",
        "yField": "value",
        "line": {"style": {"curveType": "monotoneX"}},
    }


def day_counts(rows, start: date) -> list[int]:
    """逐日计数：近 30 天窗口 [start, start+30)，按行的日期归属累加。"""
    counts = [0] * DAY_CHART_DAYS
    for row in rows:
        if row.create_time is None:
            continue
        index = (row.create_time.replace(tzinfo=stats.ZONE).date() - start).days
        if 0 <= index < DAY_CHART_DAYS:
            counts[index] += 1
    return counts


def count_by_week(rows, current_week_start: date) -> dict:
    """逐周计数表：预置近 12 周（老→新）全 0 桶，再按行的周一归属累加，保证折线点位稳定。"""
    counts: dict[date, int] = {}
    for i in range(WEEK_CHART_WEEKS - 1, -1, -1):
        counts[current_week_start - timedelta(weeks=i)] = 0
    for row in rows:
        if row.create_time is None:
            continue
        row_week = stats.week_start(row.create_time.replace(tzinfo=stats.ZONE).date())
        if row_week in counts:
            counts[row_week] += 1
    return counts


# ------------------------------------------------------------------ 数字摘要

def day_chart_summary(day_rows, today: date) -> str:
    """日告警趋势数字摘要：峰值 MM-dd（N 条）・日均 X.X 条；无告警返回「近 N 天暂无告警」。"""
    start = today - timedelta(days=DAY_CHART_DAYS)
    counts = day_counts(day_rows, start)
    total = sum(counts)
    if total == 0:
        return f"近 {DAY_CHART_DAYS} 天暂无告警"
    peak_index = max(range(DAY_CHART_DAYS), key=lambda i: counts[i])
    peak_day = start + timedelta(days=peak_index)
    return (f"峰值 {peak_day:%m-%d}（{counts[peak_index]} 条）・日均 {total / DAY_CHART_DAYS:.1f} 条")


def week_chart_summary(week_counts: dict) -> str:
    """周告警趋势数字摘要：逐周数量标注行（周一日期为标签）；全 0 返回「近 N 周暂无告警」。"""
    total = sum(week_counts.values())
    if total == 0:
        return f"近 {WEEK_CHART_WEEKS} 周暂无告警"
    return "  ".join(f"{week_start:%m-%d}={count}" for week_start, count in week_counts.items())


# ------------------------------------------------------------------ 月告警分析（原日报关键洞察）

def generate_insights(daily: DailyReportConfig, llm: LlmChatClient,
                      month_rows, month_stats: stats.PeriodStats,
                      week_stats: stats.PeriodStats) -> list[str]:
    """大模型优先（未启用/失败/输出为空时降级规则洞察）。"""
    max_items = max(1, daily.insight_max_items)
    if daily.insights_llm_enabled and month_rows and llm.is_available():
        try:
            text = llm.chat(INSIGHTS_SYSTEM_PROMPT,
                            build_insights_user_content(month_rows, month_stats, week_stats))
            lines = parse_insight_lines(text, max_items)
            if lines:
                log.info("看板月告警分析大模型生成成功, lines=%s", len(lines))
                return lines
        except Exception as exc:  # noqa: BLE001 - 降级规则洞察
            log.warning("看板月告警分析大模型生成失败，降级规则洞察: %s", exc)
    return rule_based_insights(month_rows, max_items)


def build_insights_user_content(month_rows, month_stats: stats.PeriodStats,
                                week_stats: stats.PeriodStats) -> str:
    def overview(name: str, item: stats.PeriodStats) -> str:
        return (f"{name}：错误告警 {item.total} 条，修复通知 {item.fix_notified} 条，"
                f"成功 {item.fixed} 条，无需修复 {item.not_needed} 条，失败 {item.failed} 条，"
                f"重复 {item.repeated} 条，复发 {item.recurred} 条，"
                f"转人工 {item.handover} 条，待人工 {item.manual_pending} 条")

    lines = [
        "【统计概览】",
        overview("本月", month_stats),
        overview("本周", week_stats),
        "",
        "【本月告警明细】（时间 | 编号 | 服务 | 接口 | 修复状态 | 异常 | 说明）",
    ]
    for row in month_rows:
        # 库里存的是北京时间墙钟（naive），显式按北京时区解读，避免部署机时区不同导致偏移
        row_time = row.create_time.replace(tzinfo=stats.ZONE).strftime("%m-%d %H:%M") if row.create_time else "-"
        line = (f"{row_time} | [{stats.short_trace_id(row.trace_id)}]"
                f" | {row.service_name or '-'} | {row.interface_uri or '-'}"
                f" | {stats.fix_status_label(row.fix_status)}{stats.repeat_marker(row.repeat_type)}")
        exception_class = stats.exception_class_of(row)
        if exception_class:
            line += f" | {exception_class}"
        if row.fix_summary:
            line += f" | {truncate_utf8(row.fix_summary, ROW_SUMMARY_MAX_BYTES)}"
        lines.append(line)
    text = "\n".join(lines)
    if len(text) > INSIGHTS_USER_CONTENT_MAX_LENGTH:
        text = text[:INSIGHTS_USER_CONTENT_MAX_LENGTH] + "\n...(内容过长已截断)"
    return text


def parse_insight_lines(text: str, max_items: int) -> list[str]:
    """去掉列表符号、去空行、限量、按字节预算截断，统一「- 」前缀。"""
    lines: list[str] = []
    if not text:
        return lines
    for raw in text.split("\n"):
        line = re.sub(r"^[-•·*]\s*", "", raw.strip()).strip()
        if not line:
            continue
        lines.append("- " + (truncate_utf8(line, INSIGHT_LINE_MAX_BYTES) or line))
        if len(lines) >= max_items:
            break
    return lines


def rule_based_insights(month_rows, max_items: int) -> list[str]:
    """规则洞察：服务集中度 + 未闭环告警跟进提示。"""
    insights: list[str] = []
    if not month_rows:
        return ["- 本期无告警记录，各服务运行平稳。"]
    by_service: dict[str, int] = {}
    for row in month_rows:
        if row.service_name:
            by_service[row.service_name] = by_service.get(row.service_name, 0) + 1
    if by_service:
        top_service, count = max(by_service.items(), key=lambda item: item[1])
        if count >= 2:
            insights.append(
                f"- 告警主要集中在 {top_service} 服务（{count}/{len(month_rows)} 条），建议重点关注。")
    manual_ids = "/".join(
        stats.short_trace_id(row.trace_id)
        for row in month_rows
        if row.fix_status in ("HANDOVER", "MANUAL_PENDING")
    )
    if manual_ids:
        insights.append(f"- 以下告警需人工处理（依赖排查/SQL/配置），建议跟进闭环：{manual_ids}。")

    open_ids = "/".join(
        stats.short_trace_id(row.trace_id)
        for row in month_rows
        if row.fix_status is None or row.fix_status == "FAILED"
    )
    if open_ids:
        insights.append(f"- 以下告警未完成自动修复（未发起或失败），建议人工跟进：{open_ids}。")
    elif not manual_ids:
        insights.append("- 本期告警均已闭环（已修复或判定无需处理），无待跟进事项。")
    return insights[:max_items]


__all__ = ["AlarmDashboardService", "ANNALY_VAR", "CHART_VAR_DAY", "CHART_VAR_WEEK",
           "DETAIL_URL_VAR", "DAY_CHART_DAYS", "WEEK_CHART_WEEKS", "build_content",
           "day_chart_spec", "week_chart_spec", "line_chart_spec"]
