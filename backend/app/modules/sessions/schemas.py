from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.modules.conversation.models import SessionStatus, TicketSource, TicketStatus


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
