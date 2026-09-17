"""修复链路纯逻辑测试：LLM 输出解析、片段头注释、分支命名、忽略辅助逻辑。"""
from datetime import datetime

from app.fixing.committer import build_branch_name
from app.fixing.generator import SNIPPET_HEADER_PATTERN, FixGenerator
from app.fixing.service import done_fix_reject_message, is_done_status


# ---------------------------------------------------------------- LLM 输出解析

def test_parse_snake_case_keys():
    output = FixGenerator._parse(
        '{"fixed": true, "fix_type": "code", "fixed_file": "src/main/java/A.java",'
        ' "fixed_content": "class A {}", "fix_summary": "判空"}')
    assert output is not None
    assert output.fixed is True
    assert output.fixed_file == "src/main/java/A.java"
    assert output.fix_type == "code"


def test_parse_camel_case_gateway_keys():
    # 外挂 Agent 网关契约 camelCase，且无 fixed 布尔：有内容即视为已修复
    output = FixGenerator._parse(
        '{"fixedFile": "src/A.java", "fixedContent": "class A {}", "fixSummary": "x"}')
    assert output is not None
    assert output.fixed is True
    assert output.fixed_file == "src/A.java"


def test_parse_strips_code_fence():
    text = '```json\n{"fixed": false, "fix_type": "non_code", "fix_summary": "数据问题"}\n```'
    output = FixGenerator._parse(text)
    assert output is not None
    assert output.fixed is False
    assert output.fix_type == "non_code"


def test_parse_missing_content_marks_not_fixed():
    output = FixGenerator._parse('{"fixed": true, "fixed_file": "src/A.java"}')
    assert output is not None
    assert output.fixed is False  # 缺 fixed_content 不可提交


def test_parse_invalid_json_returns_none():
    assert FixGenerator._parse("抱歉，我无法修复") is None


# ---------------------------------------------------------------- 片段头注释

def test_snippet_header_pattern():
    header = "// 文件: src/main/java/com/xizhao/a/B.java (分支 prod, 出错行 92)"
    match = SNIPPET_HEADER_PATTERN.search(header)
    assert match is not None
    assert match.group(1) == "src/main/java/com/xizhao/a/B.java"
    assert match.group(2) == "prod"


# ---------------------------------------------------------------- 分支命名

def test_build_branch_name_normal():
    now = datetime(2026, 9, 16, 10, 30, 5)
    assert build_branch_name("ccd-design-project-5290", now) == "fix_bug_20260916_ccd-design-project-5290"


def test_build_branch_name_cleans_illegal_chars():
    now = datetime(2026, 9, 16)
    # 斜杠/空格等非法字符清洗掉
    assert build_branch_name("abc/def ghi", now) == "fix_bug_20260916_abcdefghi"


def test_build_branch_name_empty_fallback_hhmmss():
    now = datetime(2026, 9, 16, 10, 30, 5)
    assert build_branch_name(None, now) == "fix_bug_20260916_103005"
    assert build_branch_name("///", now) == "fix_bug_20260916_103005"


# ---------------------------------------------------------------- manual 修复类型

def test_parse_manual_fix_type():
    output = FixGenerator._parse(
        '{"fixed": false, "fix_type": "manual", "fix_summary": "需变更表结构",'
        ' "manual_steps": "1. 备份表\\n2. ALTER TABLE t ADD COLUMN c INT"}')
    assert output is not None
    assert output.fixed is False
    assert output.fix_type == "manual"
    assert "ALTER TABLE" in output.manual_steps


def test_parse_manual_steps_camel_case():
    output = FixGenerator._parse(
        '{"fixed": false, "fixType": "manual", "fixSummary": "s", "manualSteps": "1. do"}')
    assert output is not None
    assert output.manual_steps == "1. do"


# ---------------------------------------------------------------- 忽略接口辅助逻辑

def test_is_done_status():
    assert is_done_status("FIXED") is True
    assert is_done_status("NOT_NEEDED") is True
    assert is_done_status("HANDOVER") is True
    assert is_done_status("MANUAL_PENDING") is True
    assert is_done_status("FAILED") is False
    assert is_done_status(None) is False


def test_done_fix_reject_message():
    assert "已忽略" in done_fix_reject_message("NOT_NEEDED")
    assert "已成功修复" in done_fix_reject_message("FIXED")
    assert "转人工排查" in done_fix_reject_message("HANDOVER")
    assert "转人工处理" in done_fix_reject_message("MANUAL_PENDING")


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


def _service_for_ignore(store, repository):
    from app.config import Keys
    from app.fixing.service import FixService
    return FixService(None, Keys(""), store, None, None, None, repository)


def _record(fix_status=None):
    from app.models.alarm import AlarmAnalyzeRecord
    return AlarmAnalyzeRecord(id=7, trace_id="t-1", fix_status=fix_status)


def test_try_ignore_success():
    repo = _FakeRepository(_record(None))
    ok, message = _service_for_ignore(_FakeStore(), repo).try_ignore("t-1")
    assert ok is True
    assert repo.updated == (7, "NOT_NEEDED", "人工忽略：判定无需修复")


def test_try_ignore_rejects_done_status():
    for status in ("FIXED", "NOT_NEEDED", "HANDOVER", "MANUAL_PENDING"):
        repo = _FakeRepository(_record(status))
        ok, message = _service_for_ignore(_FakeStore(), repo).try_ignore("t-1")
        assert ok is False
        assert repo.updated is None
        assert "请勿重复点击" in message


def test_try_ignore_rejects_while_fixing():
    repo = _FakeRepository(_record(None))
    ok, message = _service_for_ignore(_FakeStore(fix_lock_present=True), repo).try_ignore("t-1")
    assert ok is False
    assert "自动修复中" in message
    assert repo.updated is None


def test_try_ignore_missing_record():
    repo = _FakeRepository(None)
    ok, message = _service_for_ignore(_FakeStore(), repo).try_ignore("t-1")
    assert ok is False
    assert message == "未找到告警分析结果"
