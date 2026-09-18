"""应用入口：装配容器、注册路由、管理后台任务生命周期。

启动：python -m app.main   （或 uvicorn app.main:create_app --factory）
配置：环境变量 ALARM_CONFIG 指定配置文件路径，缺省 ./config.yaml
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI

from app.api.routes import build_router
from app.container import Container, load_and_build
from app.core.logging import setup_logging

log = logging.getLogger(__name__)

#: 全局容器引用（uvicorn app.main:app 方式启动时由 create_app 填充）
container: Container | None = None


def create_app(config_path: str | None = None) -> FastAPI:
    global container  # noqa: PLW0603
    container = load_and_build(config_path)
    setup_logging(container.config.server.log_level)
    cfg = container.config

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 启动：飞书失败重发线程 + 定时调度（日报 / 修复分支催办）
        container.feishu.start_retry_loop()
        scheduler = _start_scheduler(container)
        log.info("AI-alarm_autoFix 启动完成, port=%s, env=%s", cfg.server.port, cfg.feishu.env)
        yield
        # 停机：先停调度与重发线程，再等在途分析/修复收尾（避免任务被强杀产生半提交状态）
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        container.shutdown()
        log.info("AI-alarm_autoFix 已停止")

    app = FastAPI(
        title="AI-alarm_autoFix",
        description="飞书告警接收 → AI 分析 → 自动修复 → 结果通知（Java 版 ccd-java-system "
                    "feishu.alarm 模块的 Python 重写）",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(build_router(container))
    if cfg.dashboard.enabled:
        from app.web.dashboard import build_dashboard_router

        app.include_router(build_dashboard_router(container))
        log.info("Web 告警看板已启用: /dashboard")
    return app


def _start_scheduler(container: Container):
    """APScheduler 启动定时任务（北京时间 cron），执行提交到分析线程池（LLM 分钟级，不占调度线程）。

    - 日报：daily_report.enabled 开启；
    - 修复分支催办：git.mr.enabled 且 stale_remind_cron 非空；
    - 转人工催办：fix_notify.stale_remind_cron 非空。
    全部未启用则不启动调度线程。
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    analyse = container.config.analyse
    jobs: list[tuple[str, str, Callable[[], None]]] = []
    if analyse.daily_report.enabled:
        jobs.append(("feishuAlarmDailyReport", analyse.daily_report.cron,
                     container.daily_report.send_daily_report))
    if analyse.git.mr.enabled and analyse.git.mr.stale_remind_cron.strip():
        jobs.append(("alarmFixMrStaleRemind", analyse.git.mr.stale_remind_cron,
                     container.mr_service.remind_stale))
    if analyse.fix_notify.stale_remind_cron.strip():
        jobs.append(("alarmManualStaleRemind", analyse.fix_notify.stale_remind_cron,
                     container.fix_service.remind_stale_manual))
    if not jobs:
        return None

    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    for job_id, cron, task in jobs:
        scheduler.add_job(
            lambda task=task: container.executor.run_or_inline(task),
            trigger=CronTrigger.from_crontab(cron, timezone="Asia/Shanghai"),
            id=job_id,
            misfire_grace_time=3600,
            coalesce=True,  # 错过的触发合并为一次，避免停机补跑多发
        )
        log.info("定时任务已注册, id=%s, cron=%s（北京时间）", job_id, cron)
    scheduler.start()
    return scheduler


def main() -> None:
    """命令行启动入口：python -m app.main（也可 uvicorn app.main:create_app --factory）。"""
    import os

    import uvicorn

    from app.config import load_config

    cfg = load_config(os.environ.get("ALARM_CONFIG"))
    uvicorn.run(
        "app.main:create_app",
        factory=True,  # 延迟创建：import 时不加载配置，仅真正启动时装配
        host=cfg.server.host,
        port=cfg.server.port,
        log_config=None,  # 用模块内统一日志格式
    )


if __name__ == "__main__":
    main()
