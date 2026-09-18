"""飞书告警日报：统计昨日告警数据，组装日报文本，按模板卡片推送到配置的飞书群。

调度由本服务自带（APScheduler，替代 Java 版 xxl-job），默认每天 09:40（北京时间）。
内容只保留昨日（天维度）统计；洞察分析已移交告警看板（dashboard_report 的 annaly 变量，
基于本月明细生成），日报不再输出。配置 `analyse.dashboard.detail_url` 后日报卡片同样下发
`detail_url`（「查看详情」按钮跳日/周告警趋势图看板）。
统计口径与推送逻辑在 stats.py（与告警看板共用，对应 Java 版 AlarmStatsSupport）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from app.config import AppConfig, DailyReportConfig
from app.reporting import stats
from app.feishu.client import FeishuClient
from app.providers.llm import LlmChatClient
from app.storage.alarm_repository import AlarmRepository

log = logging.getLogger(__name__)


class DailyReportService:
    def __init__(self, config: AppConfig, feishu: FeishuClient, llm: LlmChatClient,
                 repository: AlarmRepository | None):
        self._config = config
        self._daily: DailyReportConfig = config.analyse.daily_report
        self._feishu = feishu
        self._llm = llm  # 洞察已移交看板，保留入参兼容既有装配/测试
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

        today = datetime.now(stats.ZONE).date()
        yesterday_rows = self._list_by_range(today - timedelta(days=1), today)

        content = self._build_report_content(today, yesterday_rows)
        # 模板变量：content + detail_url（与告警看板同一配置 dashboard.detail_url，
        # Url 类型须传 {url, pc_url} 对象；未配置不输出该变量）
        variables: dict = {"content": content}
        detail_url = (config_detail_url(self._config) or "").strip()
        if detail_url:
            variables["detail_url"] = {"url": detail_url, "pc_url": detail_url}
        stats.send_to_chats(self._feishu, self._daily.card_template_id,
                            stats.resolve_chat_ids(self._daily.chat_ids,
                                                   self._config.feishu.default_chat_id),
                            variables, content, "告警日报")
        return content

    # ------------------------------------------------------------------ 文本组装

    def _build_report_content(self, today: date, yesterday_rows) -> str:
        """头部 → 昨日统计 → 页脚（洞察分析移交告警看板 annaly 变量，日报不再输出）。"""
        yesterday_stats = stats.stats_of(yesterday_rows)
        yesterday = today - timedelta(days=1)
        bot_name = self._daily.bot_name or "归口业务小助手"

        # 紧凑排版：全部单换行，飞书文本消息里空行会显得很稀疏
        lines = [
            f"📊 {bot_name} - 告警日报",
            f"⏰ 报告时间：{datetime.now(stats.ZONE):%Y-%m-%d %H:%M} 北京时间",
            stats.MAJOR_SEPARATOR,
            f"📅 昨日（{yesterday.month} 月 {yesterday.day} 日）",
            stats.stats_line(yesterday_stats),
            "",
            stats.MAJOR_SEPARATOR,
            f"🤖 {bot_name}"
            + (f" | ⏰ 入群：{self._daily.bot_join_time.strip()}" if self._daily.bot_join_time else ""),
            f"📡 数据源：{self._daily.data_source_name or '-'}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------ 落库查询

    def _list_by_range(self, start: date, end: date):
        start_dt = datetime(start.year, start.month, start.day, tzinfo=stats.ZONE)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=stats.ZONE)
        return self._repository.list_between(start_dt, end_dt)


def config_detail_url(config: AppConfig) -> str | None:
    """看板详情链接（日报/看板卡片共用 dashboard.detail_url 配置）。"""
    return config.analyse.dashboard.detail_url
