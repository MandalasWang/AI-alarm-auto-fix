"""组件自检命令（接入向导/排障第一步）：python -m app.doctor [配置路径]。

逐项检查配置加载与各外部组件连通性（不出任何业务请求，只做最小探活），
PASS/FAIL/WARN/OFF 汇总输出；任一「已配置组件」FAIL 时退出码 1，配置非法退出码 2。
检查项：业务库 / Redis / 飞书凭证 / GitLab API / Agent 网关 / 本地 LLM 配置完整性。

网络检查可禁用（CI 冒烟等场景）：--no-net 只做配置完整性校验。
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

from app.config import AppConfig, load_config

log = logging.getLogger(__name__)

PASS, FAIL, WARN, OFF = "PASS", "FAIL", "WARN", "OFF"


@dataclass(frozen=True)
class CheckResult:
    """单项自检结果：status ∈ PASS/FAIL/WARN/OFF，hint 为修复建议。"""

    name: str
    status: str
    detail: str = ""
    hint: str = ""


def run_checks(config: AppConfig, repository, store, feishu, gitlab, gateway,
               check_net: bool = True) -> list[CheckResult]:
    """逐项自检（组件由调用方装配，便于单测注入假件）。"""
    results: list[CheckResult] = []

    # ---- 业务库（修复/忽略/结案/日报/看板的数据源）----
    db_cfg = config.storage.database
    if not db_cfg.configured:
        results.append(CheckResult("业务库 storage.database", OFF,
                                   hint="分析卡片可用，但修复/忽略/结案回调与日报、看板不可用"))
    elif not check_net:
        results.append(CheckResult("业务库 storage.database", WARN, detail="已配置（--no-net 跳过连通检查）"))
    else:
        ok = repository is not None and repository.ping()
        results.append(CheckResult("业务库 storage.database", PASS if ok else FAIL,
                                   hint="检查 URL/账号/网络与建表（sql/schema.sql）"))

    # ---- Redis（多实例必须；单实例可退化进程内）----
    if not config.storage.redis_url:
        results.append(CheckResult("Redis storage.redis_url", WARN, detail="未配置，退化为进程内锁/缓存",
                                   hint="仅单实例部署可用；多实例必须配置"))
    elif not check_net:
        results.append(CheckResult("Redis storage.redis_url", WARN, detail="已配置（--no-net 跳过连通检查）"))
    else:
        ok = store is not None and store.ping()
        results.append(CheckResult("Redis storage.redis_url", PASS if ok else FAIL,
                                   hint="检查 redis_url 与网络"))

    # ---- 飞书凭证 ----
    feishu_ready = bool(config.feishu.app_id and config.feishu.app_secret)
    if not config.feishu.enable:
        results.append(CheckResult("飞书 feishu.enable", OFF, detail="总开关关闭，receive 忽略所有告警"))
    elif not feishu_ready:
        results.append(CheckResult("飞书凭证", FAIL, detail="app_id/app_secret 未配置",
                                   hint="填 feishu.app_id/app_secret 或用环境变量覆盖"))
    elif not check_net:
        results.append(CheckResult("飞书凭证", WARN, detail="已配置（--no-net 跳过凭证检查）"))
    else:
        ok = feishu is not None and feishu.ping()
        results.append(CheckResult("飞书凭证(tenant_access_token)", PASS if ok else FAIL,
                                   hint="检查 app_id/app_secret 与 open.feishu.cn 连通性"))

    # ---- 飞书按钮/看板入口（纯配置检查）----
    if config.feishu.enable and not config.feishu.jump_url:
        results.append(CheckResult("修复按钮 feishu.jump_url", WARN, detail="未配置，卡片无「修复」按钮入口"))
    if config.feishu.enable and config.feishu.jump_url and not config.feishu.resolve_jump_url:
        results.append(CheckResult("结案按钮 feishu.resolve_jump_url", WARN,
                                   detail="未配置，卡片无「已解决」按钮（人工处理闭环走看板亦可）"))

    # ---- GitLab API（读码兜底/MR/合并状态）----
    git = config.analyse.git
    if not git.api_ready:
        results.append(CheckResult("GitLab API", OFF, detail="base_url/token 未配置",
                                   hint="网关读码可用时可不配；MR 合拢与看板合并状态需要它"))
    elif not check_net:
        results.append(CheckResult("GitLab API", WARN, detail="已配置（--no-net 跳过连通检查）"))
    else:
        ok = gitlab is not None and gitlab.ping()
        results.append(CheckResult("GitLab API(/api/v4/version)", PASS if ok else FAIL,
                                   hint="检查 base_url/token（read_api；建 MR 需 api 权限）"))

    # ---- Agent 网关 ----
    external = config.analyse.external
    if not external.configured:
        results.append(CheckResult("Agent 网关 analyse.external", OFF, detail="未启用",
                                   hint="生产建议启用（分析/修复主通道），见 docs/agent-gateway-deploy.md"))
    elif not check_net:
        results.append(CheckResult("Agent 网关 analyse.external", WARN,
                                   detail="已配置（--no-net 跳过连通检查）"))
    else:
        reachable = False
        try:
            response = gateway.fetch_root()
            reachable = response is not None
        except Exception:  # noqa: BLE001
            reachable = False
        results.append(CheckResult("Agent 网关(可达性)", PASS if reachable else FAIL,
                                   hint="检查 url/api_key 与网关服务状态（docs/agent-gateway-deploy.md）"))

    # ---- 本地 LLM（纯配置检查，不烧 token）----
    llm = config.analyse.llm
    if not llm.enabled:
        results.append(CheckResult("本地 LLM analyse.llm", OFF, detail="未启用",
                                   hint="网关不可用时的分析降级通道，建议启用"))
    else:
        ok = bool(llm.resolved_base_url and llm.resolved_model and llm.api_key)
        results.append(CheckResult("本地 LLM 配置", PASS if ok else FAIL,
                                   detail=f"provider={llm.provider} model={llm.resolved_model}",
                                   hint="api_key/base_url/model 需齐备"))

    # ---- 看板 ----
    dash = config.dashboard
    if not dash.enabled:
        results.append(CheckResult("Web 看板 dashboard.enabled", OFF))
    elif not (dash.username and dash.password):
        results.append(CheckResult("Web 看板鉴权", WARN, detail="未配置 Basic 认证",
                                   hint="生产环境建议配置 dashboard.username/password"))

    return results


def _print(results: list[CheckResult]) -> bool:
    """打印检查报告；返回是否存在 FAIL（已配置组件失败）。"""
    icons = {PASS: "✅", FAIL: "❌", WARN: "⚠️ ", OFF: "⬚ "}
    has_fail = False
    for item in results:
        has_fail = has_fail or item.status == FAIL
        line = f"{icons[item.status]} {item.name}"
        if item.detail:
            line += f"：{item.detail}"
        print(line)
        if item.hint:
            print(f"     └ {item.hint}")
    return has_fail


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check_net = "--no-net" not in argv
    argv = [a for a in argv if a != "--no-net"]
    config_path = argv[0] if argv else os.environ.get("ALARM_CONFIG")

    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001 - 配置非法是自检要报告的第一件事
        print(f"❌ 配置加载失败：{exc}")
        return 2
    print("✅ 配置加载成功" + ("" if check_net else "（--no-net：仅配置完整性检查）"))

    # 轻量装配：只构建自检需要的组件（不启动重发线程/调度器）
    from app.core.http import HttpClient
    from app.core.kv import create_store
    from app.feishu.client import FeishuClient
    from app.providers.agent_gateway import AgentGatewayClient
    from app.providers.gitlab import GitLabClient
    from app.storage.alarm_repository import AlarmRepository

    http = HttpClient()
    store = create_store(config.storage.redis_url)
    repository = AlarmRepository.create_if_configured(config.storage.database)
    feishu = FeishuClient(http, store, "doctor:retry", config.feishu.base_url,
                          config.feishu.app_id, config.feishu.app_secret)
    git = config.analyse.git
    gitlab = GitLabClient(http, git.base_url, git.token, git.timeout_ms)
    gateway = AgentGatewayClient(http, config.analyse.external, code_read_timeout_ms=git.timeout_ms)

    results = run_checks(config, repository, store, feishu, gitlab, gateway, check_net=check_net)
    has_fail = _print(results)
    http.close()
    return 1 if has_fail else 0


if __name__ == "__main__":
    sys.exit(main())
