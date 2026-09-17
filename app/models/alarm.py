"""告警报文与分析结果模型。

JSON 键名与 Java 版完全一致（camelCase），保证：
- 推送方（各 Java 服务）无需改动即可对接 /inner/feishu/alarm/receive；
- 落库的 analyze_content 可被 Java 版代码直接解析（灰度共存/回切都安全）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """camelCase JSON ↔ snake_case Python 字段的统一约定。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",  # 报文宽松：未知字段忽略（Java 版 BeanUtils 语义）
    )


class AlarmLog(ApiModel):
    """其他服务推送的告警日志（对应 Java AlarmLogBO）。

    核心只要求 source + message，其余字段用于辅助分析与卡片展示。
    """

    source: str = Field(min_length=1, description="来源服务名")
    message: str = Field(min_length=1, description="告警/异常核心内容")
    level: str | None = None
    title: str | None = None
    request_uri: str | None = None
    stack_trace: str | None = None
    trace_id: str | None = None
    time: str | None = None
    business_keys: dict[str, Any] | None = None
    raw: str | None = None
    chat_id: str | None = None
    template_id: str | None = None


class AlarmAnalyseResult(ApiModel):
    """告警分析结果（对应 Java AlarmAnalyseResult，落库/缓存/卡片三方共用）。"""

    service: str | None = None
    env: str | None = None
    level: str | None = None
    title: str | None = None
    core_message: str | None = None
    exception_class: str | None = None
    tables: str | None = None
    code_context: str | None = None
    stack_trace: str | None = None
    db_context: str | None = None
    analysis: str | None = None
    proposal: str | None = None
    trace_id: str | None = None
    time: str | None = None
    raw_message: str | None = None
    request_uri: str | None = None
    request_param: str | None = None
    url: str | None = None
    # 归因与重复/复发识别（与 Java 版 ea7f3e678 对齐）
    problem_type: str | None = None    # CODE/DEPENDENCY/MANUAL_REQUIRED/NOT_A_PROBLEM
    fingerprint: str | None = None     # md5(service|requestUri|exceptionClass|首个业务帧)
    repeat_type: str | None = None     # NORMAL/REPEATED/RECURRED
    root_id: int | None = None         # 首次出现记录 id（REPEATED/RECURRED 非空）

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, exclude_none=True)

    @classmethod
    def from_json(cls, text: str) -> "AlarmAnalyseResult | None":
        try:
            return cls.model_validate_json(text)
        except ValueError:
            return None


class AlarmAnalyzeRecord:
    """alarm_analyze_result 表行（只读查询结果，对应 Java entity）。"""

    __slots__ = (
        "id", "trace_id", "analyze_content", "service_name", "interface_uri",
        "create_time", "update_time", "fix_status", "fix_summary", "fix_time",
        "problem_type", "fingerprint", "repeat_type", "root_id",
    )

    def __init__(
        self,
        id: int | None = None,  # noqa: A002 - 对应列名
        trace_id: str | None = None,
        analyze_content: str | None = None,
        service_name: str | None = None,
        interface_uri: str | None = None,
        create_time: datetime | None = None,
        update_time: datetime | None = None,
        fix_status: str | None = None,
        fix_summary: str | None = None,
        fix_time: datetime | None = None,
        problem_type: str | None = None,
        fingerprint: str | None = None,
        repeat_type: str | None = None,
        root_id: int | None = None,
    ) -> None:
        self.id = id
        self.trace_id = trace_id
        self.analyze_content = analyze_content
        self.service_name = service_name
        self.interface_uri = interface_uri
        self.create_time = create_time
        self.update_time = update_time
        self.fix_status = fix_status
        self.fix_summary = fix_summary
        self.fix_time = fix_time
        self.problem_type = problem_type
        self.fingerprint = fingerprint
        self.repeat_type = repeat_type
        self.root_id = root_id
