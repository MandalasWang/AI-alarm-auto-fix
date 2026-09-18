"""依赖装配：按配置构建全部组件并接线（等价 Spring 的容器，但显式可见）。

灵活性设计：所有可选链路（GitLab / 查库 / LLM / 外挂 Agent / Redis / 业务库）
未启用或未配置时装配为对应形态或 None，服务照常启动，运行期按可用性降级。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.analysis.report import ReportGenerator
from app.analysis.service import AlarmAnalyseService
from app.config import AppConfig, Keys, load_config
from app.core.executor import BoundedExecutor
from app.core.http import HttpClient
from app.core.kv import KeyValueStore, create_store
from app.feishu.client import FeishuClient
from app.fixing.committer import FixCommitter
from app.fixing.generator import FixGenerator
from app.fixing.merger import MergeRequestService
from app.fixing.service import FixService
from app.fixing.validation import FixValidator
from app.providers.agent_gateway import AgentGatewayClient
from app.providers.db_context import DatabaseContextFetcher
from app.providers.gitlab import GitLabClient
from app.providers.llm import LlmChatClient
from app.providers.nacos import NacosConfigResolver
from app.reporting.daily_report import DailyReportService
from app.reporting.dashboard_report import AlarmDashboardService
from app.storage.alarm_repository import AlarmRepository

log = logging.getLogger(__name__)


@dataclass
class Container:
    config: AppConfig
    keys: Keys
    http: HttpClient
    store: KeyValueStore
    executor: BoundedExecutor
    feishu: FeishuClient
    gitlab: GitLabClient
    gateway: AgentGatewayClient
    nacos: NacosConfigResolver
    db_context: DatabaseContextFetcher
    llm: LlmChatClient
    report_generator: ReportGenerator
    analyse_service: AlarmAnalyseService
    fix_generator: FixGenerator
    fix_committer: FixCommitter
    fix_service: FixService
    fix_validator: FixValidator
    mr_service: MergeRequestService
    daily_report: DailyReportService
    dashboard_report: AlarmDashboardService
    repository: AlarmRepository | None

    def shutdown(self) -> None:
        self.executor.shutdown()
        self.feishu.stop_retry_loop()
        self.http.close()


def load_and_build(config_path: str | None = None) -> Container:
    config = load_config(config_path)
    keys = Keys(config.storage.key_prefix)

    http = HttpClient()
    store = create_store(config.storage.redis_url)
    executor = BoundedExecutor(config.executor.workers, config.executor.queue_size, name="alarm")

    feishu = FeishuClient(http, store, keys.feishu_retry_list,
                          config.feishu.base_url, config.feishu.app_id, config.feishu.app_secret)
    git = config.analyse.git
    gitlab = GitLabClient(http, git.base_url, git.token, git.timeout_ms)
    gateway = AgentGatewayClient(http, config.analyse.external, code_read_timeout_ms=git.timeout_ms)
    nacos = NacosConfigResolver(config)
    db_context = DatabaseContextFetcher(config, nacos)
    llm = LlmChatClient(http, config.analyse.llm)
    report_generator = ReportGenerator(llm, gateway)

    repository = AlarmRepository.create_if_configured(config.storage.database)
    analyse_service = AlarmAnalyseService(
        config, keys, store, feishu, gitlab, gateway, db_context, report_generator,
        repository=repository, context_lines=git.context_lines,
    )

    fix_generator = FixGenerator(config, llm, gateway, gitlab, stack_reader=_StackReader(analyse_service))
    fix_committer = FixCommitter(config, gateway, gitlab)
    fix_validator = FixValidator(config.analyse.fix_validation, llm, gateway)
    mr_service = MergeRequestService(config, gitlab, feishu)
    fix_service = FixService(config, keys, store, feishu, fix_generator, fix_committer, repository,
                             validator=fix_validator, merger=mr_service)
    daily_report = DailyReportService(config, feishu, llm, repository)
    dashboard_report = AlarmDashboardService(config, feishu, llm, repository, store)

    return Container(
        config=config, keys=keys, http=http, store=store, executor=executor, feishu=feishu,
        gitlab=gitlab, gateway=gateway, nacos=nacos, db_context=db_context, llm=llm,
        report_generator=report_generator, analyse_service=analyse_service,
        fix_generator=fix_generator, fix_committer=fix_committer, fix_service=fix_service,
        fix_validator=fix_validator, mr_service=mr_service,
        daily_report=daily_report, dashboard_report=dashboard_report, repository=repository,
    )


class _StackReader:
    """把 AlarmAnalyseService 的业务帧解析能力以窄接口暴露给修复生成器。"""

    def __init__(self, service: AlarmAnalyseService):
        self._service = service

    def business_file_paths(self, stack_trace: str | None, max_frames: int) -> list[str]:
        from app.extractors.stack import business_file_paths

        return business_file_paths(stack_trace, max_frames)
