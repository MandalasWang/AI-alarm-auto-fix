"""GitLab API v4 客户端（读码兜底通道 + 修复提交兜底通道 + MR 合拢）。

主路径是 Agent 网关（见 app/providers/agent_gateway.py），
本客户端在其失败/未启用时直连 GitLab：
- 读文件：GET /repository/files/{path}?ref=（base64 解码）；
- 修复提交：POST /repository/branches 建分支（已存在复用）→
  PUT /repository/files/{path} 服务端直接落 commit（无需本地 clone）；
- MR 合拢：POST /merge_requests 自动建 MR，分支/MR 查询供超时未合并催办扫描。
"""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.core.errors import UpstreamError
from app.core.http import HttpClient

log = logging.getLogger(__name__)

SEARCH_API = "/api/v4/projects"
FILE_API = "/api/v4/projects/{project_id}/repository/files/{file_path}"
BRANCH_API = "/api/v4/projects/{project_id}/repository/branches"
COMMIT_API = "/api/v4/projects/{project_id}/repository/commits"
MERGE_REQUEST_API = "/api/v4/projects/{project_id}/merge_requests"


@dataclass(frozen=True)
class CommitInfo:
    short_id: str | None
    web_url: str | None
    message: str | None


class GitLabClient:
    def __init__(self, http: HttpClient, base_url: str, token: str, timeout_ms: int = 10_000):
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_ms = timeout_ms

    # ------------------------------------------------------------------ 读

    def search_project(self, keyword: str) -> str | None:
        """项目搜索，返回第一个匹配项目的 ID（字符串），找不到返回 None。"""
        try:
            response = self._http.fetch(
                "GET",
                f"{self._base_url}{SEARCH_API}",
                params={"search": keyword, "simple": "true", "per_page": "5"},
                headers=self._headers(),
                timeout_ms=self._timeout_ms,
            )
        except UpstreamError as exc:
            log.warning("GitLab 项目搜索异常, keyword=%s, err=%s", keyword, exc.detail)
            return None
        if response.status_code != 200:
            log.warning("GitLab 项目搜索失败, keyword=%s, status=%s", keyword, response.status_code)
            return None
        projects = response.json()
        return str(projects[0]["id"]) if projects else None

    def read_file(self, project_id: str, file_path: str, ref: str) -> str | None:
        """读取文件文本；未命中/异常返回 None（由上层继续尝试其余候选分支）。"""
        url = f"{self._base_url}{FILE_API.format(project_id=project_id, file_path=quote(file_path, safe=''))}"
        try:
            response = self._http.fetch(
                "GET",
                url,
                params={"ref": ref},
                headers=self._headers(),
                timeout_ms=self._timeout_ms,
            )
        except UpstreamError as exc:
            log.warning("GitLab 取文件异常, project=%s, file=%s, ref=%s, err=%s",
                        project_id, file_path, ref, exc.detail)
            return None
        if response.status_code != 200:
            log.info("GitLab 取文件未命中, project=%s, file=%s, ref=%s, status=%s",
                     project_id, file_path, ref, response.status_code)
            return None
        content = response.json().get("content")
        if not content:
            return None
        return base64.b64decode(content.replace("\n", "").replace("\r", "")).decode("utf-8")

    # ------------------------------------------------------------------ 写

    def ensure_branch(self, project_id: str, branch: str, ref: str) -> bool:
        """从基线分支建分支；已存在（400/409）视为成功复用（同日多条告警可修到同分支）。"""
        url = f"{self._base_url}{BRANCH_API.format(project_id=project_id)}"
        try:
            response = self._http.fetch(
                "POST",
                url,
                params={"branch": branch, "ref": ref},
                headers=self._headers(),
                timeout_ms=self._timeout_ms,
            )
        except UpstreamError as exc:
            log.warning("GitLab 建分支异常, branch=%s, err=%s", branch, exc.detail)
            return False
        if response.status_code in (200, 201):
            log.info("GitLab 修复分支已创建, project=%s, branch=%s, ref=%s", project_id, branch, ref)
            return True
        if response.status_code in (400, 409):
            log.info("GitLab 修复分支已存在，复用, branch=%s", branch)
            return True
        log.warning("GitLab 建分支失败, status=%s, body=%s", response.status_code, response.text[:200])
        return False

    def upsert_file(self, project_id: str, branch: str, file_path: str,
                    content: str, commit_message: str) -> bool:
        """提交文件到分支：PUT 更新；400「文件不存在」转 POST 创建。"""
        url = f"{self._base_url}{FILE_API.format(project_id=project_id, file_path=quote(file_path, safe=''))}"
        body = {
            "branch": branch,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "encoding": "base64",
            "commit_message": commit_message,
        }
        try:
            response = self._http.fetch(
                "PUT", url, headers=self._headers({"Content-Type": "application/json"}),
                json_body=body, timeout_ms=self._timeout_ms,
            )
            if response.status_code in (200, 201):
                log.info("GitLab 修复已提交, project=%s, branch=%s, file=%s", project_id, branch, file_path)
                return True
            # 400 = A file with this name doesn't exist：偶发新增文件场景转 POST
            if response.status_code == 400 and "doesn't exist" in (response.text or ""):
                return self._create_file(url, body)
            log.warning("GitLab 提交修复失败, status=%s, body=%s", response.status_code, response.text[:300])
            return False
        except UpstreamError as exc:
            log.warning("GitLab 提交修复异常, err=%s", exc.detail)
            return False

    def _create_file(self, url: str, body: dict) -> bool:
        try:
            response = self._http.fetch(
                "POST", url, headers=self._headers({"Content-Type": "application/json"}),
                json_body=body, timeout_ms=self._timeout_ms,
            )
        except UpstreamError as exc:
            log.warning("GitLab 创建修复文件异常, err=%s", exc.detail)
            return False
        if response.status_code in (200, 201):
            log.info("GitLab 修复新文件已创建")
            return True
        log.warning("GitLab 创建修复文件失败, status=%s, body=%s", response.status_code, response.text[:300])
        return False

    def last_commit(self, project_id: str, ref: str) -> CommitInfo | None:
        """查询分支最新 commit（通知卡片展示 short_id 与 diff 链接）。"""
        url = f"{self._base_url}{COMMIT_API.format(project_id=project_id)}"
        try:
            response = self._http.fetch(
                "GET", url, params={"ref_name": ref, "per_page": "1"},
                headers=self._headers(), timeout_ms=self._timeout_ms,
            )
        except UpstreamError as exc:
            log.warning("GitLab commit 信息查询异常, err=%s", exc.detail)
            return None
        if response.status_code != 200:
            log.warning("GitLab commit 信息查询失败, ref=%s, status=%s", ref, response.status_code)
            return None
        commits = response.json()
        if not commits:
            return None
        first = commits[0]
        return CommitInfo(first.get("short_id"), first.get("web_url"), first.get("message"))

    # ------------------------------------------------------------------ MR 与分支（合拢催办）

    def create_merge_request(self, project_id: str, source_branch: str, target_branch: str,
                             title: str, description: str,
                             remove_source_branch: bool = True) -> tuple[bool, str | None]:
        """创建 MR。返回 (是否成功, MR 链接)；409 视为已存在（成功但无链接）。

        需要 api 权限 token：只读 token 会 403，由上层记日志降级（修复本身不受影响）。
        """
        url = f"{self._base_url}{MERGE_REQUEST_API.format(project_id=project_id)}"
        body = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "remove_source_branch": bool(remove_source_branch),
        }
        try:
            response = self._http.fetch(
                "POST", url, headers=self._headers({"Content-Type": "application/json"}),
                json_body=body, timeout_ms=self._timeout_ms,
            )
        except UpstreamError as exc:
            log.warning("GitLab 创建 MR 异常, branch=%s, err=%s", source_branch, exc.detail)
            return False, None
        if response.status_code in (200, 201):
            web_url = response.json().get("web_url")
            log.info("GitLab MR 已创建, project=%s, branch=%s -> %s, url=%s",
                     project_id, source_branch, target_branch, web_url)
            return True, web_url
        if response.status_code == 409:
            log.info("GitLab MR 已存在，跳过创建, project=%s, branch=%s", project_id, source_branch)
            return True, None
        log.warning("GitLab 创建 MR 失败, status=%s, body=%s",
                    response.status_code, (response.text or "")[:300])
        return False, None

    def find_open_merge_request(self, project_id: str, source_branch: str) -> dict | None:
        """按源分支查打开状态的 MR（催办扫描用），无则 None。"""
        url = f"{self._base_url}{MERGE_REQUEST_API.format(project_id=project_id)}"
        try:
            response = self._http.fetch(
                "GET", url,
                params={"state": "opened", "source_branch": source_branch,
                        "per_page": "1", "order_by": "created_at", "sort": "desc"},
                headers=self._headers(), timeout_ms=self._timeout_ms,
            )
        except UpstreamError as exc:
            log.warning("GitLab MR 查询异常, branch=%s, err=%s", source_branch, exc.detail)
            return None
        if response.status_code != 200:
            return None
        mrs = response.json()
        return mrs[0] if mrs else None

    def list_branches(self, project_id: str, name_prefix: str, max_pages: int = 5) -> list[dict]:
        """列出名字以指定前缀开头的分支（逐页拉取后客户端过滤；催办扫描 fix_bug_* 用）。"""
        url = f"{self._base_url}{BRANCH_API.format(project_id=project_id)}"
        branches: list[dict] = []
        for page in range(1, max_pages + 1):
            try:
                response = self._http.fetch(
                    "GET", url, params={"per_page": "100", "page": str(page)},
                    headers=self._headers(), timeout_ms=self._timeout_ms,
                )
            except UpstreamError as exc:
                log.warning("GitLab 分支列表查询异常, project=%s, err=%s", project_id, exc.detail)
                return branches
            if response.status_code != 200:
                log.warning("GitLab 分支列表查询失败, project=%s, status=%s",
                            project_id, response.status_code)
                return branches
            batch = response.json()
            branches.extend(batch)
            if len(batch) < 100:  # 末页
                break
        return [b for b in branches if str(b.get("name", "")).startswith(name_prefix)]

    # ------------------------------------------------------------------ 内部

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"PRIVATE-TOKEN": self._token}
        if extra:
            headers.update(extra)
        return headers
