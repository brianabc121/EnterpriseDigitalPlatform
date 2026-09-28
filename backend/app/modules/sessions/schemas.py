from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

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
    handoff_reason: str | None
    last_customer_message_at: datetime | None
    last_agent_message_at: datetime | None
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


class SessionDetail(SessionOut):
    events: list[SessionEventOut]


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


class SendMessageRequest(BaseModel):
    client_msg_id: str = Field(
        min_length=8, max_length=64, description="客户端生成的唯一 ID，重试时保持不变（幂等键）"
    )
    text: str = Field(min_length=1, max_length=4000)


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
