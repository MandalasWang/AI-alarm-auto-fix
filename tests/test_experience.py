"""体验与产品化回归：告警卡片 detail_url、看板处理操作接口、doctor 自检、保留期配置。"""
import textwrap
import threading
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.doctor import FAIL, OFF, PASS, WARN, run_checks
from app.feishu.cards import build_alarm_card
from app.fixing.service import FixAcceptance
from app.main import create_app
from app.models.alarm import AlarmAnalyseResult, AlarmAnalyzeRecord

# ---------------------------------------------------------------- 告警卡片 detail_url


def test_alarm_card_detail_url_when_configured():
    result = AlarmAnalyseResult(service="svc", trace_id="t-1")
    card = build_alarm_card(result, "tpl", "", detail_url="http://host:8800/dashboard")
    variables = card["data"]["template_variable"]
    assert variables["detail_url"] == {"url": "http://host:8800/dashboard",
                                       "pc_url": "http://host:8800/dashboard"}


def test_alarm_card_no_detail_url_by_default():
    result = AlarmAnalyseResult(service="svc", trace_id="t-1")
    card = build_alarm_card(result, "tpl", "")
    assert "detail_url" not in card["data"]["template_variable"]


# ---------------------------------------------------------------- 看板处理操作接口


class _ActionRepository:
    """get_by_id 假仓储：id=1 → 未处理记录；其余 None。"""

    def get_by_id(self, record_id):
        if record_id != 1:
            return None
        return AlarmAnalyzeRecord(id=1, trace_id="t-9", service_name="svc-a",
                                  fix_status=None, create_time=datetime(2026, 9, 18, 9, 0))


class _StubFixService:
    """只覆盖看板操作接口用到的三个方法；执行用 Event 通知避免测试线程竞态。"""

    def __init__(self, accept=True):
        self._accept = accept
        self.executed: list[str] = []
        self.executed_event = threading.Event()

    def try_accept(self, trace_id):
        if not self._accept:
            return FixAcceptance(False, "该服务正在修复其他告警，请稍后再试")
        result = AlarmAnalyseResult(trace_id=trace_id, service="svc-a")
        return FixAcceptance(True, "受理成功", result=result, record_id=7)

    def execute_fix(self, trace_id, result, record_id):
        self.executed.append(trace_id)
        self.executed_event.set()

    def try_ignore(self, trace_id):
        return True, "已忽略（判定无需处理）"

    def try_resolve(self, trace_id, note=None):
        return True, "已结案（人工已解决）" + (f"（{note}）" if note else "")

    def remind_stale_manual(self):  # 调度器注册时引用，桩需存在
        return 0


_MIN_CONFIG = textwrap.dedent("""
    feishu:
      enable: false
    analyse:
      daily_report:
        enabled: false
    storage:
      redis_url: ""
      database:
        url: ""
    server:
      log_level: WARNING
""")


@pytest.fixture(scope="module")
def action_client(tmp_path_factory):
    config_path = tmp_path_factory.mktemp("cfg_act") / "config.yaml"
    config_path.write_text(_MIN_CONFIG, encoding="utf-8")
    import app.main as main_module

    app = create_app(str(config_path))
    main_module.container.repository = _ActionRepository()
    main_module.container.fix_service = _StubFixService()
    with TestClient(app) as test_client:
        yield test_client


def test_action_retry_accepted(action_client):
    import app.main as main_module

    response = action_client.post("/api/dashboard/alarms/1/retry").json()
    assert response["code"] == 0 and "受理成功" in response["message"]
    assert main_module.container.fix_service.executed_event.wait(5)
    assert main_module.container.fix_service.executed == ["t-9"]


def test_action_ignore_and_resolve(action_client):
    ignored = action_client.post("/api/dashboard/alarms/1/ignore").json()
    assert ignored["code"] == 0 and "已忽略" in ignored["message"]
    resolved = action_client.post("/api/dashboard/alarms/1/resolve",
                                  params={"note": "已修复"}).json()
    assert resolved["code"] == 0 and "已结案" in resolved["message"]


def test_action_record_not_found(action_client):
    body = action_client.post("/api/dashboard/alarms/99/ignore").json()
    assert body["code"] == 500 and "未找到告警记录" in body["message"]


def test_action_retry_rejected_message_surfaced(tmp_path_factory):
    """防重拒绝：FixService 的拒绝文案原样透传到 message（code=500）。"""
    config_path = tmp_path_factory.mktemp("cfg_rej") / "config.yaml"
    config_path.write_text(_MIN_CONFIG, encoding="utf-8")
    import app.main as main_module

    app = create_app(str(config_path))
    main_module.container.repository = _ActionRepository()
    main_module.container.fix_service = _StubFixService(accept=False)
    with TestClient(app) as test_client:
        body = test_client.post("/api/dashboard/alarms/1/retry").json()
        assert body["code"] == 500
        assert "正在修复其他告警" in body["message"]


# ---------------------------------------------------------------- doctor 自检


def _config(**overrides) -> AppConfig:
    config = AppConfig()
    config.feishu.enable = False       # 关掉总开关：飞书凭证检查转 OFF 而非 FAIL
    config.analyse.daily_report.enabled = False
    return config


def test_doctor_unconfigured_components_are_off_or_warn():
    results = {item.name: item for item in
               run_checks(_config(), repository=None, store=None, feishu=None,
                          gitlab=None, gateway=None, check_net=False)}
    assert results["业务库 storage.database"].status == OFF
    assert results["Redis storage.redis_url"].status == WARN  # 未配置：单实例告警
    assert results["Agent 网关 analyse.external"].status == OFF
    assert results["本地 LLM analyse.llm"].status == OFF
    assert results["Web 看板鉴权"].status == WARN


def test_doctor_all_pass_with_healthy_components():
    class _Ok:
        def ping(self):
            return True

    class _Gateway:
        def fetch_root(self):
            return object()

    config = _config()
    config.storage.database.url = "jdbc:mysql://127.0.0.1:3306/db"
    config.storage.redis_url = "redis://127.0.0.1:6379/0"
    config.feishu.enable = True
    config.feishu.app_id, config.feishu.app_secret = "cli", "secret"
    config.feishu.jump_url = "http://host/fix?traceId="
    config.feishu.resolve_jump_url = "http://host/resolve?traceId="
    config.dashboard.username, config.dashboard.password = "u", "p"
    config.analyse.git.base_url, config.analyse.git.token = "http://git", "t"
    config.analyse.external.enabled = True
    config.analyse.external.url, config.analyse.external.api_key = "http://gw", "k"

    results = run_checks(config, repository=_Ok(), store=_Ok(), feishu=_Ok(),
                         gitlab=_Ok(), gateway=_Gateway(), check_net=True)
    assert all(item.status in (PASS, OFF) for item in results), \
        [(i.name, i.status) for i in results]


def test_doctor_reports_fail_for_broken_component():
    class _Broken:
        def ping(self):
            return False

    config = _config()
    config.storage.database.url = "jdbc:mysql://127.0.0.1:3306/db"
    results = run_checks(config, repository=_Broken(), store=None, feishu=None,
                         gitlab=None, gateway=None, check_net=True)
    db = next(item for item in results if item.name.startswith("业务库"))
    assert db.status == FAIL


# ---------------------------------------------------------------- 保留期配置


def test_retention_defaults_off():
    config = AppConfig()
    assert config.storage.retention_days == 0  # 默认不清理（共用表谨慎）
    assert config.storage.retention_cron == "10 3 * * *"
