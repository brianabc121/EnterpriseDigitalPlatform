import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
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
    # 所属会话。入库时可能还没有会话，由实时消费进程补上。
    session_id: Mapped[uuid.UUID | None]
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


class SessionStatus(StrEnum):
    AI_SERVING = "ai_serving"
    QUEUED = "queued"
    HUMAN_SERVING = "human_serving"
    TRANSFERRING = "transferring"
    CLOSED = "closed"


OPEN_STATUSES = (
    SessionStatus.AI_SERVING,
    SessionStatus.QUEUED,
    SessionStatus.HUMAN_SERVING,
    SessionStatus.TRANSFERRING,
)


class CloseReason(StrEnum):
    AGENT = "agent"  # 坐席结束
    IDLE_TIMEOUT = "idle_timeout"  # 长时间没有新消息
    LEAVE_MESSAGE = "leave_message"  # 排队超时或非工作时间，转为留言
    AI_RESOLVED = "ai_resolved"  # AI 接待结束（P3）


class Session(IdMixin, TimestampMixin, TenantMixin, Base):
    """Room 中的一次服务过程。同一个 Room 同时只有一个未结束的会话。"""

    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "room_id"], ["rooms.tenant_id", "rooms.id"]),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "channel_account_id"],
            ["channel_accounts.tenant_id", "channel_accounts.id"],
        ),
        ForeignKeyConstraint(["tenant_id", "assignee_id"], ["staff.tenant_id", "staff.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "skill_group_id"], ["skill_groups.tenant_id", "skill_groups.id"]
        ),
    )

    room_id: Mapped[uuid.UUID]
    customer_id: Mapped[uuid.UUID]
    channel_account_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(String(16))
    assignee_id: Mapped[uuid.UUID | None]
    skill_group_id: Mapped[uuid.UUID | None]
    # 排队优先级：数值越大越优先（VIP、投诉等）。
    priority: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    queued_at: Mapped[datetime | None]
    assigned_at: Mapped[datetime | None]
    first_response_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    close_reason: Mapped[str | None] = mapped_column(String(32))
    handoff_reason: Mapped[str | None] = mapped_column(String(64))
    ai_summary: Mapped[str | None] = mapped_column(Text)
    csat: Mapped[int | None] = mapped_column(SmallInteger)
    csat_comment: Mapped[str | None] = mapped_column(Text)
    last_customer_message_at: Mapped[datetime | None]
    last_agent_message_at: Mapped[datetime | None]


class SessionEvent(IdMixin, TenantMixin, Base):
    """会话事件流水：创建、排队、分配、转接、结束等，供追溯和报表使用。"""

    __tablename__ = "session_events"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"]),
    )

    session_id: Mapped[uuid.UUID]
    type: Mapped[str] = mapped_column(String(32))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[uuid.UUID | None]
    payload: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TicketSource(StrEnum):
    QUEUE_TIMEOUT = "queue_timeout"
    OFF_HOURS = "off_hours"
    VISITOR = "visitor"


class TicketStatus(StrEnum):
    OPEN = "open"
    DONE = "done"


class Ticket(IdMixin, TimestampMixin, TenantMixin, Base):
    """留言与跟进任务。"""

    __tablename__ = "tickets"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        ForeignKeyConstraint(["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"]),
        ForeignKeyConstraint(["tenant_id", "assignee_id"], ["staff.tenant_id", "staff.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "skill_group_id"], ["skill_groups.tenant_id", "skill_groups.id"]
        ),
    )

    customer_id: Mapped[uuid.UUID]
    session_id: Mapped[uuid.UUID | None]
    source: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), server_default=TicketStatus.OPEN.value)
    assignee_id: Mapped[uuid.UUID | None]
    skill_group_id: Mapped[uuid.UUID | None]
    closed_at: Mapped[datetime | None]
