"""应用装配冒烟测试：最小配置下整个 FastAPI 应用可启动、接口契约可响应。

不依赖外部系统（Redis 走进程内实现、业务库不配置、飞书/LLM 不启用），
验证的是装配正确性与接口层行为，不出网。
"""
import textwrap

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    config_path = tmp_path_factory.mktemp("cfg") / "config.yaml"
    config_path.write_text(textwrap.dedent("""
        feishu:
          enable: true
          app_id: cli_test
          app_secret: secret
          default_chat_id: oc_test
          default_card_template_id: tpl_test
          jump_url: "http://host/inner/feishu/alarm/callback/fix?traceId="
        analyse:
          enabled: true
          daily_report:
            enabled: false
        storage:
          redis_url: ""
          database:
            url: ""
        server:
          log_level: WARNING
    """), encoding="utf-8")
    app = create_app(str(config_path))
    with TestClient(app) as test_client:  # with 触发 lifespan：启动并停止后台任务
        yield test_client


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "UP"}


def test_receive_accepts_camel_case_payload(client):
    response = client.post("/inner/feishu/alarm/receive", json={
        "source": "order-svc",
        "message": "NullPointerException: boom",
        "stackTrace": "at com.xizhao.order.Svc.handle(Svc.java:10)",
        "traceId": "smoke-1",
    })
    # 受理即成功（分析在后台执行，外部依赖未启用时静默降级）
    body = response.json()
    assert response.status_code == 200
    assert body["code"] == 0 and body["data"] is True


def test_receive_validates_required_fields(client):
    response = client.post("/inner/feishu/alarm/receive", json={"source": "x"})
    assert response.status_code == 422  # 缺 message


def test_fix_callback_without_record(client):
    # 未配置业务库：同步校验失败并给出明确提示（而非 500）
    response = client.get("/inner/feishu/alarm/callback/fix", params={"traceId": "no-such"})
    body = response.json()
    assert body["code"] == 500
    assert "业务库" in body["message"]


def test_card_callback_unknown_action(client):
    response = client.post("/feishu/alarm/callback", json={
        "token": "", "open_message_id": "om_x", "action": {"tag": "button", "value": {"action": "xxx"}},
    })
    assert response.json()["toast"]["content"] == "未知操作"
