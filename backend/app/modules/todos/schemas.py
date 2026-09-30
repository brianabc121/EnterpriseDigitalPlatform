import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.modules.todos.models import Priority, RejectReason, TodoSource, TodoStatus

View = Literal["pending", "mine", "pool", "assigned", "all"]
DueFilter = Literal["overdue", "today", "soon"]


class FieldValue(BaseModel):
    key: str
    label: str
    value: str
    sensitive: bool = False


class TodoOut(BaseModel):
    id: uuid.UUID
    no: str
    type_id: uuid.UUID
    type_code: str
    type_name: str
    title: str
    detail: str
    fields: list[FieldValue]
    customer_id: uuid.UUID | None
    customer_name: str | None
    session_id: uuid.UUID | None
    order_id: uuid.UUID | None
    source: TodoSource
    confidence: float | None
    priority: Priority
    status: TodoStatus
    assignee_id: uuid.UUID | None
    assignee_name: str | None
    skill_group_id: uuid.UUID | None
    skill_group_name: str | None
    assigned_by: uuid.UUID | None
    expected_at: datetime | None
    due_at: datetime | None
    overdue: bool = Field(description="截止时间已过、仍未完成")
    respond_due_at: datetime | None
    first_response_at: datetime | None
    confirmed_at: datetime | None
    confirmed_by: uuid.UUID | None
    closed_at: datetime | None
    result: str | None
    reject_reason: RejectReason | None
    close_note: str | None
    progress_note: str | None = Field(description="客户可见的进度说明")
    nudge_count: int
    evidence_count: int
    created_by_type: str
    created_by: uuid.UUID | None
    created_by_name: str | None
    created_at: datetime
    updated_at: datetime


class TodoPage(BaseModel):
    items: list[TodoOut]
    total: int


class TodoEventOut(BaseModel):
    id: uuid.UUID
    type: str
    actor_type: str
    actor_id: uuid.UUID | None
    actor_name: str | None
    payload: dict[str, Any]
    created_at: datetime


class EvidenceMessage(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID | None
    sender_type: str
    text: str
    sent_at: datetime


class TodoAllowed(BaseModel):
    """当前员工能对这条待办做的操作（前端据此显示按钮）。"""

    confirm: bool
    handle: bool
    claim: bool
    assign: bool


class TodoDetail(TodoOut):
    type_fields: list[dict[str, Any]] = Field(description="类型的字段定义（编辑表单用）")
    events: list[TodoEventOut]
    evidence: list[EvidenceMessage]
    allowed: TodoAllowed


class AssigneeOption(BaseModel):
    id: uuid.UUID
    name: str


class AssigneeOptions(BaseModel):
    """可以分派、转交待办的对象：启用状态的员工和技能组。"""

    staff: list[AssigneeOption]
    groups: list[AssigneeOption]


class TodoCounts(BaseModel):
    pending: int = Field(description="等我确认的（能分派待办的人是数据范围内全部待确认）")
    mine: int = Field(description="我的未完成待办")
    due_today: int
    overdue: int
    pool: int = Field(description="我能认领的")


class TodoCreate(BaseModel):
    type_id: uuid.UUID
    title: str = Field(min_length=1, max_length=100)
    detail: str = Field(default="", max_length=4000)
    fields: dict[str, str] = Field(default_factory=dict)
    customer_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    evidence_message_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    expected_at: datetime | None = None
    due_at: datetime | None = None
    priority: Priority | None = None
    assignee_id: uuid.UUID | None = Field(
        default=None, description="处理人；与 skill_group_id 都为空时按类型的规则分派"
    )
    skill_group_id: uuid.UUID | None = Field(default=None, description="放进这个技能组的待认领池")
    source: Literal["staff", "copilot", "sidebar"] = "staff"


class TodoUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=100)
    detail: str | None = Field(default=None, max_length=4000)
    fields: dict[str, str] | None = Field(
        default=None, description="只修改给出的字段，空字符串表示清除"
    )
    priority: Priority | None = None
    expected_at: datetime | None = None
    progress_note: str | None = Field(default=None, max_length=500)


class ConfirmRequest(BaseModel):
    """确认时可以先修改类型、标题、字段、处理人和截止时间。"""

    type_id: uuid.UUID | None = None
    title: str | None = Field(default=None, min_length=1, max_length=100)
    detail: str | None = Field(default=None, max_length=4000)
    fields: dict[str, str] | None = None
    priority: Priority | None = None
    assignee_id: uuid.UUID | None = None
    skill_group_id: uuid.UUID | None = None
    due_at: datetime | None = None


class RejectRequest(BaseModel):
    reason: RejectReason
    note: str | None = Field(default=None, max_length=500)


class MergeRequest(BaseModel):
    target_id: uuid.UUID = Field(description="并入这位客户已有的待办")


class AssignRequest(BaseModel):
    assignee_id: uuid.UUID | None = None
    skill_group_id: uuid.UUID | None = Field(
        default=None, description="不指定处理人时放进这个技能组的待认领池"
    )
    note: str | None = Field(default=None, max_length=500)


class RescheduleRequest(BaseModel):
    due_at: datetime
    reason: str = Field(min_length=1, max_length=500)


class DoneRequest(BaseModel):
    result: str = Field(min_length=1, max_length=2000)
    notify_customer: bool = False
    notice: str | None = Field(
        default=None, max_length=1000, description="通知客户的内容；为空时用类型的完成通知模板"
    )


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class NoteRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class CommentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    mentions: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class NotifyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class NotifyResult(BaseModel):
    status: Literal["sent", "manual", "unreachable"]
    channel: str | None
    reason: str | None


class BatchRequest(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    action: Literal["confirm", "reject"]
    reason: RejectReason | None = None
    note: str | None = Field(default=None, max_length=500)


class BatchFailure(BaseModel):
    id: uuid.UUID
    error: str


class BatchResult(BaseModel):
    done: list[uuid.UUID]
    failed: list[BatchFailure]


class RevealOut(BaseModel):
    fields: list[FieldValue]


class ExtractRequest(BaseModel):
    """从选中的消息或粘贴的文字预填待办（不保存）。"""

    session_id: uuid.UUID | None = None
    message_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    text: str | None = Field(default=None, max_length=4000)


class TodoSuggestion(BaseModel):
    type_id: uuid.UUID
    type_code: str
    type_name: str
    title: str
    detail: str
    fields: dict[str, str]
    missing: list[str] = Field(description="还缺少的必填字段")
    expected_at: datetime | None
    confidence: float
    promised_by_agent: bool
    evidence_message_ids: list[uuid.UUID]


class ExtractResult(BaseModel):
    items: list[TodoSuggestion]


# ---- 待办类型 ----


class TodoFieldSpec(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    label: str = Field(min_length=1, max_length=32)
    type: Literal["text", "number", "date", "option", "phone", "email", "address", "file"] = "text"
    required: bool = False
    sensitive: bool = False
    options: list[str] | None = None


AssignStep = Literal["session_agent", "owner", "channel_group", "skill_group", "staff"]


def _owner_first() -> list[AssignStep]:
    return ["owner"]


class AssignRule(BaseModel):
    steps: list[AssignStep] = Field(default_factory=_owner_first, max_length=5)
    skill_group_id: uuid.UUID | None = None
    staff_id: uuid.UUID | None = None
    group_mode: Literal["least_loaded", "pool"] = "pool"


class TodoTypeWrite(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    name: str = Field(min_length=1, max_length=32)
    ai_hint: str = Field(default="", max_length=500)
    examples: list[str] = Field(default_factory=list, max_length=5)
    fields: list[TodoFieldSpec] = Field(default_factory=list, max_length=20)
    assign_rule: AssignRule = Field(default_factory=AssignRule)
    priority: Priority = Priority.NORMAL
    sla_response_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 30)
    sla_resolve_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 90)
    sla_resolve_days: int | None = Field(default=None, ge=1, le=90)
    remind_before_minutes: int = Field(default=120, ge=0, le=60 * 24 * 7)
    escalate_after_minutes: int = Field(default=240, ge=0, le=60 * 24 * 30)
    ai_enabled: bool = True
    handoff: bool = False
    notify_supervisor: bool = False
    promise_text: str = Field(default="", max_length=500)
    done_template: str = Field(default="", max_length=1000)
    enabled: bool = True
    sort: int = Field(default=100, ge=0, le=10000)


class TodoTypeOut(TodoTypeWrite):
    id: uuid.UUID
    preset: bool
    system: bool
    created_at: datetime
    updated_at: datetime


class TodoTypeList(BaseModel):
    items: list[TodoTypeOut]


# ---- 访客端：服务进度 ----


class VisitorTodoOut(BaseModel):
    no: str
    type_name: str
    title: str
    status: str
    status_label: str
    due_at: datetime | None
    progress_note: str | None
    created_at: datetime
    closed_at: datetime | None


class VisitorTodoList(BaseModel):
    enabled: bool
    items: list[VisitorTodoOut]
