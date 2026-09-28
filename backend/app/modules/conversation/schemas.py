from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class RoomOut(BaseModel):
    id: UUID
    customer_id: UUID
    customer_display_name: str
    channel_account_id: UUID
    im_group_id: str
    last_message_at: datetime | None
    last_active_at: datetime
    created_at: datetime


class RoomPage(BaseModel):
    items: list[RoomOut]
    total: int


class MessageOut(BaseModel):
    id: UUID
    direction: str
    sender_type: str
    sender_id: UUID | None
    content_type: str
    content: dict[str, Any]
    text_plain: str | None
    im_seq: int | None
    source: str = Field(description="入库途径：webhook（发送后回调）、reconcile（对账补录）、api")
    sent_at: datetime


class MessagePage(BaseModel):
    """按发送时间倒序（最新的在前）。has_more 为 true 时，用最后一条的 id 作为 before 继续翻页。"""

    items: list[MessageOut]
    has_more: bool
