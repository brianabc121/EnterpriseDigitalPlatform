from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class VisitorInitRequest(BaseModel):
    channel_key: str = Field(min_length=5, max_length=80, description="渠道公开标识（嵌入代码中）")
    visitor_token: str | None = Field(
        default=None,
        max_length=2048,
        description="上次初始化返回的访客令牌；没有或失效时按新访客处理",
    )
    page_url: str | None = Field(default=None, max_length=2048)
    referrer: str | None = Field(default=None, max_length=2048)


class IMCredentials(BaseModel):
    """访客用 OpenIM SDK 登录所需的信息。"""

    user_id: str
    token: str
    group_id: str
    conversation_id: str
    api_url: str
    ws_url: str
    platform_id: int


class VisitorInitResponse(BaseModel):
    visitor_token: str
    room_id: UUID
    im: IMCredentials


class VisitorMessageOut(BaseModel):
    id: UUID
    server_msg_id: str = Field(description="IM 消息 ID，与 Widget 从 IM 收到的消息对齐")
    sender_type: str = Field(description="customer、agent、bot 或 system")
    sender_name: str | None
    content_type: str
    text: str | None
    sent_at: datetime


class VisitorMessagePage(BaseModel):
    """按发送时间倒序（最新的在前）。"""

    items: list[VisitorMessageOut]
    has_more: bool


class VisitorSessionState(BaseModel):
    status: str = Field(
        description="none（还没有会话）、ai_serving、queued、human_serving、transferring、closed"
    )
    session_id: UUID | None = None
    queue_position: int | None = Field(default=None, description="排队中时为第几位（从 1 开始）")
    assignee_name: str | None = None
    closed_at: datetime | None = None
    csat: int | None = None
