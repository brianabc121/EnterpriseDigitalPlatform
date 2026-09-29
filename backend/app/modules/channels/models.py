import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class ChannelType(StrEnum):
    WEB = "web"
    WECOM_KF = "wecom_kf"  # 微信客服账号
    WECOM_CONTACT = "wecom_contact"  # 企业微信客户联系（只同步客户，不能经 API 发消息）


class ChannelStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ChannelAccount(IdMixin, TimestampMixin, TenantMixin, Base):
    """渠道账号：一个接入点（某个官网 Widget、某个微信客服账号……）。"""

    __tablename__ = "channel_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "routing_policy_id"],
            ["routing_policies.tenant_id", "routing_policies.id"],
        ),
    )

    type: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    # 公开标识，形如 "{租户代码}.{随机串}"：嵌入网页，用来找到租户和渠道，不是凭证。
    public_key: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(16), server_default=ChannelStatus.ACTIVE.value)
    config: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # 为空时使用租户的默认路由策略。
    routing_policy_id: Mapped[uuid.UUID | None]
