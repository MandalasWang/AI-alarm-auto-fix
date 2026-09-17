"""P0 生产加固回归：修复质量闸（语法预检 + LLM 自检）、MR 合拢与催办、执行链集成、指标端点。"""
import textwrap
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.config import AppConfig, FixValidationConfig, GitConfig, Keys, MergeRequestConfig
from app.fixing.committer import CommitResult
from app.fixing.generator import FixOutput
from app.fixing.merger import (
    StaleBranch, MergeRequestService, build_mr_description, build_remind_text, _parse_gitlab_time,
)
from app.fixing.service import FixService
from app.fixing.validation import FixValidator, syntax_precheck
from app.models.alarm import AlarmAnalyseResult

# ---------------------------------------------------------------- 语法预检

_VALID_JAVA = (
    "package com.xizhao.order;\n\n"
    "public class OrderService {\n"
    "    public String hello(String name) {\n"
    '        return "hi " + name;\n'
    "    }\n"
    "}\n"
)
_VALID_FILE = "src/main/java/com/xizhao/order/OrderService.java"


def test_precheck_pass():
    assert syntax_precheck(_VALID_FILE, _VALID_JAVA, "old") is None


def test_precheck_empty_content():
    assert syntax_precheck(_VALID_FILE, "  ", "old") is not None


def test_precheck_truncated_content():
    cut = _VALID_JAVA[: -len("}\n}")]
    assert syntax_precheck(_VALID_FILE, cut, "old") is not None  # 未以 } 结束/括号不配对


def test_precheck_unbalanced_braces_ignores_strings_and_comments():
    # 花括号出现在字符串与注释里：不应计入配对，正文本身配对 → 通过
    tricky = (
        "package com.x;\n"
        "public class A {\n"
        '    String s = "}{ ([)] }";\n'
        "    // stray { brace in comment\n"
        "    /* another { one */\n"
        "    char c = '{';\n"
        "}\n"
    )
    assert syntax_precheck("src/main/java/com/x/A.java", tricky, None) is None
    # 正文真少一个 } → 不通过
    assert syntax_precheck("src/main/java/com/x/A.java", tricky[:-2], None) is not None


def test_precheck_package_mismatch():
    assert "package" in (syntax_precheck(
        "src/main/java/com/xizhao/order/OrderService.java", _VALID_JAVA.replace(
            "com.xizhao.order", "com.wrong.pkg"), "old") or "")


def test_precheck_type_name_mismatch():
    renamed = _VALID_JAVA.replace("OrderService", "OtherService")
    assert "类型名" in (syntax_precheck(_VALID_FILE, renamed, "old") or "")


def test_precheck_identical_to_original():
    assert "完全相同" in (syntax_precheck(_VALID_FILE, _VALID_JAVA, _VALID_JAVA) or "")


def test_precheck_missing_package():
    assert "package" in (syntax_precheck(_VALID_FILE, "public class X {\n}\n", None) or "")


def test_mask_literals_handles_text_block_and_escape():
    from app.fixing.validation import _mask_literals
    source = (
        'String s = "a\\"{b}";\n'
        'String t = """\n{"json": 1}\n""";\n'
        "char c = '\\\\';\n"
        "int x = 1;\n"
    )
    masked = _mask_literals(source)
    assert masked.count("{") == 0 and masked.count("}") == 0


# ---------------------------------------------------------------- FixValidator 编排

class _FakeLlm:
    def __init__(self, response: str | None = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def chat(self, system_prompt, user_content, task="chat"):
        self.calls += 1
        if self._error:
            raise self._error
        return self._response or ""


class _NoGateway:
    commit_available = False


def _validator(llm, enabled=True, self_check=True):
    return FixValidator(FixValidationConfig(enabled=enabled, llm_self_check=self_check),
                        llm, _NoGateway())


def _fix_output(content=_VALID_JAVA, file=_VALID_FILE, original="old source"):
    return FixOutput(fixed=True, fixed_file=file, fixed_content=content,
                     fix_summary="判空", fix_type="code", base_branch="prod",
                     original_source=original)


def test_validator_pass():
    llm = _FakeLlm('{"valid": true, "reason": ""}')
    assert _validator(llm).validate(_fix_output()) is None
    assert llm.calls == 1


def test_validator_precheck_fails_without_llm_call():
    llm = _FakeLlm()
    reason = _validator(llm).validate(_fix_output(content=_VALID_JAVA.replace("com.xizhao", "x.y")))
    assert reason is not None and "package" in reason
    assert llm.calls == 0


def test_validator_selfcheck_reject():
    llm = _FakeLlm('{"valid": false, "reason": "第 3 行缺分号"}')
    reason = _validator(llm).validate(_fix_output())
    assert reason is not None and "缺分号" in reason


def test_validator_selfcheck_broken_channel_fails_open():
    llm = _FakeLlm(error=RuntimeError("llm down"))
    assert _validator(llm).validate(_fix_output()) is None  # 闸自身故障不误杀


def test_validator_selfcheck_unparseable_fails_open():
    llm = _FakeLlm("抱歉我看不出问题")
    assert _validator(llm).validate(_fix_output()) is None


def test_validator_disabled_skips_all():
    llm = _FakeLlm()
    assert _validator(llm, enabled=False).validate(_fix_output(content="not java")) is None
    assert llm.calls == 0


# ---------------------------------------------------------------- 执行链集成：质量闸 + MR

class _Store:
    def acquire_lock(self, key, value, ttl_seconds):
        return True

    def release_lock(self, key, expected_value=None):
        pass

    def get(self, key):
        return None


class _Repository:
    def __init__(self):
        self.updated = None

    def update_fix_result(self, record_id, fix_status, fix_summary):
        self.updated = (record_id, fix_status, fix_summary)


class _Generator:
    def __init__(self, output):
        self._output = output

    def generate(self, result):
        return self._output


class _Committer:
    def __init__(self):
        self.called = 0

    def commit_fix(self, *args):
        self.called += 1
        return CommitResult("fix_bug_20260917_t1", "abc1234", "http://git/commit/abc1234", "msg")


class _Merger:
    def __init__(self, url="http://git/mr/1"):
        self._url = url
        self.called_with = None

    def create_for_fix(self, **kwargs):
        self.called_with = kwargs
        return self._url


class _RejectValidator:
    def __init__(self, reason="圆括号不配对（3 对 2）"):
        self._reason = reason
        self.checked = None

    def validate(self, fix, service=None):
        self.checked = fix
        return self._reason


class _Feishu:
    def send_card_to_chat(self, chat_id, card):
        return "om_1"

    def send_text_to_chat(self, chat_id, text):
        self.texts = getattr(self, "texts", []) + [text]


def _config():
    return AppConfig.model_validate({
        "feishu": {"default_chat_id": "oc_1", "default_card_template_id": "tpl"},
        "analyse": {"fix_notify": {"card_template_id": "tpl"}},
    })


def _result():
    result = AlarmAnalyseResult(trace_id="t-1", service="order-svc", request_uri="/api/x")
    result.proposal = "建议判空"
    return result


def test_execute_fix_validation_reject_marks_failed_without_commit():
    committer, repository = _Committer(), _Repository()
    service = FixService(_config(), Keys(""), _Store(), _Feishu(), _Generator(_fix_output()),
                         committer, repository, validator=_RejectValidator())
    service.execute_fix("t-1", _result(), 7)
    assert committer.called == 0  # 未提交
    assert repository.updated[1] == "FAILED"
    assert "修复自检未通过" in repository.updated[2]


def test_execute_fix_pass_creates_mr_and_records_link():
    committer, repository, merger = _Committer(), _Repository(), _Merger()
    validator = _RejectValidator()
    validator._reason = None  # 通过
    feishu = _Feishu()
    service = FixService(_config(), Keys(""), _Store(), feishu, _Generator(_fix_output()),
                         committer, repository, validator=validator, merger=merger)
    service.execute_fix("t-1", _result(), 7)
    assert committer.called == 1
    assert repository.updated[1] == "FIXED"
    assert merger.called_with["branch"] == "fix_bug_20260917_t1"
    assert "MR: http://git/mr/1" in repository.updated[2]


def test_execute_fix_without_validator_keeps_legacy_behavior():
    committer, repository = _Committer(), _Repository()
    service = FixService(_config(), Keys(""), _Store(), _Feishu(), _Generator(_fix_output()),
                         committer, repository)
    service.execute_fix("t-1", _result(), 7)
    assert committer.called == 1 and repository.updated[1] == "FIXED"


# ---------------------------------------------------------------- MR 构建

def test_build_mr_description_contains_context():
    text = build_mr_description(service="order-svc", trace_id="t-1",
                                branch="fix_bug_20260917_t1", base_branch="prod",
                                fix_summary="判空保护", proposal="建议加判空")
    assert "order-svc" in text and "t-1" in text
    assert "fix_bug_20260917_t1" in text and "判空保护" in text and "建议加判空" in text


def test_build_remind_text_with_and_without_mr():
    items = [
        StaleBranch(service="a", branch="fix_bug_1", age_hours=72,
                    mr_url="http://git/mr/9", mr_title="t"),
        StaleBranch(service="b", branch="fix_bug_2", age_hours=50, mr_url=None, mr_title=None),
    ]
    text = build_remind_text(items, stale_hours=48)
    assert "fix_bug_1" in text and "http://git/mr/9" in text
    assert "尚未创建 MR" in text


def test_parse_gitlab_time():
    aware = _parse_gitlab_time("2026-09-16T10:30:05+08:00")
    assert aware is not None and aware.utcoffset() is not None
    assert _parse_gitlab_time("2026-09-16T02:30:05Z") is not None
    assert _parse_gitlab_time("garbage") is None
    assert _parse_gitlab_time(None) is None


class _GitLabForMr:
    def __init__(self):
        self.created = None

    def search_project(self, keyword):
        return None

    def create_merge_request(self, project_id, source_branch, target_branch, title,
                             description, remove_source_branch=True):
        self.created = (project_id, source_branch, target_branch, remove_source_branch)
        return True, "http://git/mr/1"


def _mr_config(**mr_overrides):
    git = GitConfig(base_url="http://git.example.cn", token="tok",
                    projects={"order-svc": "216"},
                    mr=MergeRequestConfig(**{"enabled": True, **mr_overrides}))
    return _config_with_git(git)


def _config_with_git(git):
    config = _config()
    config.analyse.git = git  # pydantic v2 默认允许属性赋值（未开 frozen）
    return config


def test_create_for_fix_returns_url():
    gitlab = _GitLabForMr()
    service = MergeRequestService(_mr_config(), gitlab, _Feishu())
    url = service.create_for_fix(service="order-svc", trace_id="t-1",
                                 branch="fix_bug_1", base_branch="prod",
                                 fix_summary="s", proposal="p")
    assert url == "http://git/mr/1"
    assert gitlab.created == ("216", "fix_bug_1", "prod", True)


def test_create_for_fix_skips_when_disabled():
    gitlab = _GitLabForMr()
    service = MergeRequestService(_mr_config(enabled=False), gitlab, _Feishu())
    assert service.create_for_fix(service="order-svc", trace_id="t-1", branch="fix_bug_1",
                                  base_branch="prod", fix_summary="s", proposal="p") is None
    assert gitlab.created is None


class _GitLabForRemind:
    def __init__(self, branches, open_mrs):
        self._branches = branches
        self._open_mrs = open_mrs

    def list_branches(self, project_id, name_prefix, max_pages=5):
        return self._branches

    def find_open_merge_request(self, project_id, source_branch):
        return self._open_mrs.get(source_branch)


def test_remind_stale_sends_text_for_stale_branches():
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    gitlab = _GitLabForRemind(
        branches=[
            {"name": "fix_bug_old", "merged": False, "commit": {"created_at": old}},
            {"name": "fix_bug_fresh", "merged": False, "commit": {"created_at": fresh}},
            {"name": "fix_bug_merged", "merged": True, "commit": {"created_at": old}},
        ],
        open_mrs={"fix_bug_old": {"web_url": "http://git/mr/9", "title": "t"}},
    )
    feishu = _Feishu()
    service = MergeRequestService(_mr_config(), gitlab, feishu)
    count = service.remind_stale()
    assert count == 1
    sent = feishu.texts[0]
    assert "fix_bug_old" in sent and "http://git/mr/9" in sent


def test_remind_stale_disabled_or_no_projects():
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    gitlab = _GitLabForRemind([{"name": "fix_bug_old", "merged": False,
                                "commit": {"created_at": old}}], {})
    disabled = MergeRequestService(_mr_config(enabled=False), gitlab, _Feishu())
    assert disabled.remind_stale() == 0
    no_projects = GitConfig(base_url="http://git", token="t", projects={},
                            mr=MergeRequestConfig())
    service = MergeRequestService(_config_with_git(no_projects), gitlab, _Feishu())
    assert service.remind_stale() == 0


# ---------------------------------------------------------------- 指标

def test_metrics_endpoints_via_testclient():
    pytest.importorskip("prometheus_client")
    from fastapi.testclient import TestClient
    from app.main import create_app
    import tempfile, os

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as fh:
        fh.write(textwrap.dedent("""
            feishu:
              enable: true
              app_id: cli_test
              app_secret: s
              default_chat_id: oc_test
              default_card_template_id: tpl
            analyse:
              daily_report:
                enabled: false
            server:
              log_level: WARNING
        """))
        path = fh.name
    try:
        app = create_app(path)
        with TestClient(app) as client:
            ready = client.get("/readyz")
            assert ready.status_code == 200
            body = ready.json()
            assert body["status"] == "READY"
            assert body["checks"]["db"] == "not_configured"  # 未配置业务库不阻断
            assert body["checks"]["kv"] == "ok"

            metrics_response = client.get("/metrics")
            assert metrics_response.status_code == 200
            assert "alarm_received_total" in metrics_response.text
            assert "llm_call_seconds" in metrics_response.text
    finally:
        os.unlink(path)


def test_executor_reject_counter():
    from prometheus_client import REGISTRY
    from app.core.executor import BoundedExecutor

    executor = BoundedExecutor(workers=1, queue_size=0, name="test-reject")
    gate = threading.Event()
    try:
        assert executor.submit(gate.wait) is True  # 占满唯一容量
        assert executor.submit(gate.wait) is False  # 池满拒绝
        before = REGISTRY.get_sample_value("alarm_executor_reject_total") or 0
        assert executor.submit(gate.wait) is False
        after = REGISTRY.get_sample_value("alarm_executor_reject_total") or 0
        assert after == before + 1
    finally:
        gate.set()
        executor.shutdown()
