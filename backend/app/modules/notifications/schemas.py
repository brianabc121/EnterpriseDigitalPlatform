from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class NotificationOut(BaseModel):
    id: UUID
    kind: str = Field(
        description="kb_expiring 知识即将到期、lead_draft 线索待确认、kb_digest 知识周报"
    )
    title: str
    body: str | None
    link: str | None = Field(description="控制台内的页面路径")
    created_at: datetime
    read_at: datetime | None


class NotificationList(BaseModel):
    items: list[NotificationOut]
    unread: int = Field(description="未读的站内信数")
