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
    session_id: UUID | None
    direction: str
    sender_type: str
    sender_id: UUID | None
    sender_name: str | None = Field(
        default=None, description="坐席消息为坐席姓名，智能客服消息为设置的名称"
    )
    content_type: str
    content: dict[str, Any]
    text_plain: str | None
    channel_msg_id: str | None = Field(description="IM 消息 ID（OpenIM serverMsgID）")
    client_msg_id: str | None
    im_seq: int | None
    source: str = Field(
        description="入库途径：webhook（发送后回调）、reconcile（对账补录）、api、channel（外部渠道拉取）"
    )
    send_status: str | None = Field(
        default=None, description="经 API 发出的消息：pending、sent、failed；其他消息为空"
    )
    send_error: str | None = Field(
        default=None, description="发送失败的原因（如已超过微信客服 48 小时回复窗口）"
    )
    sent_at: datetime


class MessagePage(BaseModel):
    """按发送时间倒序（最新的在前）。has_more 为 true 时，用最后一条的 id 作为 before 继续翻页。"""

    items: list[MessageOut]
    has_more: bool
