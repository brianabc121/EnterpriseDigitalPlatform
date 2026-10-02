"""AI 公司助理的数据（设计文档 §27.5）。"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class BotStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ReplyMode(StrEnum):
    SILENT = "silent"  # 群里不说话，只记录
    MENTIONED = "mentioned"  # 被 @ 时回答


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class AssistantBot(IdMixin, TimestampMixin, TenantMixin, Base):
    """一个接入的机器人：平台、非密配置、加密的密钥（JSON）、回调令牌。"""

    __tablename__ = "assistant_bots"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    provider: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    secrets_enc: Mapped[str] = mapped_column(Text, server_default="")
    webhook_token: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12), server_default=BotStatus.ACTIVE.value)
    last_received_at: Mapped[datetime | None]
    last_sent_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    failures: Mapped[int] = mapped_column(server_default="0")
    created_by: Mapped[uuid.UUID | None]


class AssistantIdentity(IdMixin, TenantMixin, Base):
    """IM 账号与员工的对应。staff_id 为空表示出现过但还没绑定。"""

    __tablename__ = "assistant_identities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "bot_id", "external_user_id"),
        ForeignKeyConstraint(
            ["tenant_id", "bot_id"],
            ["assistant_bots.tenant_id", "assistant_bots.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "staff_id"], ["staff.tenant_id", "staff.id"], ondelete="SET NULL"
        ),
    )

    bot_id: Mapped[uuid.UUID]
    external_user_id: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(128), server_default="")
    # 私聊的会话 ID（主动通知用）；没有私聊过时为空。
    chat_id: Mapped[str | None] = mapped_column(String(128))
    staff_id: Mapped[uuid.UUID | None]
    bound_at: Mapped[datetime | None]
    last_seen_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AssistantGroup(IdMixin, TimestampMixin, TenantMixin, Base):
    """助理所在的群。reply_mode 为空时按租户设置。"""

    __tablename__ = "assistant_groups"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "bot_id", "external_chat_id"),
        ForeignKeyConstraint(
            ["tenant_id", "bot_id"],
            ["assistant_bots.tenant_id", "assistant_bots.id"],
            ondelete="CASCADE",
        ),
    )

    bot_id: Mapped[uuid.UUID]
    external_chat_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128), server_default="")
    recording: Mapped[bool] = mapped_column(server_default="true")
    reply_mode: Mapped[str | None] = mapped_column(String(12))
    extract: Mapped[bool] = mapped_column(server_default="true")
    message_count: Mapped[int] = mapped_column(server_default="0")
    last_message_at: Mapped[datetime | None]
    last_extracted_at: Mapped[datetime | None]
    extracted_candidates: Mapped[int] = mapped_column(server_default="0")


class AssistantGroupMessage(IdMixin, TenantMixin, Base):
    """记录的群消息。"""

    __tablename__ = "assistant_group_messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "group_id", "external_message_id"),
        ForeignKeyConstraint(
            ["tenant_id", "group_id"],
            ["assistant_groups.tenant_id", "assistant_groups.id"],
            ondelete="CASCADE",
        ),
    )

    group_id: Mapped[uuid.UUID]
    external_message_id: Mapped[str] = mapped_column(String(128))
    sender_external_id: Mapped[str] = mapped_column(String(128))
    sender_name: Mapped[str] = mapped_column(String(128), server_default="")
    staff_id: Mapped[uuid.UUID | None]
    text: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime]
    extracted_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AssistantMessage(IdMixin, TenantMixin, Base):
    """员工与助理的一问一答（IM 私聊和控制台，bot_id 为空是控制台）。"""

    __tablename__ = "assistant_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "staff_id"], ["staff.tenant_id", "staff.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "bot_id"],
            ["assistant_bots.tenant_id", "assistant_bots.id"],
            ondelete="CASCADE",
        ),
    )

    bot_id: Mapped[uuid.UUID | None]
    staff_id: Mapped[uuid.UUID]
    role: Mapped[str] = mapped_column(String(12))
    text: Mapped[str] = mapped_column(Text)
    tools: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
