"""配置加载与校验测试。"""
import textwrap

import pytest

from app.config import AppConfig, LlmConfig, load_config
from app.core.errors import ConfigError


def _write(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


def test_load_minimal_config(tmp_path):
    config = load_config(_write(tmp_path, """
        feishu:
          app_id: cli_x
          default_chat_id: oc_1
        analyse:
          llm:
            enabled: true
            api_key: sk-x
    """))
    assert config.feishu.app_id == "cli_x"
    assert config.analyse.llm.enabled is True
    # 未配置部分取默认值
    assert config.analyse.git.default_branch == "prod"
    assert config.executor.workers == 4


def test_unknown_key_rejected(tmp_path):
    # 配置键名写错必须启动即报错，防静默失效
    with pytest.raises(ConfigError, match="配置校验失败"):
        load_config(_write(tmp_path, """
            feishu:
              app_ide: cli_x
        """))


def test_missing_file_rejected():
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config("no_such_config_file.yaml")


def test_llm_provider_defaults():
    deepseek = LlmConfig(provider="deepseek")
    assert deepseek.resolved_base_url == "https://api.deepseek.com"
    assert deepseek.resolved_model == "deepseek-chat"
    qwen = LlmConfig(provider="qwen", chat_path="/compatible-mode/v1/chat/completions")
    assert "dashscope.aliyuncs.com" in qwen.endpoint
    custom = LlmConfig(provider="custom")
    with pytest.raises(ConfigError):
        _ = custom.endpoint  # custom 未配 base_url 必须显式报错


def test_jdbc_and_plain_mysql_url():
    from app.config import DbSourceConfig

    jdbc = DbSourceConfig(url="jdbc:mysql://10.0.0.1:3306/order_db?useSSL=false")
    assert jdbc.as_pymysql_params() == ("10.0.0.1", 3306, "order_db")

    plain = DbSourceConfig(url="mysql://10.0.0.2/order_db")
    assert plain.as_pymysql_params() == ("10.0.0.2", 3306, "order_db")

    bad = DbSourceConfig(url="not-a-url")
    with pytest.raises(ConfigError):
        bad.as_pymysql_params()


def test_storage_database_shares_url_parsing():
    # 回归：storage.database 曾因缺 URL 解析方法导致启动即崩（e2e 首日发现）
    from app.config import load_config
    import textwrap

    import tempfile, os

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as fh:
        fh.write(textwrap.dedent("""
            storage:
              database:
                url: jdbc:mysql://10.0.0.3:3306/ccd-system?useSSL=false
                username: ccd
                password: p
        """))
        path = fh.name
    try:
        config = load_config(path)
        assert config.storage.database.as_pymysql_params() == ("10.0.0.3", 3306, "ccd-system")
    finally:
        os.unlink(path)


def test_model_json_camel_case_roundtrip():
    from app.models.alarm import AlarmAnalyseResult

    result = AlarmAnalyseResult(service="a", trace_id="t-1", stack_trace="at x")
    # 落库 JSON 键为 camelCase，与 Java 版互操作
    assert '"traceId"' in result.to_json()
    assert '"stackTrace"' in result.to_json()
    parsed = AlarmAnalyseResult.from_json(result.to_json())
    assert parsed is not None and parsed.trace_id == "t-1"


def test_alarm_log_accepts_camel_case_payload():
    from app.models.alarm import AlarmLog

    alarm = AlarmLog.model_validate({
        "source": "order-svc",
        "message": "boom",
        "stackTrace": "at com.xizhao.A.b(A.java:9)",
        "traceId": "t-9",
        "businessKeys": {"uri": "/api/x", "params": "{\"id\":1}"},
        "unknownField": "ignored",
    })
    assert alarm.trace_id == "t-9"
    assert alarm.business_keys["uri"] == "/api/x"
