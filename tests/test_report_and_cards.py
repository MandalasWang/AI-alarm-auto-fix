"""报告分段与告警卡片构建测试。"""
from app.analysis.report import ReportGenerator
from app.feishu.cards import build_alarm_card
from app.models.alarm import AlarmAnalyseResult


# ---------------------------------------------------------------- 报告分段

REPORT = (
    "【问题定位】\nservice=A, OrderService#x:92\n"
    "【报错分析】\n空指针，原因是 xxx\n"
    "【修改建议】\n在 92 行加判空"
)


def test_extract_sections():
    assert ReportGenerator._extract_section(REPORT, "问题定位", "报错分析") == \
        "service=A, OrderService#x:92"
    assert ReportGenerator._extract_section(REPORT, "报错分析", "修改建议") == \
        "空指针，原因是 xxx"
    assert ReportGenerator._extract_section(REPORT, "修改建议", None) == "在 92 行加判空"


def test_extract_no_marker_returns_full():
    assert ReportGenerator._extract_section("全文没有标记", "问题定位", "报错分析") == "全文没有标记"


def test_fallback_report_structure():
    result = AlarmAnalyseResult(service="a", request_uri="/x", exception_class="NPE",
                                core_message="boom", tables="t1")
    report = ReportGenerator._fallback_report(result)
    assert "【问题定位】" in report and "【报错分析】" in report and "【修改建议】" in report
    assert "t1" in report


# ---------------------------------------------------------------- 告警卡片

def _sample_result(**overrides) -> AlarmAnalyseResult:
    fields = dict(
        service="ccd-java-system", level="ERROR", title="ERROR 告警",
        request_uri="/inner/api", trace_id="t-123", exception_class="java.lang.NPE",
        tables="t_order", stack_trace="at com.xizhao.A.b(A.java:1)",
        analysis="分析", proposal="建议", env="test", time="2026-09-16 10:00:00",
    )
    fields.update(overrides)
    return AlarmAnalyseResult(**fields)


def test_alarm_card_basic_variables():
    card = build_alarm_card(_sample_result(), "tpl-1", "http://host/fix?traceId=")
    assert card["type"] == "template"
    assert card["data"]["template_id"] == "tpl-1"
    variables = card["data"]["template_variable"]
    assert variables["service"] == "ccd-java-system"
    assert variables["analyze"] == "分析"
    assert variables["suggest"] == "建议"
    # url 变量是分端链接对象（飞书 Url 类型变量传字符串报 200732）
    assert variables["url"] == {"url": "http://host/fix?traceId=t-123",
                                "pc_url": "http://host/fix?traceId=t-123"}


def test_alarm_card_null_safe_defaults():
    variables = build_alarm_card(_sample_result(service=None, analysis=None, proposal=None),
                                 "tpl-1", "")["data"]["template_variable"]
    assert variables["service"] == "-"
    # analysis 为空时回退 suggest（旧数据兼容）
    assert variables["analyze"] == variables["suggest"] == "-"


def test_alarm_card_budget_trims_stack_first():
    # 双引号在 JSON 序列化时翻倍膨胀，用其构造超 28KB 目标的卡片触发整体削减：
    # 堆栈最先被省略，随后 param/suggest/analyze 逐段减半
    import json

    huge = '"' * 20000
    card = build_alarm_card(_sample_result(stack_trace=huge, analysis=huge, proposal=huge,
                                           request_param=huge),
                            "tpl-1", "")
    variables = card["data"]["template_variable"]
    assert variables["track"] == "(堆栈过长已省略，完整堆栈见告警日志)"
    assert len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= 30 * 1024


# ---------------------------------------------------------------- 归因/复发卡片变量

def test_alarm_card_attribution_variables():
    from app.core.fingerprint import PROBLEM_DEPENDENCY
    result = AlarmAnalyseResult(problem_type=PROBLEM_DEPENDENCY, root_id=42,
                                repeat_type="RECURRED", trace_id="t-9")
    card = build_alarm_card(result, "tpl", "http://h/fix?traceId=",
                            "http://h/ignore?traceId=")
    variables = card["data"]["template_variable"]
    assert variables["problem_type"] == "上下游依赖问题"
    assert variables["root_id"] == "42"
    assert variables["repeat_label"] == "⚠️ 修复后复发"
    # ignor_url 与 url 同为 Url 类型对象（字符串会被飞书 200732 拒绝）
    assert variables["ignor_url"] == {"url": "http://h/ignore?traceId=t-9",
                                      "pc_url": "http://h/ignore?traceId=t-9"}


def test_alarm_card_no_ignor_url_when_not_configured():
    result = AlarmAnalyseResult(repeat_type=None)
    variables = build_alarm_card(result, "tpl", "http://h/fix?traceId=")["data"]["template_variable"]
    assert "ignor_url" not in variables
    assert "repeat_label" not in variables
    assert variables["root_id"] == ""


def test_manual_notify_variables_and_text():
    from app.feishu.cards import build_manual_notify_text, build_manual_notify_variables
    variables = build_manual_notify_variables(
        handover_type="待人工处理（SQL/配置等，需人工介入）",
        service="svc", interface="/api/x", trace_id="t-1",
        handover_time="2026-09-17 12:00:00",
        steps="1. 备份表\n2. ALTER TABLE t ADD COLUMN c INT")
    assert variables["handover_type"].startswith("待人工处理")
    assert "ALTER TABLE" in variables["steps"]
    text = build_manual_notify_text(variables)
    for token in ("告警人工处理通知", "服务名称：svc", "建议步骤："):
        assert token in text


# ---------------------------------------------------------------- 归因解析与规则兜底

def test_parse_problem_type_from_report():
    from app.analysis.report import parse_problem_type
    assert parse_problem_type("【问题定位】x\n【问题归因】DEPENDENCY：下游超时") == "DEPENDENCY"
    assert parse_problem_type("【问题归因】\nCODE") == "CODE"
    assert parse_problem_type("【问题归因】MANUAL_REQUIRED 需 SQL 变更") == "MANUAL_REQUIRED"
    assert parse_problem_type("【问题归因】NOT_A_PROBLEM") == "NOT_A_PROBLEM"
    # 无归因段不扫描全文（避免正文出现 "errorCode" 之类 token 误判）
    assert parse_problem_type("【问题定位】xx errorCode 建议修复\n【修改建议】yy") is None
    assert parse_problem_type("【问题归因】不知道") is None
    assert parse_problem_type(None) is None


def test_rule_problem_type_fallback():
    from app.analysis.report import rule_problem_type
    assert rule_problem_type(AlarmAnalyseResult()) == "MANUAL_REQUIRED"
    assert rule_problem_type(AlarmAnalyseResult(exception_class="feign.FeignException")) == "DEPENDENCY"
    assert rule_problem_type(AlarmAnalyseResult(
        exception_class="java.net.SocketTimeoutException")) == "DEPENDENCY"
    assert rule_problem_type(AlarmAnalyseResult(
        stack_trace="java.net.ConnectException: Connection refused\nat com.xizhao.a.B.c(B.java:1)")
    ) == "DEPENDENCY"
    assert rule_problem_type(AlarmAnalyseResult(
        exception_class="java.lang.NullPointerException",
        stack_trace="java.lang.NullPointerException\nat com.xizhao.a.B.c(B.java:1)")) == "CODE"
