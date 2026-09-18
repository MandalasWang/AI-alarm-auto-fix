"""风暴与成本防护回归：执行池满拒绝策略、服务级 LLM 分析预算、kv 原子计数。"""
import textwrap
import threading

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.analysis.report import ReportGenerator
from app.analysis.service import AlarmAnalyseService
from app.config import AppConfig, Keys
from app.core.kv import MemoryStore
from app.main import create_app
from app.models.alarm import AlarmAnalyseResult


# ---------------------------------------------------------------- 配置校验


def test_overflow_policy_validated():
    assert AppConfig().executor.overflow_policy == "inline"
    config = AppConfig.model_validate({"executor": {"overflow_policy": "reject"}})
    assert config.executor.overflow_policy == "reject"
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"executor": {"overflow_policy": "boom"}})


def test_llm_budget_default_off():
    assert AppConfig().analyse.llm.hourly_budget_per_service == 0


# ---------------------------------------------------------------- kv 原子计数


def test_memory_store_incr_counts_and_expires():
    store = MemoryStore()
    assert store.incr("k", 3600) == 1
    assert store.incr("k", 3600) == 2
    assert store.get("k") == "2"
    store.delete("k")
    assert store.incr("k", 3600) == 1  # 删除后重新从 1 计


def test_memory_store_incr_distinct_keys():
    store = MemoryStore()
    assert store.incr("a", 3600) == 1
    assert store.incr("b", 3600) == 1
    assert store.incr("a", 3600) == 2


# ---------------------------------------------------------------- 预算闸逻辑


def _analyse_service(store, budget: int) -> AlarmAnalyseService:
    config = AppConfig()
    config.analyse.llm.hourly_budget_per_service = budget
    return AlarmAnalyseService(config, Keys(""), store, None, None, None, None,
                               report_generator=None, repository=None)


def test_budget_exceeded_after_limit():
    service = _analyse_service(MemoryStore(), budget=2)
    assert service._llm_budget_exceeded("svc-a") is False
    assert service._llm_budget_exceeded("svc-a") is False
    assert service._llm_budget_exceeded("svc-a") is True  # 第 3 次：超出预算
    assert service._llm_budget_exceeded("svc-b") is False  # 其他服务独立计数


def test_budget_disabled_always_allows():
    service = _analyse_service(MemoryStore(), budget=0)
    for _ in range(5):
        assert service._llm_budget_exceeded("svc-a") is False


def test_budget_incr_failure_fails_open():
    class _BrokenStore:
        def incr(self, key, ttl_seconds):
            raise RuntimeError("kv down")

    service = _analyse_service(_BrokenStore(), budget=1)
    assert service._llm_budget_exceeded("svc-a") is False  # 保护闸故障放行


# ---------------------------------------------------------------- 规则兜底报告（跳过模型）


def test_generate_and_split_without_llm_uses_rule_fallback():
    class _MustNotCall:
        def chat(self, *args, **kwargs):
            raise AssertionError("预算触发后不应调用任何模型")

    class _Gateway:
        code_read_available = False
        commit_available = False

        def chat(self, *args, **kwargs):
            raise AssertionError("预算触发后不应调用网关")

    generator = ReportGenerator(_MustNotCall(), _Gateway())
    result = AlarmAnalyseResult(service="svc", exception_class="java.net.ConnectException",
                                core_message="Connection refused to db")
    generator.generate_and_split(result, llm_allowed=False)
    assert result.analysis  # 规则兜底报告永不为空
    assert result.problem_type == "DEPENDENCY"  # 连接类异常规则归因


# ---------------------------------------------------------------- 接收接口池满拒绝（端到端）


def test_receive_rejects_when_pool_full_under_reject_policy(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(textwrap.dedent("""
        feishu:
          enable: true
          app_id: cli_test
          app_secret: secret
          default_chat_id: oc_test
          default_card_template_id: tpl_test
        analyse:
          enabled: true
          daily_report:
            enabled: false
        storage:
          redis_url: ""
          database:
            url: ""
        executor:
          workers: 1
          queue_size: 0
          overflow_policy: reject
        server:
          log_level: WARNING
    """), encoding="utf-8")
    app = create_app(str(config_path))
    with TestClient(app) as test_client:
        import app.main as main_module

        container = main_module.container
        gate = threading.Event()
        started = threading.Event()

        def _block(alarm):
            started.set()
            gate.wait(5)  # 占住唯一的 worker（队列 0 无排队位）

        container.analyse_service.handle_alarm = _block

        def _post(trace_id):
            return test_client.post("/inner/feishu/alarm/receive", json={
                "source": "order-svc", "message": "boom", "traceId": trace_id})

        first = _post("t-1")  # 占住 worker；受理仍即时返回
        assert first.json()["code"] == 0
        assert started.wait(5)

        second = _post("t-2")  # 池满 + reject 策略 → 明确拒绝文案
        body = second.json()
        assert body["code"] == 500
        assert "队列已满" in body["message"]

        gate.set()  # 放行被阻塞的分析，收尾


def test_receive_inlines_when_pool_full_under_inline_policy(tmp_path):
    """默认 inline 策略保持既有语义：池满退化为调用线程执行，接口仍受理成功。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(textwrap.dedent("""
        feishu:
          enable: true
          app_id: cli_test
          app_secret: secret
          default_chat_id: oc_test
          default_card_template_id: tpl_test
        analyse:
          enabled: true
          daily_report:
            enabled: false
        storage:
          redis_url: ""
          database:
            url: ""
        executor:
          workers: 1
          queue_size: 0
          overflow_policy: inline
        server:
          log_level: WARNING
    """), encoding="utf-8")
    app = create_app(str(config_path))
    with TestClient(app) as test_client:
        import app.main as main_module

        container = main_module.container
        entered = threading.Event()
        release = threading.Event()

        def _block(alarm):
            entered.set()
            release.wait(5)

        container.analyse_service.handle_alarm = _block
        response = test_client.post("/inner/feishu/alarm/receive", json={
            "source": "order-svc", "message": "boom", "traceId": "t-1"})
        assert response.json()["code"] == 0  # inline：调用线程执行分析，接口不被拒绝
        release.set()
        assert entered.wait(5)
