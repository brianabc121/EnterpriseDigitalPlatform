from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class VisitorIdentity(BaseModel):
    """实名访客：网站后端用渠道的签名密钥为当前登录用户签名。

    signature = HMAC-SHA256(identity_secret, f"{external_id}:{name}:{timestamp}") 的十六进制，
    name 为空时写空字符串；timestamp 为 Unix 秒，与服务器时间相差不超过 10 分钟。
    """

    external_id: str = Field(min_length=1, max_length=100, description="网站自己的用户 ID")
    name: str | None = Field(default=None, max_length=64)
    timestamp: int
    signature: str = Field(min_length=64, max_length=64)


class VisitorInitRequest(BaseModel):
    channel_key: str = Field(min_length=5, max_length=80, description="渠道公开标识（嵌入代码中）")
    visitor_token: str | None = Field(
        default=None,
        max_length=2048,
        description="上次初始化返回的访客令牌；没有或失效时按新访客处理",
    )
    identity: VisitorIdentity | None = Field(
        default=None, description="实名访客的签名身份；提供时优先于 visitor_token"
    )
    embed_origin: str | None = Field(
        default=None, max_length=255, description="嵌入 Widget 的网站来源（渠道限制了来源时校验）"
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


class WidgetView(BaseModel):
    title: str
    welcome_message: str | None
    privacy_notice: str | None


class VisitorInitResponse(BaseModel):
    visitor_token: str
    room_id: UUID
    customer_name: str
    verified: bool = Field(description="是否为实名访客")
    widget: WidgetView
    im: IMCredentials


class AttachmentOut(BaseModel):
    url: str
    name: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None


class VisitorMessageOut(BaseModel):
    id: UUID
    server_msg_id: str = Field(description="IM 消息 ID，与 Widget 从 IM 收到的消息对齐")
    sender_type: str = Field(description="customer、agent、bot 或 system")
    sender_name: str | None
    content_type: str
    text: str | None
    attachment: AttachmentOut | None = Field(default=None, description="图片或文件")
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


class CsatRequest(BaseModel):
    session_id: UUID
    score: int = Field(ge=1, le=5, description="1 到 5 分")
    comment: str | None = Field(default=None, max_length=500)


class LeaveMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    contact: str | None = Field(default=None, max_length=128, description="手机号、邮箱等联系方式")
