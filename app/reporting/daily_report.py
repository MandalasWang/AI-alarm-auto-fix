"""飞书告警日报：统计昨日/本周/本月告警数据，组装日报文本，按模板卡片推送到配置的飞书群。

调度由本服务自带（APScheduler，替代 Java 版 xxl-job），默认每天 09:40（北京时间）。
统计口径：错误告警=区间内 alarm_analyze_result 记录数；修复通知=fix_status 已判定数（含转人工/待人工）；
修复成功率=成功/(错误告警-无需处理-转人工-待人工-重复)，非自动修复项剔除出分母（重复剔除防刷屏稀释），
分母为 0 且有告警记 100%，无告警记 0% 不展示；重复/复发按 repeat_type 单独计数。
关键洞察优先大模型生成（复用 analyse.llm 配置），失败降级规则洞察；单群发送失败不阻断其他群。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import AppConfig, DailyReportConfig
from app.core.fingerprint import REPEAT_RECURRED, REPEAT_REPEATED
from app.core.text import truncate_utf8
from app.feishu.cards import build_template_card
from app.feishu.client import FeishuClient
from app.models.alarm import AlarmAnalyseResult
from app.providers.llm import LlmChatClient
from app.storage.alarm_repository import AlarmRepository

log = logging.getLogger(__name__)

#: 报表统一按北京时间取日期边界
ZONE = ZoneInfo("Asia/Shanghai")

_MAJOR_SEPARATOR = "━━━━━━━━━━━━━━━"
_MINOR_SEPARATOR = "———————"

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
#: 喂给大模型的明细总长上限（字符），防止日报 prompt 超长
INSIGHTS_USER_CONTENT_MAX_LENGTH = 20_000
#: 明细里修复说明的字节预算
ROW_SUMMARY_MAX_BYTES = 600


@dataclass
class PeriodStats:
    """告警周期统计。修复成功率 = 成功/(错误告警-无需处理-转人工-待人工-重复)，非自动修复项剔除出分母。"""

    total: int = 0
    fix_notified: int = 0
    fixed: int = 0
    failed: int = 0
    not_needed: int = 0
    handover: int = 0        # 已转人工排查数（上下游依赖问题）
    manual_pending: int = 0  # 待人工处理数（SQL/配置等需人工介入）
    repeated: int = 0        # 重复告警数（窗口内同指纹，已压卡片）
    recurred: int = 0        # 修复后复发数
    success_rate: float = 0.0


class DailyReportService:
    def __init__(self, config: AppConfig, feishu: FeishuClient, llm: LlmChatClient,
                 repository: AlarmRepository | None):
        self._config = config
        self._daily: DailyReportConfig = config.analyse.daily_report
        self._feishu = feishu
        self._llm = llm
        self._repository = repository

    # ------------------------------------------------------------------ 入口

    def send_daily_report(self) -> str | None:
        """统计并推送告警日报（定时任务调用）。日报开关关闭时返回 None。"""
        if not self._daily.enabled:
            log.info("告警日报开关已关闭(analyse.daily-report.enabled=false)，跳过推送")
            return None
        if self._repository is None:
            log.warning("未配置业务库，告警日报无法统计，跳过推送")
            return None

        today = datetime.now(ZONE).date()
        month_rows = self._list_by_range(
            today.replace(day=1), _add_months(today.replace(day=1), 1))
        week_rows = self._list_by_range(_week_start(today), _week_start(today) + timedelta(days=7))
        yesterday_rows = self._list_by_range(today - timedelta(days=1), today)

        content = self._build_report_content(today, month_rows, week_rows, yesterday_rows)
        self._send_to_chats(content)
        return content

    # ------------------------------------------------------------------ 文本组装

    def _build_report_content(self, today: date, month_rows, week_rows, yesterday_rows) -> str:
        yesterday_stats = _stats_of(yesterday_rows)
        week_stats = _stats_of(week_rows)
        month_stats = _stats_of(month_rows)

        yesterday = today - timedelta(days=1)
        week_start = _week_start(today)
        week_end = week_start + timedelta(days=6)
        bot_name = self._daily.bot_name or "归口业务小助手"

        # 紧凑排版：全部单换行，飞书文本消息里空行会显得很稀疏
        lines = [
            f"📊 {bot_name} - 告警日报",
            f"⏰ 报告时间：{datetime.now(ZONE):%Y-%m-%d %H:%M} 北京时间",
            _MAJOR_SEPARATOR,
            f"📅 昨日（{yesterday.month} 月 {yesterday.day} 日）",
            _stats_line(yesterday_stats),
            _MINOR_SEPARATOR,
            f"📅 本周（W{_week_number(today)}，"
            f"{week_start.month} 月 {week_start.day} 日～{week_end.month} 月 {week_end.day} 日）",
            _stats_line(week_stats),
            _MINOR_SEPARATOR,
            f"📅 本月（{today.year} 年 {today.month} 月）" + _month_start_suffix(month_rows),
            _stats_line(month_stats),
            "",
            _MAJOR_SEPARATOR,
            "💡 关键洞察",
        ]
        lines.extend(self._generate_insights(month_rows, month_stats, week_stats))
        lines += [
            "",
            _MAJOR_SEPARATOR,
            f"🤖 {bot_name}"
            + (f" | ⏰ 入群：{self._daily.bot_join_time.strip()}" if self._daily.bot_join_time else ""),
            f"📡 数据源：{self._daily.data_source_name or '-'}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ 洞察

    def _generate_insights(self, month_rows, month_stats: PeriodStats,
                           week_stats: PeriodStats) -> list[str]:
        max_items = max(1, self._daily.insight_max_items)
        if (self._daily.insights_llm_enabled and month_rows and self._llm.is_available()):
            try:
                text = self._llm.chat(INSIGHTS_SYSTEM_PROMPT,
                                      self._build_insights_user_content(month_rows, month_stats, week_stats))
                lines = self._parse_insight_lines(text, max_items)
                if lines:
                    log.info("日报关键洞察大模型生成成功, lines=%s", len(lines))
                    return lines
            except Exception as exc:  # noqa: BLE001 - 降级规则洞察
                log.warning("日报关键洞察大模型生成失败，降级规则洞察: %s", exc)
        return self._rule_based_insights(month_rows, max_items)

    @staticmethod
    def _build_insights_user_content(month_rows, month_stats: PeriodStats,
                                     week_stats: PeriodStats) -> str:
        def overview(name: str, stats: PeriodStats) -> str:
            return (f"{name}：错误告警 {stats.total} 条，修复通知 {stats.fix_notified} 条，"
                    f"成功 {stats.fixed} 条，无需修复 {stats.not_needed} 条，失败 {stats.failed} 条，"
                    f"重复 {stats.repeated} 条，复发 {stats.recurred} 条，"
                    f"转人工 {stats.handover} 条，待人工 {stats.manual_pending} 条")

        lines = [
            "【统计概览】",
            overview("本月", month_stats),
            overview("本周", week_stats),
            "",
            "【本月告警明细】（时间 | 编号 | 服务 | 接口 | 修复状态 | 异常 | 说明）",
        ]
        for row in month_rows:
            # 库里存的是北京时间墙钟（naive），显式按北京时区解读，避免部署机时区不同导致偏移
            row_time = row.create_time.replace(tzinfo=ZONE).strftime("%m-%d %H:%M") if row.create_time else "-"
            line = (f"{row_time} | [{_short_trace_id(row.trace_id)}]"
                    f" | {row.service_name or '-'} | {row.interface_uri or '-'}"
                    f" | {_fix_status_label(row.fix_status)}{_repeat_marker(row.repeat_type)}")
            exception_class = _exception_class_of(row)
            if exception_class:
                line += f" | {exception_class}"
            if row.fix_summary:
                line += f" | {truncate_utf8(row.fix_summary, ROW_SUMMARY_MAX_BYTES)}"
            lines.append(line)
        text = "\n".join(lines)
        if len(text) > INSIGHTS_USER_CONTENT_MAX_LENGTH:
            text = text[:INSIGHTS_USER_CONTENT_MAX_LENGTH] + "\n...(内容过长已截断)"
        return text

    @staticmethod
    def _parse_insight_lines(text: str, max_items: int) -> list[str]:
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

    @staticmethod
    def _rule_based_insights(month_rows, max_items: int) -> list[str]:
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
            _short_trace_id(row.trace_id)
            for row in month_rows
            if row.fix_status in ("HANDOVER", "MANUAL_PENDING")
        )
        if manual_ids:
            insights.append(f"- 以下告警需人工处理（依赖排查/SQL/配置），建议跟进闭环：{manual_ids}。")

        open_ids = "/".join(
            _short_trace_id(row.trace_id)
            for row in month_rows
            if row.fix_status is None or row.fix_status == "FAILED"
        )
        if open_ids:
            insights.append(f"- 以下告警未完成自动修复（未发起或失败），建议人工跟进：{open_ids}。")
        elif not manual_ids:
            insights.append("- 本期告警均已闭环（已修复或判定无需处理），无待跟进事项。")
        return insights[:max_items]

    # ------------------------------------------------------------------ 落库查询与推送

    def _list_by_range(self, start: date, end: date):
        start_dt = datetime(start.year, start.month, start.day, tzinfo=ZONE)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=ZONE)
        return self._repository.list_between(start_dt, end_dt)

    def _send_to_chats(self, content: str) -> None:
        chat_ids: list[str] = []
        for chat_id in self._daily.chat_ids:
            trimmed = chat_id.strip()
            if trimmed and trimmed not in chat_ids:
                chat_ids.append(trimmed)
        if not chat_ids and self._config.feishu.default_chat_id:
            chat_ids.append(self._config.feishu.default_chat_id.strip())
        if not chat_ids:
            log.warning("未配置日报推送群(analyse.daily-report.chat_ids)，告警日报未发送")
            return
        template_id = (self._daily.card_template_id or "").strip()
        for chat_id in chat_ids:
            try:
                if template_id:
                    self._feishu.send_card_to_chat(
                        chat_id, build_template_card(template_id, {"content": content}))
                else:
                    self._feishu.send_text_to_chat(chat_id, content)
                log.info("告警日报已推送, chatId=%s, templateId=%s, length=%s",
                         chat_id, template_id, len(content))
            except Exception as exc:  # noqa: BLE001 - 单群失败不阻断其他群
                log.error("告警日报推送失败, chatId=%s, err=%s", chat_id, exc)


# ---------------------------------------------------------------------- 纯函数

def _stats_of(rows) -> PeriodStats:
    stats = PeriodStats(total=len(rows))
    for row in rows:
        status = row.fix_status
        if status == "FIXED":
            stats.fixed += 1
        elif status == "FAILED":
            stats.failed += 1
        elif status == "NOT_NEEDED":
            stats.not_needed += 1
        elif status == "HANDOVER":
            stats.handover += 1
        elif status == "MANUAL_PENDING":
            stats.manual_pending += 1
        if row.repeat_type == REPEAT_REPEATED:
            stats.repeated += 1
        elif row.repeat_type == REPEAT_RECURRED:
            stats.recurred += 1
    stats.fix_notified = (stats.fixed + stats.failed + stats.not_needed
                          + stats.handover + stats.manual_pending)
    # 修复成功率=成功/(错误告警-无需处理-转人工-待人工-重复)，非自动修复项剔除出分母
    # （重复告警不是独立问题，留在分母会被刷屏告警稀释成功率）；分母为 0 且有告警即无可自动修复项，
    # 零失败记 100（记 0 会误读为全部修复失败）；无告警时该期不展示成功率，记 0
    rate_base = (stats.total - stats.not_needed - stats.handover
                 - stats.manual_pending - stats.repeated)
    if rate_base > 0:
        stats.success_rate = stats.fixed * 100.0 / rate_base
    else:
        stats.success_rate = 100.0 if stats.total > 0 else 0.0
    return stats


def _stats_line(stats: PeriodStats) -> str:
    """首行告警/修复通知/成功/失败/成功率；次行仅展示非零的重复/复发/无需修复/转人工/待人工段。

    该期无告警时只展示「错误告警 0 条」。
    """
    if stats.total == 0:
        return "🔴 错误告警 0 条"
    first = (f"🔴 错误告警 {stats.total} 条・🔧 修复通知 {stats.fix_notified} 条"
             f"・✅ 成功 {stats.fixed} 条・❌ 失败 {stats.failed} 条"
             f"・📈 修复成功率 {stats.success_rate:.1f}%")
    extra = [f"🔁 重复 {stats.repeated} 条" if stats.repeated else None,
             f"↩️ 复发 {stats.recurred} 条" if stats.recurred else None,
             f"🚫 无需修复 {stats.not_needed} 条" if stats.not_needed else None,
             f"👥 转人工 {stats.handover} 条" if stats.handover else None,
             f"⏳ 待人工 {stats.manual_pending} 条" if stats.manual_pending else None]
    segments = [seg for seg in extra if seg]
    return first + ("\n" + "・".join(segments) if segments else "")


def _month_start_suffix(month_rows) -> str:
    """本月最早告警晚于月初时追加「，从 MM-dd 起算」。"""
    earliest = min(
        (row.create_time for row in month_rows if row.create_time is not None),
        default=None,
    )
    if earliest is None:
        return ""
    earliest_date = earliest.replace(tzinfo=ZONE).date()
    if earliest_date.day == 1:
        return ""
    return f"，从 {earliest_date:%m-%d} 起算"


def _week_start(day: date) -> date:
    """ISO 周一。"""
    return day - timedelta(days=day.weekday())


def _week_number(day: date) -> int:
    return day.isocalendar()[1]


def _add_months(first_of_month: date, months: int) -> date:
    month_index = first_of_month.month - 1 + months
    year = first_of_month.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _short_trace_id(trace_id: str | None) -> str:
    """traceId 取末段编号，如 ccd-design-project-5290 → 5290。"""
    if not trace_id:
        return "-"
    short_id = trace_id.rsplit("-", 1)[-1]
    return short_id or trace_id


def _fix_status_label(fix_status: str | None) -> str:
    if fix_status is None:
        return "暂无修复通知"
    return {"FIXED": "已修复", "NOT_NEEDED": "无需修复", "FAILED": "失败",
            "HANDOVER": "已转人工", "MANUAL_PENDING": "待人工处理"}.get(fix_status, fix_status)


def _repeat_marker(repeat_type: str | None) -> str:
    """明细行重复标注：重复告警「（重复）」、修复后复发「（复发）」，其余空串。"""
    if repeat_type == REPEAT_REPEATED:
        return "（重复）"
    if repeat_type == REPEAT_RECURRED:
        return "（复发）"
    return ""


def _exception_class_of(row) -> str | None:
    """从落库的 AI 分析结果 JSON 里提取异常类名（失败忽略，不影响明细行）。"""
    if not row.analyze_content:
        return None
    try:
        bo = AlarmAnalyseResult.from_json(row.analyze_content)
        return bo.exception_class if bo else None
    except Exception:  # noqa: BLE001
        return None


__all__ = ["DailyReportService", "PeriodStats"]
