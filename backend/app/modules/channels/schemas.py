import re
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field

from app.modules.channels.models import ChannelAccount, ChannelStatus

_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$")


def _origin(value: str) -> str:
    value = value.strip().rstrip("/")
    if not _ORIGIN.match(value):
        raise ValueError(f"来源域名格式应为 https://example.com：{value}")
    return value.lower()


Origin = Annotated[str, AfterValidator(_origin)]


class WidgetSettings(BaseModel):
    """访客 Widget 的展示与接入设置（保存在渠道配置中）。"""

    title: str = Field(default="在线客服", min_length=1, max_length=32)
    welcome_message: str | None = Field(
        default=None, max_length=500, description="访客打开 Widget 时看到的欢迎语"
    )
    privacy_notice: str | None = Field(
        default=None, max_length=1000, description="隐私提示，访客发送第一条消息前展示"
    )
    allowed_origins: list[Origin] = Field(
        default_factory=list,
        max_length=50,
        description="允许嵌入 Widget 的网站（如 https://www.example.com）；为空表示不限制",
    )

    @classmethod
    def of(cls, config: dict[str, Any]) -> "WidgetSettings":
        return cls.model_validate(config.get("widget") or {})


class KfSettings(BaseModel):
    """微信客服账号的设置（保存在渠道配置中）。"""

    welcome_message: str | None = Field(
        default=None,
        max_length=500,
        description="客户进入会话时自动发送的欢迎语（事件响应消息，不占 48 小时内 5 条的额度）",
    )


class KfView(KfSettings):
    open_kfid: str


class ChannelOut(BaseModel):
    id: UUID
    type: str
    name: str
    public_key: str
    status: str
    routing_policy_id: UUID | None = Field(description="为空时使用租户的默认路由策略")
    widget: WidgetSettings
    identity_secret: str | None = Field(
        description="实名访客签名密钥（HMAC-SHA256）；为空表示未启用实名访客"
    )
    kf: KfView | None = Field(default=None, description="微信客服渠道的设置")
    created_at: datetime

    @classmethod
    def of(cls, channel: ChannelAccount) -> "ChannelOut":
        config = channel.config or {}
        return cls(
            id=channel.id,
            type=channel.type,
            name=channel.name,
            public_key=channel.public_key,
            status=channel.status,
            routing_policy_id=channel.routing_policy_id,
            widget=WidgetSettings.of(config),
            identity_secret=config.get("identity_secret"),
            kf=KfView.model_validate(config["kf"]) if config.get("kf") else None,
            created_at=channel.created_at,
        )


class ChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    status: ChannelStatus | None = None
    routing_policy_id: UUID | None = Field(
        default=None, description="绑定的路由策略；显式传 null 表示改用默认策略"
    )
    widget: WidgetSettings | None = None
    kf: KfSettings | None = Field(default=None, description="只适用于微信客服渠道")


class ChannelList(BaseModel):
    items: list[ChannelOut]
