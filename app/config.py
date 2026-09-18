"""配置模型与加载。

对应 Java 版 Nacos 下发的 feishu.alarm.* / feishu.alarm.analyse.* 全部配置项，
键名重组为更清晰的层级（feishu / analyse / storage / executor / server）。
增强点：
- pydantic 严格校验，未知键/类型错误启动即失败（防配置静默失效）；
- 敏感字段支持环境变量覆盖，避免密钥落盘；
- jdbc:mysql:// 或 mysql:// 两种 URL 均可解析为 PyMySQL 连接参数。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import ConfigError

log = logging.getLogger(__name__)

# 环境变量覆盖表：环境变量名 → (配置对象属性路径, 是否必填提示)
_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "ALARM_FEISHU_APP_ID": ("feishu", "app_id"),
    "ALARM_FEISHU_APP_SECRET": ("feishu", "app_secret"),
    "ALARM_GITLAB_TOKEN": ("analyse", "git", "token"),
    "ALARM_LLM_API_KEY": ("analyse", "llm", "api_key"),
    "ALARM_EXTERNAL_API_KEY": ("analyse", "external", "api_key"),
    "ALARM_REDIS_URL": ("storage", "redis_url"),
    "ALARM_DB_PASSWORD": ("storage", "database", "password"),
    "ALARM_DASHBOARD_PASSWORD": ("dashboard", "password"),
}


class StrictModel(BaseModel):
    """未知键一律拒绝：配置写错键名时启动即报错。"""

    model_config = ConfigDict(extra="forbid")


class FeishuConfig(StrictModel):
    enable: bool = True
    base_url: str = "https://open.feishu.cn"
    app_id: str = ""
    app_secret: str = ""
    default_chat_id: str = ""
    default_card_template_id: str = ""
    jump_url: str = ""
    ignore_jump_url: str = ""  # 忽略按钮跳转地址前缀（callback/ignore?traceId=）；空则卡片不带 ignor_url 变量
    callback_token: str = ""
    inner_auth_token: str = ""
    redis_key_ttl_hours: int = 24
    env: str = "test"


class FixValidationConfig(StrictModel):
    """修复质量闸配置：提交前校验生成的修复文件，失败转 FAILED 可重试。

    语法预检（确定性，包名/类型名/括号配对/截断检测）在 enabled 时始终执行；
    LLM 自检（第二遍大模型评审 diff 是否存在编译级错误）由 llm_self_check 单独开关。
    """

    enabled: bool = True
    llm_self_check: bool = True


class MergeRequestConfig(StrictModel):
    """修复 MR 配置：提交成功后自动建 MR，并定时催办超时未合并的 fix 分支。"""

    enabled: bool = True
    remove_source_branch: bool = True    # MR 合并后自动删源分支
    stale_hours: int = 48                # fix_bug 分支超此时长未合并即催办
    stale_remind_cron: str = "30 10 * * *"  # 催办扫描时间（北京时间 cron）；空串关闭催办
    chat_ids: list[str] = Field(default_factory=list)  # 催办群；空则回退 feishu.default_chat_id


class GitConfig(StrictModel):
    enabled: bool = True
    base_url: str = ""
    token: str = ""
    default_branch: str = "prod"
    fix_base_branch: str = "prod"
    projects: dict[str, str] = Field(default_factory=dict)
    max_snippets: int = 3
    context_lines: int = 15
    timeout_ms: int = 10_000
    mr: MergeRequestConfig = Field(default_factory=MergeRequestConfig)

    @property
    def api_ready(self) -> bool:
        return bool(self.base_url and self.token)


# provider → (默认 base_url, 默认 model)；custom 必须显式配置，(None, None) 表示无默认
LLM_PROVIDER_DEFAULTS: dict[str, tuple[str | None, str | None]] = {
    "deepseek": ("https://api.deepseek.com", "deepseek-chat"),
    "openai": ("https://api.openai.com", "gpt-4o"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode", "qwen-plus"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode", "qwen-plus"),
    "custom": (None, None),
}


class LlmConfig(StrictModel):
    enabled: bool = False
    provider: str = "deepseek"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    chat_path: str = "/v1/chat/completions"
    timeout_ms: int = 60_000
    max_tokens: int = 2000
    temperature: float = 0.2
    max_retries: int = 2

    @property
    def resolved_base_url(self) -> str | None:
        if self.base_url:
            return self.base_url.rstrip("/")
        default = LLM_PROVIDER_DEFAULTS.get(self.provider.lower(), (None, None))[0]
        return default.rstrip("/") if default else None

    @property
    def resolved_model(self) -> str | None:
        if self.model:
            return self.model
        return LLM_PROVIDER_DEFAULTS.get(self.provider.lower(), (None, None))[1]

    @property
    def endpoint(self) -> str:
        base = self.resolved_base_url
        if not base:
            raise ConfigError(f"llm.provider={self.provider} 未配置 base_url（custom provider 必须显式配置）")
        return base + (self.chat_path or "/v1/chat/completions")


class ExternalConfig(StrictModel):
    """外挂 Agent 网关（部署见 docs/agent-gateway-deploy.md）。"""

    enabled: bool = False
    url: str = ""
    api_key: str = ""
    workspace: str = "ccd-java-system"
    workspaces: dict[str, str] = Field(default_factory=dict)
    poll_interval_ms: int = 5_000
    timeout_ms: int = 300_000
    code_read_enabled: bool = True
    fix_commit_enabled: bool = True

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.url and self.api_key)

    def resolve_workspace(self, service: str | None) -> str:
        """服务名映射 → 默认工作区 → 服务名本身。"""
        if service:
            mapped = self.workspaces.get(service)
            if mapped:
                return mapped
        return self.workspace or (service or "")


class DbSourceConfig(StrictModel):
    url: str = ""
    username: str = ""
    password: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def as_pymysql_params(self) -> tuple[str, int, str]:
        """解析为 (host, port, database)。支持 jdbc:mysql:// 与 mysql:// 前缀。"""
        match = re.match(r"^(?:jdbc:)?mysql://([^/:?]+)(?::(\d+))?/([^?]+)", self.url)
        if not match:
            raise ConfigError(f"无法解析数据库 URL（支持 jdbc:mysql://host:port/db）: {self.url!r}")
        host, port, database = match.group(1), match.group(2), match.group(3)
        return host, int(port or 3306), database.split("/")[0] if "/" in database else database


class NacosConfig(StrictModel):
    enabled: bool = True
    mode: str = "direct-db"  # direct-db / open-api
    config_db: DbSourceConfig = Field(default_factory=DbSourceConfig)
    server_addr: str = ""
    namespace: str = ""
    group: str = "DEFAULT_GROUP"
    data_id_pattern: str = "{service}.yaml"
    common_data_ids: str = "xizhao-common.yaml"
    username: str = ""
    password: str = ""
    cache_minutes: int = 30
    timeout_ms: int = 10_000

    @property
    def direct_db(self) -> bool:
        return self.mode.lower() == "direct-db" and self.config_db.configured


class DbConfig(StrictModel):
    enabled: bool = False
    nacos: NacosConfig = Field(default_factory=NacosConfig)
    datasources: dict[str, DbSourceConfig] = Field(default_factory=dict)
    service_datasources: dict[str, str] = Field(default_factory=dict)
    sample_limit: int = 3
    query_timeout_seconds: int = 5


class FixNotifyConfig(StrictModel):
    card_template_id: str = ""
    manual_card_template_id: str = ""  # 人工处理/转人工通知卡片模板 ID；空则发文本消息兜底


class RepeatConfig(StrictModel):
    """重复告警/复发识别配置（按告警指纹判定）。"""

    window_hours: int = 24  # 指纹匹配回溯窗口（小时）；0 或负数关闭重复识别


class DailyReportConfig(StrictModel):
    enabled: bool = True
    cron: str = "40 9 * * *"  # 分钟级 cron（北京时间），对应 Java xxl-job 0 40 9 * * ?
    chat_ids: list[str] = Field(default_factory=list)
    card_template_id: str = "AAqTsvflVLL8E"
    bot_name: str = "归口业务小助手"
    bot_join_time: str = ""
    data_source_name: str = "归口业务告警群"
    insights_llm_enabled: bool = True
    insight_max_items: int = 4


class DashboardReportConfig(StrictModel):
    """告警看板配置（HTTP 接口按需触发：GET /inner/feishu/alarm/dashboard/report）。"""

    # 看板卡片模板 ID（模板变量：content 文本 + day_chart/week_chart 图表变量 + annaly 月告警分析）；
    # 空则回退日报模板（daily_report.card_template_id，其无图表组件，图表变量被忽略只渲染文本）
    card_template_id: str = ""
    # 详情链接（模板 Url 类型变量 detail_url，日报/看板卡片「查看详情」按钮共用，配日/周趋势看板地址）；
    # 空则卡片不下发该变量
    detail_url: str = ""


class AnalyseConfig(StrictModel):
    enabled: bool = True
    git: GitConfig = Field(default_factory=GitConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    external: ExternalConfig = Field(default_factory=ExternalConfig)
    db: DbConfig = Field(default_factory=DbConfig)
    fix_notify: FixNotifyConfig = Field(default_factory=FixNotifyConfig)
    daily_report: DailyReportConfig = Field(default_factory=DailyReportConfig)
    dashboard: DashboardReportConfig = Field(default_factory=DashboardReportConfig)
    repeat: RepeatConfig = Field(default_factory=RepeatConfig)
    fix_validation: FixValidationConfig = Field(default_factory=FixValidationConfig)


class DatabaseStoreConfig(DbSourceConfig):
    """alarm_analyze_result 所在业务库（复用数据源的 URL 解析与 configured 判定）。"""


class StorageConfig(StrictModel):
    redis_url: str = ""
    key_prefix: str = ""
    database: DatabaseStoreConfig = Field(default_factory=DatabaseStoreConfig)


class ExecutorConfig(StrictModel):
    workers: int = 4
    queue_size: int = 100


class ServerConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = 8800
    log_level: str = "INFO"


class DashboardConfig(StrictModel):
    """Web 告警看板（/dashboard）：只读展示告警处理情况/指标/趋势 + 可选 Basic 认证。"""

    enabled: bool = True
    username: str = ""                    # Basic 认证用户名；与 password 同时非空才启用鉴权
    password: str = ""                    # Basic 认证密码（可用环境变量 ALARM_DASHBOARD_PASSWORD 覆盖）
    merge_cache_seconds: int = 300        # 「是否合并」GitLab 查询结果的进程内缓存秒数


class AppConfig(StrictModel):
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    analyse: AnalyseConfig = Field(default_factory=AnalyseConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)


# ---- 与 Java 版一致的默认 key（storage.key_prefix 为空时启用，保证 Redis 数据互通）----
DEFAULT_KEYS = {
    "fix_lock": "system:feishu:alarm:fix:lock:",
    "fix_service_lock": "system:feishu:alarm:fix:service-lock:",
    "analyse_cache": "alarm:analyse:msg:",
    "feishu_retry_list": "system:feishusendfailed:list",
}


class Keys:
    """集中管理所有 Redis/缓存 key，杜绝散落字符串拼接。"""

    def __init__(self, prefix: str) -> None:
        p = prefix or ""
        self.fix_lock = p + DEFAULT_KEYS["fix_lock"]
        self.fix_service_lock = p + DEFAULT_KEYS["fix_service_lock"]
        self.analyse_cache = p + DEFAULT_KEYS["analyse_cache"]
        self.feishu_retry_list = p + DEFAULT_KEYS["feishu_retry_list"]


def _apply_env_overrides(config: AppConfig) -> list[str]:
    """环境变量覆盖敏感配置，返回已生效的变量名（用于启动日志确认，不打印值）。"""
    applied: list[str] = []
    for env_name, path in _ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if not value:
            continue
        target: Any = config
        for key in path[:-1]:
            target = getattr(target, key)
        setattr(target, path[-1], value)
        applied.append(env_name)
    return applied


def load_config(path: str | None = None) -> AppConfig:
    """加载并校验配置。

    :param path: 配置文件路径；缺省取环境变量 ALARM_CONFIG，再缺省 ./config.yaml。
    :raises ConfigError: 文件不存在 / YAML 非法 / 键名或类型错误（附 pydantic 明细）。
    """
    config_path = path or os.environ.get("ALARM_CONFIG") or "config.yaml"
    if not os.path.isfile(config_path):
        raise ConfigError(f"配置文件不存在: {config_path}（复制 config.example.yaml 为 config.yaml 后修改）")
    try:
        with open(config_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 语法错误: {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是键值映射: {config_path}")

    try:
        config = AppConfig.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"配置校验失败（检查键名拼写/类型）: {details}") from exc

    applied = _apply_env_overrides(config)
    if applied:
        log.info("以下敏感配置已由环境变量覆盖: %s", ", ".join(applied))
    return config
