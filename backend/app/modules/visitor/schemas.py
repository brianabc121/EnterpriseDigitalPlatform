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
