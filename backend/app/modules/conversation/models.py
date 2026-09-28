import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, ForeignKeyConstraint, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Room(IdMixin, TimestampMixin, TenantMixin, Base):
    """客户身份的长期对话容器，对应一个 OpenIM 服务群。"""

    __tablename__ = "rooms"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "identity_id"),
        UniqueConstraint("tenant_id", "im_group_id"),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["customer_identities.tenant_id", "customer_identities.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "channel_account_id"],
            ["channel_accounts.tenant_id", "channel_accounts.id"],
        ),
    )

    customer_id: Mapped[uuid.UUID]
    identity_id: Mapped[uuid.UUID]
    channel_account_id: Mapped[uuid.UUID]
    im_group_id: Mapped[str] = mapped_column(String(128))
    # OpenIM 服务群建好的时间；为空表示还没建（访客初始化时补建）。
    im_ready_at: Mapped[datetime | None]
    # 对账游标：不大于它的 seq 都已核对过。
    synced_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")
    last_message_at: Mapped[datetime | None]
    last_active_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Direction(StrEnum):
    IN = "in"
    OUT = "out"


class SenderType(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    BOT = "bot"
    SYSTEM = "system"


class MessageSource(StrEnum):
    WEBHOOK = "webhook"
    RECONCILE = "reconcile"
    API = "api"


class Message(IdMixin, TimestampMixin, TenantMixin, Base):
    """统一的消息归档。"""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "channel_account_id", "channel_msg_id", name="uq_messages_channel_msg"
        ),
        ForeignKeyConstraint(["tenant_id", "room_id"], ["rooms.tenant_id", "rooms.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "channel_account_id"],
            ["channel_accounts.tenant_id", "channel_accounts.id"],
        ),
    )

    room_id: Mapped[uuid.UUID]
    channel_account_id: Mapped[uuid.UUID]
    direction: Mapped[str] = mapped_column(String(8))
    sender_type: Mapped[str] = mapped_column(String(16))
    sender_id: Mapped[uuid.UUID | None]
    content_type: Mapped[str] = mapped_column(String(16))
    content: Mapped[dict[str, Any]]
    text_plain: Mapped[str | None] = mapped_column(Text)
    channel_msg_id: Mapped[str | None] = mapped_column(String(128))
    client_msg_id: Mapped[str | None] = mapped_column(String(128))
    im_seq: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16))
    sent_at: Mapped[datetime]
