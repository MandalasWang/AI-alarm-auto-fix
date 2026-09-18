"""告警统计共享支撑：告警日报（daily_report）与告警看板（dashboard_report）共用的统计口径、
纯函数与飞书卡片推送，对应 Java 版 `AlarmStatsSupport`，保证两份报表数字一致。

统计口径：错误告警=区间内 alarm_analyze_result 记录数；修复通知=fix_status 已判定数（含转人工/待人工）；
解决率=(AI修复+人工解决)/(错误告警-无需处理-转人工-待人工-重复)，非自动修复项剔除出分母
（重复剔除防刷屏稀释；人工已解决计入分子、留在分母），分母为 0 且有告警记 100%，无告警记 0% 不展示。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.fingerprint import REPEAT_RECURRED, REPEAT_REPEATED
from app.feishu.cards import build_template_card

log = logging.getLogger(__name__)

#: 报表统一按北京时间取日期边界
ZONE = ZoneInfo("Asia/Shanghai")

#: 大分隔线（头部/各统计期/页脚之间）
MAJOR_SEPARATOR = "━━━━━━━━━━━━━━━"
#: 小分隔线（统计期之间）
MINOR_SEPARATOR = "———————"


@dataclass
class PeriodStats:
    """告警周期统计。解决率 = (AI修复+人工解决)/(错误告警-无需处理-转人工-待人工-重复)，
    非自动修复项剔除出分母（人工已解决计入分子、留在分母）。"""

    total: int = 0
    fix_notified: int = 0
    fixed: int = 0
    failed: int = 0
    not_needed: int = 0
    handover: int = 0        # 已转人工排查数（上下游依赖问题）
    manual_pending: int = 0  # 待人工处理数（SQL/配置等需人工介入）
    manual_resolved: int = 0  # 人工已解决数（结案终态，计入已解决）
    repeated: int = 0        # 重复告警数（窗口内同指纹，已压卡片）
    recurred: int = 0        # 修复后复发数
    success_rate: float = 0.0


def stats_of(rows) -> PeriodStats:
    """周期统计：fix_status 六态计数 + 重复/复发计数 + 成功率。"""
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
        elif status == "MANUAL_RESOLVED":
            stats.manual_resolved += 1
        if row.repeat_type == REPEAT_REPEATED:
            stats.repeated += 1
        elif row.repeat_type == REPEAT_RECURRED:
            stats.recurred += 1
    stats.fix_notified = (stats.fixed + stats.failed + stats.not_needed
                          + stats.handover + stats.manual_pending)
    # 解决率=(AI修复+人工解决)/(错误告警-无需处理-转人工-待人工-重复)：非自动修复项剔除出分母
    # （重复告警不是独立问题，留在分母会被刷屏告警稀释成功率）；人工已解决是真实解决，计入分子、
    # 留在分母。分母为 0 且有告警即无可自动修复项，零失败记 100（记 0 会误读为全部修复失败）；
    # 无告警时该期不展示成功率，记 0
    rate_base = (stats.total - stats.not_needed - stats.handover
                 - stats.manual_pending - stats.repeated)
    if rate_base > 0:
        stats.success_rate = (stats.fixed + stats.manual_resolved) * 100.0 / rate_base
    else:
        stats.success_rate = 100.0 if stats.total > 0 else 0.0
    return stats


def stats_line(stats: PeriodStats) -> str:
    """首行告警/修复通知/成功/失败/成功率；
    次行仅展示非零的 重复/复发/无需修复/转人工/待人工/人工解决 段。

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
             f"⏳ 待人工 {stats.manual_pending} 条" if stats.manual_pending else None,
             f"🛠 人工解决 {stats.manual_resolved} 条" if stats.manual_resolved else None]
    segments = [seg for seg in extra if seg]
    return first + ("\n" + "・".join(segments) if segments else "")


def month_start_suffix(month_rows) -> str:
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


def week_start(day: date) -> date:
    """ISO 周一。"""
    return day - timedelta(days=day.weekday())


def week_number(day: date) -> int:
    return day.isocalendar()[1]


def add_months(first_of_month: date, months: int) -> date:
    month_index = first_of_month.month - 1 + months
    year = first_of_month.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def short_trace_id(trace_id: str | None) -> str:
    """traceId 取末段编号，如 ccd-design-project-5290 → 5290。"""
    if not trace_id:
        return "-"
    short_id = trace_id.rsplit("-", 1)[-1]
    return short_id or trace_id


def fix_status_label(fix_status: str | None) -> str:
    if fix_status is None:
        return "暂无修复通知"
    return {"FIXED": "已修复", "NOT_NEEDED": "无需修复", "FAILED": "失败",
            "HANDOVER": "已转人工", "MANUAL_PENDING": "待人工处理",
            "MANUAL_RESOLVED": "人工已解决"}.get(fix_status, fix_status)


def repeat_marker(repeat_type: str | None) -> str:
    """明细行重复标注：重复告警「（重复）」、修复后复发「（复发）」，其余空串。"""
    if repeat_type == REPEAT_REPEATED:
        return "（重复）"
    if repeat_type == REPEAT_RECURRED:
        return "（复发）"
    return ""


def exception_class_of(row) -> str | None:
    """从落库的 AI 分析结果 JSON 里提取异常类名（失败忽略，不影响明细行）。"""
    if not row.analyze_content:
        return None
    try:
        from app.models.alarm import AlarmAnalyseResult
        bo = AlarmAnalyseResult.from_json(row.analyze_content)
        return bo.exception_class if bo else None
    except Exception:  # noqa: BLE001
        return None


def resolve_chat_ids(chat_ids: list[str], default_chat_id: str | None) -> list[str]:
    """配置群列表（去空格去重），未配置时回退默认告警群。"""
    resolved: list[str] = []
    for chat_id in chat_ids or []:
        trimmed = chat_id.strip()
        if trimmed and trimmed not in resolved:
            resolved.append(trimmed)
    if not resolved and default_chat_id and default_chat_id.strip():
        resolved.append(default_chat_id.strip())
    return resolved


def send_to_chats(feishu, template_id: str, chat_ids: list[str],
                  variables: dict, text_fallback: str, log_tag: str) -> None:
    """推送到所有配置群：模板卡片（template_variable 全量下发，可含文本/图表/链接变量）优先，
    模板未配置发文本兜底；单群失败不阻断其他群。对应 Java 版 AlarmStatsSupport.sendToChats。"""
    if not chat_ids:
        log.warning("未配置%s推送群(analyse.daily-report.chat_ids)，%s未发送", log_tag, log_tag)
        return
    template = (template_id or "").strip()
    for chat_id in chat_ids:
        try:
            if template:
                feishu.send_card_to_chat(chat_id, build_template_card(template, variables))
            else:
                feishu.send_text_to_chat(chat_id, text_fallback)
            log.info("%s已推送, chatId=%s, templateId=%s, length=%s",
                     log_tag, chat_id, template, len(text_fallback))
        except Exception as exc:  # noqa: BLE001 - 单群失败不阻断其他群
            log.error("%s推送失败, chatId=%s, err=%s", log_tag, chat_id, exc)
