"""告警指纹与重复/复发判定、归因映射测试（与 Java 版 TestAttribution 对应场景一致）。"""
from datetime import datetime, timedelta

from app.core import fingerprint as fp
from app.models.alarm import AlarmAnalyzeRecord

_STACK = ("java.lang.Thread.run(Thread.java:833)\n"
          "at com.xizhao.foo.Bar.doIt(Bar.java:42)\n"
          "at com.xizhao.foo.Baz.other(Baz.java:7)")


def _record(fix_status=None, fix_time=None, root_id=None) -> AlarmAnalyzeRecord:
    return AlarmAnalyzeRecord(fix_status=fix_status, fix_time=fix_time, root_id=root_id)


# ---------------------------------------------------------------- 指纹

def test_fingerprint_stable_and_sensitive():
    a = fp.fingerprint("svc", "/api/x", "java.lang.NullPointerException", _STACK)
    assert a == fp.fingerprint("svc", "/api/x", "java.lang.NullPointerException", _STACK)
    assert len(a) == 32 and a == a.lower()
    assert a != fp.fingerprint("svc2", "/api/x", "java.lang.NullPointerException", _STACK)
    assert a != fp.fingerprint("svc", "/api/y", "java.lang.NullPointerException", _STACK)
    assert a != fp.fingerprint("svc", "/api/x", "java.lang.IllegalStateException", _STACK)
    # 指纹不含行号：同方法行号平移指纹不变；换方法才变
    assert a == fp.fingerprint("svc", "/api/x", "java.lang.NullPointerException",
                               "at com.xizhao.foo.Bar.doIt(Bar.java:99)")
    assert a != fp.fingerprint("svc", "/api/x", "java.lang.NullPointerException",
                               "at com.xizhao.foo.Baz.other(Baz.java:7)")


def test_fingerprint_all_blank_is_none():
    assert fp.fingerprint(None, None, None, None) is None
    assert fp.fingerprint("", " ", "", "   \n  ") is None


def test_fingerprint_from_stack_only():
    value = fp.fingerprint(None, None, None, _STACK)
    assert value is not None


def test_first_business_frame():
    assert fp.first_business_frame(
        "java.lang.Thread.run(Thread.java:833)\nat com.xizhao.foo.Bar.doIt(Bar.java:42)"
    ) == "com.xizhao.foo.Bar.doIt"
    assert fp.first_business_frame("at java.base/java.lang.Exception(x:1)") is None
    assert fp.first_business_frame(None) is None
    assert fp.first_business_frame("java.lang.NullPointerException: boom") is None
    # 行号缺省可匹配
    assert fp.first_business_frame("at com.xizhao.a.B.c(B.java)") == "com.xizhao.a.B.c"


# ---------------------------------------------------------------- 重复/复发判定

def test_decide_repeat_type():
    now = datetime(2026, 9, 17, 12, 0, 0)
    assert fp.decide_repeat_type(None, now) == fp.REPEAT_NORMAL
    recurred = _record("FIXED", now - timedelta(minutes=1))
    assert fp.decide_repeat_type(recurred, now) == fp.REPEAT_RECURRED
    clock_skew = _record("FIXED", now + timedelta(minutes=1))
    assert fp.decide_repeat_type(clock_skew, now) == fp.REPEAT_REPEATED
    for status in (None, "FAILED", "NOT_NEEDED", "HANDOVER", "MANUAL_PENDING"):
        assert fp.decide_repeat_type(_record(status), now) == fp.REPEAT_REPEATED


# ---------------------------------------------------------------- 归因映射

def test_fix_status_of_problem_type():
    assert fp.fix_status_of_problem_type(fp.PROBLEM_CODE) is None
    assert fp.fix_status_of_problem_type(None) is None
    assert fp.fix_status_of_problem_type("garbage") is None
    assert fp.fix_status_of_problem_type(fp.PROBLEM_DEPENDENCY) == "HANDOVER"
    assert fp.fix_status_of_problem_type(fp.PROBLEM_MANUAL_REQUIRED) == "MANUAL_PENDING"
    assert fp.fix_status_of_problem_type(fp.PROBLEM_NOT_A_PROBLEM) == "NOT_NEEDED"


def test_problem_type_label():
    assert fp.problem_type_label(fp.PROBLEM_CODE) == "代码问题"
    assert fp.problem_type_label(fp.PROBLEM_DEPENDENCY) == "上下游依赖问题"
    assert fp.problem_type_label(fp.PROBLEM_MANUAL_REQUIRED) == "需人工介入"
    assert fp.problem_type_label(fp.PROBLEM_NOT_A_PROBLEM) == "无需处理"
    assert fp.problem_type_label(None) == "-"
