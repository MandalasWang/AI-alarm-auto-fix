"""安全与合规回归：样本数据脱敏、操作审计落库（尽力而为语义）。"""
from app.config import AppConfig, Keys
from app.fixing.service import FixService
from app.models.alarm import AlarmAnalyseResult, AlarmAnalyzeRecord
from app.providers.db_context import mask_sensitive_text

# ---------------------------------------------------------------- 样本数据脱敏


def test_mask_phone_keeps_prefix_and_suffix():
    assert mask_sensitive_text("用户手机号 13812345678 已实名") == "用户手机号 138****5678 已实名"


def test_mask_id_card():
    assert mask_sensitive_text("身份证 11010119900307861X 核验") == "身份证 1101***********861X 核验"


def test_mask_email():
    assert mask_sensitive_text("联系 zhang.san@example.com") == "联系 zh***@example.com"


def test_mask_combined_in_one_text():
    text = "13812345678 / 11010119900307861X / zhang.san@example.com"
    masked = mask_sensitive_text(text)
    assert "13812345678" not in masked and "138****5678" in masked
    assert "11010119900307861X" not in masked
    assert "zhang.san@" not in masked


def test_no_false_positive_on_short_numbers():
    # 订单号/时间戳等短数字不应被误伤
    text = "order 12345678, ts 1758176400000, code 1234"
    assert mask_sensitive_text(text) == text


def test_no_false_positive_on_digits_embedded_in_longer_number():
    # 长数字中间的手机号段不切（前后紧邻数字），避免把订单号误判成手机号
    assert mask_sensitive_text("9138123456789") == "9138123456789"


def test_plain_text_untouched():
    assert mask_sensitive_text("NullPointerException at OrderService.java:77") == \
        "NullPointerException at OrderService.java:77"


# ---------------------------------------------------------------- 操作审计（尽力而为）


class _AuditRepository:
    """带 insert_audit 的假仓储（对齐 AlarmRepository 审计方法签名）。"""

    def __init__(self, record=None):
        self._record = record
        self.updated = None
        self.audits = []

    def latest_by_trace_id(self, trace_id):
        return self._record

    def update_fix_result(self, record_id, fix_status, fix_summary):
        self.updated = (record_id, fix_status, fix_summary)

    def insert_audit(self, **kwargs):
        self.audits.append(kwargs)


class _PlainRepository:
    """不带 insert_audit 的旧式假仓储（模拟缺方法时审计必须静默跳过）。"""

    def __init__(self, record=None):
        self._record = record
        self.updated = None

    def latest_by_trace_id(self, trace_id):
        return self._record

    def update_fix_result(self, record_id, fix_status, fix_summary):
        self.updated = (record_id, fix_status, fix_summary)


def _plain_repo(record):
    return _PlainRepository(record)


class _Store:
    def get(self, key):
        return None


def _record(fix_status=None):
    return AlarmAnalyzeRecord(id=7, trace_id="t-1", service_name="svc-a", fix_status=fix_status)


def _service(repository):
    return FixService(None, Keys(""), _Store(), None, None, None, repository)


def test_try_ignore_writes_audit_transition():
    repo = _AuditRepository(_record(None))
    ok, _ = _service(repo).try_ignore("t-1")
    assert ok is True
    assert len(repo.audits) == 1
    audit = repo.audits[0]
    assert audit["action"] == "ignore"
    assert audit["from_status"] is None and audit["to_status"] == "NOT_NEEDED"
    assert audit["outcome"] == "NOT_NEEDED"


def test_try_resolve_writes_audit_transition():
    repo = _AuditRepository(_record("MANUAL_PENDING"))
    ok, _ = _service(repo).try_resolve("t-1", "已处理")
    assert ok is True
    assert repo.audits[0]["action"] == "resolve"
    assert repo.audits[0]["from_status"] == "MANUAL_PENDING"
    assert repo.audits[0]["to_status"] == "MANUAL_RESOLVED"


def test_audit_failure_does_not_break_main_flow():
    class _ExplodingAuditRepo(_AuditRepository):
        def insert_audit(self, **kwargs):
            raise RuntimeError("db broken")

    repo = _ExplodingAuditRepo(_record(None))
    ok, message = _service(repo).try_ignore("t-1")  # 审计写失败不影响忽略本身
    assert ok is True and repo.updated[1] == "NOT_NEEDED"


def test_repository_without_audit_support_is_skipped():
    repo = _plain_repo(_record(None))
    ok, _ = _service(repo).try_ignore("t-1")  # 缺 insert_audit 的仓储静默跳过审计
    assert ok is True and repo.updated[1] == "NOT_NEEDED"


def test_mask_sensitive_config_default_on():
    assert AppConfig().analyse.db.mask_sensitive is True


# ---------------------------------------------------------------- 修复结果终态审计


def test_execute_fix_audits_final_status():
    from app.fixing.committer import CommitResult
    from tests.test_p0_hardening import (_Committer, _Feishu, _Generator, _Store,  # noqa: 复用既有假件
                                          _fix_output, _result)

    class _Repo(_AuditRepository):
        def __init__(self):
            super().__init__(AlarmAnalyzeRecord(id=7, trace_id="t-1", service_name="order-svc",
                                                fix_status=None))

    repo = _Repo()
    config = AppConfig.model_validate({"feishu": {"default_chat_id": "oc_1"}})
    service = FixService(config, Keys(""), _Store(), _Feishu(), _Generator(_fix_output()),
                         _Committer(), repo)
    service.execute_fix("t-1", _result(), 7)
    result_audits = [a for a in repo.audits if a["action"] == "fix_result"]
    assert result_audits and result_audits[0]["to_status"] == "FIXED"
