"""外挂 Agent 网关客户端（任务式协议）。

网关部署与协议详见 docs/agent-gateway-deploy.md。端点一览：
- POST /analyze      提交分析/修复任务（taskType=analyse|fix, workspace, prompt）→ {data:{taskId}}
- GET  /analyze/{id} 轮询任务状态 → SUCCESS 取 lastMessage 为 Agent 最终输出原文
- GET  /code/file    从网关本地 git workspace 读源文件（webhook+每晚同步保鲜）
- POST /fix/commit   在网关独立 worktree 建 fix 分支提交并 push（需 GITLAB_PUSH_TOKEN）

JSON 契约校验一律在本侧完成，网关只回传 Agent 输出原文。
优先于本地 LLM 调用，失败由上层降级（分析降级本地 LLM、提交降级 GitLab API 直连）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import quote

from app.config import ExternalConfig
from app.core import metrics
from app.core.errors import UpstreamError
from app.core.http import HttpClient

log = logging.getLogger(__name__)

TASK_TYPE_ANALYSE = "analyse"
TASK_TYPE_FIX = "fix"

#: 任务提交/单次状态查询超时：仅入队与查状态，很快
_HTTP_TIMEOUT_MS = 15_000


@dataclass(frozen=True)
class GatewayCommitResult:
    branch: str
    short_id: str | None
    web_url: str | None
    message: str | None


class AgentGatewayClient:
    def __init__(self, http: HttpClient, config: ExternalConfig, code_read_timeout_ms: int = 10_000):
        self._http = http
        self._config = config
        self._code_read_timeout_ms = code_read_timeout_ms

    # ------------------------------------------------------------------ 可用性

    @property
    def code_read_available(self) -> bool:
        return self._config.code_read_enabled and self._config.configured

    @property
    def commit_available(self) -> bool:
        return self._config.fix_commit_enabled and self._config.configured

    def fetch_root(self):
        """可达性探测（doctor/预检用）：GET 网关根路径，返回原始响应（任何 HTTP 状态都算可达）。

        网关未定义根路由时通常返回 404——连接层通即可；连接失败抛 UpstreamError 由调用方处理。
        """
        return self._http.fetch("GET", self._config.url.rstrip("/") + "/",
                                headers={"Authorization": f"Bearer {self._config.api_key}"},
                                timeout_ms=self._code_read_timeout_ms)

    # ------------------------------------------------------------------ 读码

    def fetch_file(self, service: str, file_path: str, ref: str) -> str | None:
        """从网关本地 workspace 读文件；不可用/未命中/异常均返回 None（由上层降级 GitLab API）。"""
        if not self.code_read_available:
            return None
        cfg = self._config
        workspace = cfg.resolve_workspace(service)
        try:
            response = self._http.fetch(
                "GET",
                cfg.url.rstrip("/") + "/code/file",
                params={"workspace": workspace, "path": file_path, "ref": ref},
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                timeout_ms=self._code_read_timeout_ms,
            )
        except UpstreamError as exc:
            log.warning("网关读码异常, workspace=%s, file=%s, err=%s", workspace, file_path, exc.detail)
            return None
        if response.status_code != 200:
            log.warning("网关读码失败, workspace=%s, file=%s, ref=%s, status=%s",
                        workspace, file_path, ref, response.status_code)
            return None
        data = response.json().get("data")
        return data.get("content") if data else None

    # ------------------------------------------------------------------ 任务（分析/修复生成）

    def chat(self, service: str, task_type: str, system_prompt: str, user_content: str) -> str:
        """提交 Agent 任务并同步轮询至结束，返回最终输出原文。

        运行在告警后台线程池中，轮询等待不阻塞请求线程；
        超时/失败抛 UpstreamError，由上层降级本地 LLM。
        """
        cfg = self._config
        workspace = cfg.resolve_workspace(service)
        prompt = f"{system_prompt}\n\n{user_content}"
        started = time.monotonic()
        try:
            task_id = self._submit(task_type, workspace, prompt)
            result = self._poll_until_done(task_id)
        except Exception:
            metrics.LLM_CALL.labels("gateway", task_type, "error").inc()
            raise
        finally:
            metrics.LLM_LATENCY.labels("gateway", task_type).observe(time.monotonic() - started)
        metrics.LLM_CALL.labels("gateway", task_type, "ok").inc()
        return result

    def _submit(self, task_type: str, workspace: str, prompt: str) -> str:
        cfg = self._config
        log.info("提交外挂Agent任务, taskType=%s, workspace=%s, promptLength=%s",
                 task_type, workspace, len(prompt))
        response = self._http.fetch(
            "POST",
            cfg.url.rstrip("/") + "/analyze",
            headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
            json_body={"taskType": task_type, "workspace": workspace, "prompt": prompt},
            timeout_ms=_HTTP_TIMEOUT_MS,
        )
        text = response.text or ""
        if response.status_code != 200:
            raise UpstreamError("Agent网关", f"任务提交失败 status={response.status_code} body={text[:500]}")
        payload = response.json()
        if payload.get("code") != 0:
            raise UpstreamError("Agent网关", f"任务提交被拒: {text[:500]}")
        task_id = (payload.get("data") or {}).get("taskId")
        if not task_id:
            raise UpstreamError("Agent网关", f"任务提交返回无 taskId: {text[:500]}")
        log.info("外挂Agent任务已受理, taskId=%s, taskType=%s", task_id, task_type)
        return task_id

    def _poll_until_done(self, task_id: str) -> str:
        cfg = self._config
        deadline = time.monotonic() + cfg.timeout_ms / 1000.0
        interval = max(1.0, cfg.poll_interval_ms / 1000.0)
        while time.monotonic() < deadline:
            time.sleep(interval)
            data = self._fetch_task(task_id)
            if data is None:
                continue
            status = data.get("status")
            if status == "SUCCESS":
                last_message = data.get("lastMessage")
                if not last_message:
                    raise UpstreamError("Agent网关", f"任务成功但输出为空, taskId={task_id}")
                log.info("外挂Agent任务完成, taskId=%s, outputLength=%s", task_id, len(last_message))
                return str(last_message).strip()
            if status == "FAILED":
                raise UpstreamError("Agent网关", f"任务失败 taskId={task_id} error={data.get('error')}")
            # PENDING / RUNNING 继续轮询
        raise UpstreamError("Agent网关", f"任务轮询超时({cfg.timeout_ms}ms), taskId={task_id}")

    def _fetch_task(self, task_id: str) -> dict | None:
        """查询单次任务状态；网络抖动等临时失败返回 None 由轮询继续。"""
        cfg = self._config
        try:
            response = self._http.fetch(
                "GET",
                f"{cfg.url.rstrip('/')}/analyze/{quote(str(task_id), safe='')}",
                headers={"Authorization": f"Bearer {cfg.api_key}"},
                timeout_ms=_HTTP_TIMEOUT_MS,
            )
        except UpstreamError as exc:
            log.warning("外挂Agent任务状态查询异常, taskId=%s, err=%s", task_id, exc.detail)
            return None
        if response.status_code != 200:
            log.warning("外挂Agent任务状态查询失败, taskId=%s, status=%s", task_id, response.status_code)
            return None
        payload = response.json()
        return payload.get("data") if payload.get("code") == 0 else None

    # ------------------------------------------------------------------ 修复提交

    def commit_fix(self, service: str, branch: str, base_branch: str, file_path: str,
                   content: str, commit_message: str) -> GatewayCommitResult | None:
        """网关侧建 fix 分支提交并 push；失败返回 None（上层降级 GitLab API 直连）。

        提交含 fetch+push 两段内网 git 往返，读码超时（10s 级）不够用，下限 60s。
        """
        if not self.commit_available:
            return None
        cfg = self._config
        workspace = cfg.resolve_workspace(service)
        timeout_ms = max(self._code_read_timeout_ms, 60_000)
        try:
            response = self._http.fetch(
                "POST",
                cfg.url.rstrip("/") + "/fix/commit",
                headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
                json_body={
                    "workspace": workspace,
                    "branch": branch,
                    "baseBranch": base_branch,
                    "path": file_path,
                    "content": content,
                    "message": commit_message,
                },
                timeout_ms=timeout_ms,
            )
        except UpstreamError as exc:
            log.warning("网关修复提交异常, workspace=%s, branch=%s, err=%s", workspace, branch, exc.detail)
            return None
        if response.status_code != 200:
            log.warning("网关修复提交失败, workspace=%s, branch=%s, file=%s, status=%s, body=%s",
                        workspace, branch, file_path, response.status_code, (response.text or "")[:300])
            return None
        data = response.json().get("data")
        if not data or not data.get("branch"):
            log.warning("网关修复提交返回无 branch, workspace=%s, branch=%s", workspace, branch)
            return None
        result = GatewayCommitResult(
            branch=data["branch"], short_id=data.get("shortId"),
            web_url=data.get("webUrl"), message=data.get("message"),
        )
        log.info("网关修复提交完成, workspace=%s, branch=%s, shortId=%s", workspace, branch, result.short_id)
        return result
