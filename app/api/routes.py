"""HTTP 接口层：告警接收、修复回调、忽略回调、卡片交互回调。

与 Java 版接口契约对齐（路径、参数名、响应语义）：
- POST /inner/feishu/alarm/receive          接收告警，异步分析后推送群卡片
- GET  /inner/feishu/alarm/callback/fix     卡片「修复」按钮跳转：按 traceId 自动修复（异步）
- GET  /inner/feishu/alarm/callback/ignore  卡片「忽略」按钮跳转：按 traceId 忽略告警（人工判定无需修复）
- POST /feishu/alarm/callback               飞书卡片交互回调（confirm/ignore）
- GET  /healthz                             探活

统一响应 {code, message, data}：code=0 成功。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app.container import Container
from app.core import metrics
from app.models.alarm import AlarmLog

log = logging.getLogger(__name__)


def _ok(data: object = True) -> dict:
    return {"code": 0, "message": "success", "data": data}


def _fail(message: str) -> dict:
    return {"code": 500, "message": message, "data": None}


def _toast(toast_type: str, content: str) -> dict:
    """飞书卡片回调响应（toast 提示）。"""
    return {"toast": {"type": toast_type, "content": content}}


def build_router(container: Container) -> APIRouter:
    router = APIRouter()
    cfg = container.config

    def inner_guard(request: Request) -> None:
        """/inner/* 内部接口鉴权：配置了 inner_auth_token 时校验请求头
        （兼容 Java 网关的两种头名）；未配置则放行，等价 Java 网关白名单行为。"""
        expected = cfg.feishu.inner_auth_token
        if not expected:
            return
        supplied = (request.headers.get("xizhao_request_auth")
                    or request.headers.get("xizhaoRequestAuth"))
        if supplied != expected:
            raise HTTPException(status_code=401, detail="inner auth failed")

    # ------------------------------------------------------------------ 告警接收

    @router.post("/inner/feishu/alarm/receive", summary="接收告警日志，异步分析后推送飞书群卡片",
                 dependencies=[Depends(inner_guard)])
    def receive(alarm: AlarmLog) -> dict:
        if not cfg.feishu.enable:
            log.info("飞书告警推送开关已关闭(feishu.enable=false)，忽略告警。source=%s", alarm.source)
            return _ok(False)
        metrics.ALARM_RECEIVED.labels(alarm.source or "unknown").inc()
        log.info("接收告警日志, source=%s, level=%s, traceId=%s",
                 alarm.source, alarm.level, alarm.trace_id)

        # 分析链路含 LLM 调用（分钟级），异步执行避免推送方阻塞超时；
        # 链路内部任一环节失败均有降级与日志，不影响接收结果。
        # _wrapped 里记录异常即可（BoundedExecutor 已兜底记日志，这里显式带上下文）。
        def _analyse() -> None:
            try:
                container.analyse_service.handle_alarm(alarm)
            except Exception:  # noqa: BLE001
                log.exception("告警分析异步执行失败, source=%s, traceId=%s", alarm.source, alarm.trace_id)

        container.executor.run_or_inline(_analyse)
        return _ok(True)

    # ------------------------------------------------------------------ 修复回调

    @router.get("/inner/feishu/alarm/callback/fix",
                summary="按历史分析结果自动修复代码（异步执行），结果经修复通知卡片反馈",
                dependencies=[Depends(inner_guard)])
    def callback_fix(trace_id: str = Query(..., alias="traceId", description="告警链路 ID")) -> dict:
        log.info("收到飞书卡片修复回调, traceId=%s", trace_id)
        acceptance = container.fix_service.try_accept(trace_id)
        if not acceptance.accepted:
            return _fail(acceptance.message)

        # 修复流程异步执行（LLM 生成分钟级），两把锁均由异步任务释放
        result, result_id = acceptance.result, acceptance.record_id

        def _fix() -> None:
            container.fix_service.execute_fix(trace_id, result, result_id)  # type: ignore[arg-type]

        container.executor.run_or_inline(_fix)
        return _ok(True)

    # ------------------------------------------------------------------ 忽略回调

    @router.get("/inner/feishu/alarm/callback/ignore",
                summary="忽略告警（人工判定无需修复），后续点修复/忽略将提示已忽略/已修复",
                dependencies=[Depends(inner_guard)])
    def callback_ignore(trace_id: str = Query(..., alias="traceId", description="告警链路 ID")) -> dict:
        log.info("收到飞书卡片忽略回调, traceId=%s", trace_id)
        ok, message = container.fix_service.try_ignore(trace_id)
        return _ok(True) if ok else _fail(message)

    # ------------------------------------------------------------------ 告警看板

    @router.get("/inner/feishu/alarm/dashboard/report",
                summary="告警看板统计：异步统计周/月数据、日/周趋势折线与月告警分析并推送飞书群；每自然日仅推送一次",
                dependencies=[Depends(inner_guard)])
    def dashboard_report() -> dict:
        """与 Java 版契约一致：异步受理（月告警分析含 LLM 十秒级，立即返回不阻塞调用方）；
        本接口被日报卡片「查看详情」按钮指向、按钮可被反复点击，故每自然日仅放行一次推送
        （Redis 按日期幂等，推送失败释放锁允许当日重试），重复触发返回 data=False + 提示。"""
        if not container.dashboard_report.try_acquire_daily_send_lock():
            log.info("看板今日已推送过，拒绝重复触发")
            return {"code": 0, "message": "今日看板已推送过，请勿重复触发", "data": False}
        log.info("收到告警看板统计请求，提交异步执行")

        def _send() -> None:
            try:
                container.dashboard_report.send_dashboard_report()
            except Exception:  # noqa: BLE001
                log.exception("告警看板异步执行失败，释放当日推送锁允许重试")
                container.dashboard_report.release_daily_send_lock()

        container.executor.run_or_inline(_send)
        return _ok(True)

    # ------------------------------------------------------------------ 卡片交互回调（公网）

    @router.post("/feishu/alarm/callback", summary="飞书告警卡片按钮点击回调")
    def card_callback(payload: dict) -> JSONResponse:
        log.info("收到飞书卡片回调: %s", payload)

        # 1. 校验 token（未配置则放行并告警，与 Java 版一致）
        callback_token = cfg.feishu.callback_token
        if not callback_token:
            log.warning("未配置 feishu.callback_token，跳过回调校验（生产环境建议配置）")
        elif callback_token != payload.get("token"):
            log.warning("飞书卡片回调 token 校验失败，忽略")
            return JSONResponse(_toast("error", "校验失败"))

        # 2. 解析 action（按钮 value 形如 {"action": "confirm"}）
        action_value = (payload.get("action") or {}).get("value") or {}
        action = str((action_value or {}).get("action") or "").lower()
        if action not in ("confirm", "ignore"):
            log.warning("飞书卡片回调未识别的 action: %s", action_value)
            return JSONResponse(_toast("error", "未知操作"))

        # 3. 取回告警分析结果（以 message_id 关联）
        message_id = str(payload.get("open_message_id") or "")
        result_json = container.store.get(container.keys.analyse_cache + message_id) if message_id else None
        if not result_json:
            log.warning("飞书卡片回调未找到告警分析结果(可能已过期)，messageId=%s", message_id)
            return JSONResponse(_toast("warning", "告警上下文已过期"))

        # 4. 路由：confirm 为扩展点（真实修复走卡片 url 跳转 /inner/feishu/alarm/callback/fix），
        #    此处与 Java 版桩实现语义一致仅记日志；ignore 忽略
        if action == "confirm":
            log.info("用户确认告警处理, messageId=%s", message_id)
            return JSONResponse(_toast("success", "已确认，处理中"))
        log.info("用户忽略告警, messageId=%s", message_id)
        return JSONResponse(_toast("success", "已忽略"))

    # ------------------------------------------------------------------ 探活与可观测

    @router.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "UP"}

    @router.get("/readyz", include_in_schema=False, summary="就绪探针：依赖组件连通性检查")
    def readyz() -> JSONResponse:
        """/healthz 只证明进程活着；本接口校验依赖组件是否可用，不可用返回 503。

        - db：业务库连通（未配置视为 not_configured，不阻断就绪——分析/卡片仍可用）；
        - kv：Redis 连通（未配置时进程内实现恒 ok）。
        LLM/外挂网关按设计允许运行期降级，不作为就绪条件。
        """
        checks: dict[str, str] = {}
        if container.repository is None:
            checks["db"] = "not_configured"
        else:
            checks["db"] = "ok" if container.repository.ping() else "error"
        checks["kv"] = "ok" if container.store.ping() else "error"
        ready = all(value in ("ok", "not_configured") for value in checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "READY" if ready else "NOT_READY", "checks": checks},
        )

    @router.get("/metrics", include_in_schema=False, summary="Prometheus 指标")
    def prometheus_metrics() -> Response:
        content_type, body = metrics.render()
        return Response(content=body, media_type=content_type)

    return router


__all__ = ["build_router"]
