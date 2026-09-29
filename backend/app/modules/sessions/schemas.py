from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.modules.conversation.models import (
    SessionStatus,
    TicketSource,
    TicketStatus,
    TransferStatus,
)


class SessionOut(BaseModel):
    id: UUID
    room_id: UUID
    im_group_id: str
    customer_id: UUID
    customer_display_name: str
    channel_account_id: UUID
    status: SessionStatus
    assignee_id: UUID | None
    assignee_display_name: str | None
    skill_group_id: UUID | None
    priority: int
    queued_at: datetime | None
    assigned_at: datetime | None
    first_response_at: datetime | None
    closed_at: datetime | None
    close_reason: str | None
    handoff_reason: str | None = Field(
        description="转人工原因（AI 接待转人工，或 AI 优先却不能接待时的原因）"
    )
    ai_summary: str | None = Field(default=None, description="AI 转人工时写给坐席的交接摘要")
    last_customer_message_at: datetime | None
    last_agent_message_at: datetime | None
    csat: int | None = Field(default=None, description="客户满意度评分（1-5），未评价为空")
    csat_comment: str | None = None
    intent: str | None = Field(default=None, description="识别出的意图（按意图分配）")
    overflowed_at: datetime | None = Field(default=None, description="排队溢出到备用技能组的时间")
    my_role: str | None = Field(
        default=None,
        description="当前员工在会话里的身份：assignee（接待）、monitor（旁听）、assist（协助）",
    )
    created_at: datetime


class SessionPage(BaseModel):
    items: list[SessionOut]
    total: int


class SessionEventOut(BaseModel):
    id: UUID
    type: str
    actor_type: str
    actor_id: UUID | None
    payload: dict[str, Any]
    created_at: datetime


class WatcherOut(BaseModel):
    staff_id: UUID
    display_name: str
    role: str = Field(description="monitor（旁听）或 assist（协助）")
    joined_at: datetime


class SessionDetail(SessionOut):
    events: list[SessionEventOut]
    watchers: list[WatcherOut] = Field(default_factory=list, description="正在旁听、协助的员工")


class AssistRequest(BaseModel):
    staff_id: UUID = Field(description="邀请协助的员工")


class TicketOut(BaseModel):
    id: UUID
    customer_id: UUID
    customer_display_name: str
    session_id: UUID | None
    source: TicketSource
    content: str
    contact: str | None
    status: TicketStatus
    assignee_id: UUID | None
    skill_group_id: UUID | None
    created_at: datetime
    closed_at: datetime | None


class TicketPage(BaseModel):
    items: list[TicketOut]
    total: int


class Attachment(BaseModel):
    url: str = Field(max_length=2048, description="上传接口返回的 file_url")
    name: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0)
    content_type: str = Field(max_length=120)
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)


class SendMessageRequest(BaseModel):
    client_msg_id: str = Field(
        min_length=8, max_length=64, description="客户端生成的唯一 ID，重试时保持不变（幂等键）"
    )
    type: Literal["text", "image", "file"] = "text"
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    attachment: Attachment | None = Field(
        default=None, description="图片或文件（type 为 image、file 时）"
    )
    origin: Literal["manual", "quick_reply", "suggestion", "knowledge"] = Field(
        default="manual",
        description="回复的来源：手写、快捷话术、AI 建议、知识检索（用于统计 AI 建议采纳率）",
    )

    @model_validator(mode="after")
    def _check(self) -> "SendMessageRequest":
        if self.type == "text" and not self.text:
            raise ValueError("文本消息需要 text")
        if self.type != "text" and self.attachment is None:
            raise ValueError("图片或文件消息需要 attachment")
        return self


class TransferRequest(BaseModel):
    to_staff_id: UUID | None = Field(default=None, description="转给坐席（需要对方在 60 秒内接受）")
    to_group_id: UUID | None = Field(default=None, description="或转给技能组（重新排队分配）")
    note: str | None = Field(default=None, max_length=500, description="转接备注，目标坐席可见")
    force: bool = Field(
        default=False, description="强制转接，不需要对方确认（session:transfer_any）"
    )
    transfer_ownership: bool = Field(
        default=False, description="同时把客户归属转给目标坐席（customer:assign）"
    )


class TransferOut(BaseModel):
    id: UUID
    session_id: UUID
    from_staff_id: UUID | None
    to_staff_id: UUID | None
    to_group_id: UUID | None
    note: str | None
    forced: bool
    transfer_ownership: bool
    status: TransferStatus
    expires_at: datetime | None
    decided_at: datetime | None
    created_at: datetime
    from_staff_name: str | None = None
    customer_display_name: str | None = None


class TransferList(BaseModel):
    items: list[TransferOut]


class TransferAgent(BaseModel):
    staff_id: UUID
    display_name: str
    active_sessions: int
    max_concurrency: int


class TransferGroup(BaseModel):
    id: UUID
    name: str


class TransferTargets(BaseModel):
    agents: list[TransferAgent] = Field(description="在线的其他坐席")
    groups: list[TransferGroup]
