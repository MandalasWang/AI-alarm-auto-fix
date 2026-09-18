"""处理闭环批次回归：人工结案（MANUAL_RESOLVED）、转人工超时催办、修复分支防撞、复发关联原修复。"""
from datetime import datetime, timedelta

from app.config import AppConfig, Keys
from app.fixing.committer import CommitResult, FixCommitter, build_branch_name
from app.fixing.service import (
    FixService, build_fixed_summary, build_manual_stale_text,
    done_fix_reject_message, fix_status_label, is_done_status,
)
from app.core.fingerprint import REPEAT_RECURRED, REPEAT_REPEATED, decide_repeat_type
from app.feishu.cards import build_alarm_card
from app.models.alarm import AlarmAnalyseResult, AlarmAnalyzeRecord
from app.reporting.stats import PeriodStats, stats_line, stats_of
from app.web.dashboard import parse_branch_name, resolve_rate

# ---------------------------------------------------------------- 复发判定兼容人工结案


def _prior(fix_status, fix_time):
    return AlarmAnalyzeRecord(id=1, trace_id="t-1", fix_status=fix_status, fix_time=fix_time)


def test_recurred_after_manual_resolved():
    now = datetime(2026, 9, 18, 12, 0)
    prior = _prior("MANUAL_RESOLVED", now - timedelta(hours=1))
    assert decide_repeat_type(prior, now) == REPEAT_RECURRED


def test_repeated_while_manual_pending():
    now = datetime(2026, 9, 18, 12, 0)
    prior = _prior("MANUAL_PENDING", None)
    assert decide_repeat_type(prior, now) == REPEAT_REPEATED


# ---------------------------------------------------------------- 结案终态语义


def test_manual_resolved_is_done_status():
    assert is_done_status("MANUAL_RESOLVED") is True
    assert "已由人工处理解决" in done_fix_reject_message("MANUAL_RESOLVED")
    assert fix_status_label("MANUAL_RESOLVED") == "人工已解决"


class _FakeStore:
    def __init__(self, fix_lock_present=False):
        self._present = fix_lock_present

    def get(self, key):
        return "1" if self._present else None


class _FakeRepository:
    def __init__(self, record=None):
        self._record = record
        self.updated = None

    def latest_by_trace_id(self, trace_id):
        return self._record

    def update_fix_result(self, record_id, fix_status, fix_summary):
        self.updated = (record_id, fix_status, fix_summary)


def _service(store, repository):
    return FixService(None, Keys(""), store, None, None, None, repository)


def _record(fix_status=None):
    return AlarmAnalyzeRecord(id=7, trace_id="t-1", fix_status=fix_status)


def test_try_resolve_success_with_note():
    repo = _FakeRepository(_record("MANUAL_PENDING"))
    ok, message = _service(_FakeStore(), repo).try_resolve("t-1", "已扩容数据库连接池")
    assert ok is True
    assert repo.updated == (7, "MANUAL_RESOLVED", "人工已处理：问题已解决（已扩容数据库连接池）")


def test_try_resolve_success_plain():
    repo = _FakeRepository(_record("HANDOVER"))
    ok, _ = _service(_FakeStore(), repo).try_resolve("t-1", None)
    assert ok is True
    assert repo.updated == (7, "MANUAL_RESOLVED", "人工已处理：问题已解决")


def test_try_resolve_allowed_from_failed_and_pending():
    for status in (None, "FAILED", "MANUAL_PENDING", "HANDOVER"):
        repo = _FakeRepository(_record(status))
        ok, _ = _service(_FakeStore(), repo).try_resolve("t-1")
        assert ok is True, status


def test_try_resolve_rejects_done_status():
    for status in ("FIXED", "NOT_NEEDED", "MANUAL_RESOLVED"):
        repo = _FakeRepository(_record(status))
        ok, message = _service(_FakeStore(), repo).try_resolve("t-1")
        assert ok is False
        assert repo.updated is None
        assert "请勿重复点击" in message


def test_try_resolve_rejects_while_fixing():
    repo = _FakeRepository(_record(None))
    ok, message = _service(_FakeStore(fix_lock_present=True), repo).try_resolve("t-1")
    assert ok is False
    assert "自动修复中" in message
    assert repo.updated is None


def test_try_resolve_missing_record_and_no_repo():
    assert _service(_FakeStore(), _FakeRepository(None)).try_resolve("t-1")[0] is False
    assert _service(_FakeStore(), None).try_resolve("t-1")[0] is False


# ---------------------------------------------------------------- fix_summary 落实际分支


def test_build_fixed_summary_contains_branch_line():
    commit = CommitResult(branch="fix_bug_20260918_t-1_101530", short_id="abc1234",
                          web_url="http://git/c/-/commit/abc1234",
                          message="[AI] fix: 自动修复告警 t-1 - 判空")
    summary = build_fixed_summary(commit, mr_url="http://git/mr/1", trace_id="t-1")
    lines = summary.splitlines()
    assert lines[0].startswith("[AI] fix:")
    assert lines[1] == "分支: fix_bug_20260918_t-1_101530"
    assert "commit: abc1234" in summary and "MR: http://git/mr/1" in summary


def test_parse_branch_name_prefers_recorded_branch():
    summary = ("[AI] fix: x\n分支: fix_bug_20260918_t-1_101530\n"
               "commit: abc1234\n查看改动: http://git/c/abc1234")
    assert parse_branch_name(summary) == "fix_bug_20260918_t-1_101530"
    assert parse_branch_name("旧记录没有分支行\ncommit: abc1234") is None
    assert parse_branch_name(None) is None


def test_resolver_uses_recorded_branch_for_mr_lookup():
    from app.web.dashboard import MergeStateResolver

    config = AppConfig()
    config.analyse.git.base_url = "https://gitlab.example.com"
    config.analyse.git.token = "token"
    config.analyse.git.projects = {"svc": "42"}
    config.analyse.git.fix_base_branch = "prod"

    seen_branches = []

    class FakeGitLab:
        def commit_branch_refs(self, project_id, sha):
            return []

        def merge_request_by_source_branch(self, project_id, branch):
            seen_branches.append(branch)
            return {"state": "opened", "web_url": "http://g/mr/1"}

    resolver = MergeStateResolver(config, FakeGitLab(), ttl_seconds=60)
    summary = "[AI] fix: x\n分支: fix_bug_20260918_retry_101530\ncommit: abc1234"
    fix_time = datetime(2026, 9, 18, 9, 0)
    info = resolver.resolve("svc", "t-1", fix_time, summary)
    # 优先用落库的实际分支（带重试后缀），而不是按 traceId+时间重算的名字
    assert seen_branches == ["fix_bug_20260918_retry_101530"]
    assert info["branch"] == "fix_bug_20260918_retry_101530"


# ---------------------------------------------------------------- 提交撞分支换名重试


class _FakeGateway:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    @property
    def commit_available(self):
        return True

    def commit_fix(self, service, branch, base_branch, file_path, content, message):
        self.calls.append(branch)
        result = self._results.pop(0)
        if result is None:
            return None
        return result


class _FakeGitLab:
    def __init__(self):
        self.calls = []

    def search_project(self, service):
        return "42"

    def ensure_branch(self, project_id, branch, ref):
        self.calls.append(branch)
        return False  # GitLab 未配置 token 时不会走到这里；保底返回失败


def _committer_config():
    config = AppConfig()
    config.analyse.git.enabled = True
    config.analyse.git.base_url = ""
    config.analyse.git.token = ""  # GitLab 直连不可用，只看网关通道行为
    return config


def test_commit_retry_with_suffixed_branch_on_collision():
    first = None  # 第一轮：远端已存在同名分支，网关 push 被拒 → None
    second = CommitResult(branch="fix_bug_20260918_t-1_101530", short_id="abc1234",
                          web_url="http://x", message="[AI] fix: y")
    gateway = _FakeGateway([first, second])
    committer = FixCommitter(_committer_config(), gateway, _FakeGitLab())
    result = committer.commit_fix("svc", "a.java", "class A {}", "fix: x", "prod", "t-1")
    assert result is not None and result.branch == "fix_bug_20260918_t-1_101530"
    # 第一次用标准分支名，重试分支名 = 标准名 + _HHMMSS 后缀
    assert gateway.calls[0] == build_branch_name("t-1")
    assert gateway.calls[1].startswith(gateway.calls[0] + "_")
    assert len(gateway.calls[1]) == len(gateway.calls[0]) + 7


def test_commit_retry_gives_up_after_second_failure():
    gateway = _FakeGateway([None, None])
    committer = FixCommitter(_committer_config(), gateway, _FakeGitLab())
    assert committer.commit_fix("svc", "a.java", "class A {}", "fix: x", "prod", "t-1") is None
    assert len(gateway.calls) == 2


# ---------------------------------------------------------------- 转人工催办


def test_build_manual_stale_text_lists_items():
    records = [
        AlarmAnalyzeRecord(id=1, trace_id="t-1", service_name="svc-a", interface_uri="/api/a",
                           fix_status="MANUAL_PENDING", create_time=datetime.now() - timedelta(hours=50)),
        AlarmAnalyzeRecord(id=2, trace_id="t-2", service_name="svc-b", interface_uri="/api/b",
                           fix_status="HANDOVER", fix_time=datetime.now() - timedelta(hours=60)),
    ]
    text = build_manual_stale_text(records, stale_hours=48)
    assert "超过 48 小时" in text
    assert "[svc-a] /api/a traceId=t-1" in text and "待人工处理" in text
    assert "[svc-b] /api/b traceId=t-2" in text and "已转人工排查" in text
    assert "「已解决」" in text  # 提示结案出口


def test_build_manual_stale_text_caps_items():
    records = [AlarmAnalyzeRecord(id=i, trace_id=f"t-{i}", service_name="svc",
                                  interface_uri="/api", fix_status="MANUAL_PENDING",
                                  create_time=datetime.now() - timedelta(hours=100))
               for i in range(25)]
    text = build_manual_stale_text(records, stale_hours=48)
    assert "另有 5 条未列出" in text


class _FakeFeishu:
    def __init__(self):
        self.sent = []

    def send_text_to_chat(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_remind_stale_manual_sends_to_chats():
    config = AppConfig()
    config.feishu.default_chat_id = "oc_default"
    config.analyse.fix_notify.stale_chat_ids = ["oc_hr"]

    stale_record = AlarmAnalyzeRecord(
        id=1, trace_id="t-1", service_name="svc-a", interface_uri="/api/a",
        fix_status="MANUAL_PENDING", create_time=datetime.now() - timedelta(hours=50))

    class _Repo:
        def list_stale_manual(self, before, limit=200):
            assert before < datetime.now()
            return [stale_record]

    feishu = _FakeFeishu()
    service = FixService(config, Keys(""), _FakeStore(), feishu, None, None, _Repo())
    count = service.remind_stale_manual()
    assert count == 1
    assert feishu.sent[0][0] == "oc_hr"  # 配置了催办群则用之
    assert "svc-a" in feishu.sent[0][1]


def test_remind_stale_manual_falls_back_to_default_chat():
    config = AppConfig()
    config.feishu.default_chat_id = "oc_default"

    class _Repo:
        def list_stale_manual(self, before, limit=200):
            return []

    feishu = _FakeFeishu()
    service = FixService(config, Keys(""), _FakeStore(), feishu, None, None, _Repo())
    assert service.remind_stale_manual() == 0
    assert feishu.sent == []


# ---------------------------------------------------------------- 统计口径：人工解决计入


def _row(fix_status, repeat_type="NORMAL"):
    return AlarmAnalyzeRecord(id=1, trace_id="t", fix_status=fix_status, repeat_type=repeat_type)


def test_stats_counts_manual_resolved_and_rate():
    rows = [_row("FIXED"), _row("MANUAL_RESOLVED"), _row("MANUAL_RESOLVED"),
            _row("NOT_NEEDED"), _row("REPEATED", "REPEATED")]
    period = stats_of(rows)
    assert period.manual_resolved == 2
    assert period.fix_notified == 2  # = 1 FIXED + 1 NOT_NEEDED；MANUAL_RESOLVED 是结案终态不属修复通知
    # 解决率 = (1 AI修复 + 2 人工解决) / (5 - 1 无需处理 - 1 重复) = 3/3 = 100%
    assert period.success_rate == 100.0
    assert "🛠 人工解决 2 条" in stats_line(period)


def test_stats_line_manual_resolved_hidden_when_zero():
    period = stats_of([_row("FIXED")])
    assert "人工解决" not in stats_line(period)


def test_resolve_rate_counts_manual_resolved():
    # 6 条：2 FIXED + 1 MANUAL_RESOLVED + 1 NOT_NEEDED + 1 待人工 + 1 REPEATED
    # 分母 = 6 - 1 - 1 - 1 = 3，分子 = 2 + 1 → 100%
    assert resolve_rate(6, 2, 1, manual=1, repeated=1, manual_resolved=1) == 100.0
    assert resolve_rate(6, 2, 1, manual=1, repeated=1, manual_resolved=0) == 66.7


# ---------------------------------------------------------------- 卡片变量


def test_alarm_card_includes_resolve_url_when_configured():
    result = AlarmAnalyseResult(service="svc", trace_id="t-1")
    card = build_alarm_card(result, "tpl", "https://host/fix?traceId=",
                            "https://host/ignore?traceId=", "https://host/resolve?traceId=")
    variables = card["data"]["template_variable"]
    assert variables["resolve_url"] == {"url": "https://host/resolve?traceId=t-1",
                                        "pc_url": "https://host/resolve?traceId=t-1"}


def test_alarm_card_omits_resolve_url_when_not_configured():
    result = AlarmAnalyseResult(service="svc", trace_id="t-1")
    card = build_alarm_card(result, "tpl", "https://host/fix?traceId=")
    assert "resolve_url" not in card["data"]["template_variable"]


def test_alarm_card_recurred_carries_prior_fix_summary():
    result = AlarmAnalyseResult(service="svc", trace_id="t-2", repeat_type="RECURRED",
                                recurred_fix_summary="[AI] fix: x\n分支: fix_bug_x\ncommit: abc")
    card = build_alarm_card(result, "tpl", "")
    variables = card["data"]["template_variable"]
    assert variables["repeat_label"] == "⚠️ 修复后复发"
    assert "fix_bug_x" in variables["recurred_fix_summary"]


def test_alarm_card_normal_has_no_recurred_summary():
    result = AlarmAnalyseResult(service="svc", trace_id="t-3", repeat_type="NORMAL")
    card = build_alarm_card(result, "tpl", "")
    variables = card["data"]["template_variable"]
    assert "repeat_label" not in variables and "recurred_fix_summary" not in variables


# ---------------------------------------------------------------- 模型与配置


def test_analyse_result_json_roundtrip_keeps_recurred_summary():
    result = AlarmAnalyseResult(service="svc", trace_id="t-1", repeat_type="RECURRED",
                                recurred_fix_summary="commit: abc")
    parsed = AlarmAnalyseResult.from_json(result.to_json())
    assert parsed.recurred_fix_summary == "commit: abc"


def test_default_config_has_stale_remind_enabled():
    config = AppConfig()
    assert config.analyse.fix_notify.stale_hours == 48
    assert config.analyse.fix_notify.stale_remind_cron == "20 10 * * *"
    assert config.feishu.resolve_jump_url == ""
