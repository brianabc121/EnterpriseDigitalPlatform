import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ForeignKeyConstraint,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class CorpStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


class WecomCorp(IdMixin, TimestampMixin, TenantMixin, Base):
    """授权企业（代开发应用）。永久授权码即应用 Secret，加密保存。"""

    __tablename__ = "wecom_corps"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    corp_id: Mapped[str] = mapped_column(String(64))
    corp_name: Mapped[str] = mapped_column(String(128), server_default="")
    agent_id: Mapped[int | None] = mapped_column(Integer)
    permanent_code_enc: Mapped[str] = mapped_column(Text)
    auth_info: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    auth_user_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default=CorpStatus.ACTIVE.value)
    # 欢迎语等设置（见 schemas.WecomSettings）。
    settings: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # 同步情况：各类数据最近一次全量同步的时间与错误。
    sync_state: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    authorized_at: Mapped[datetime] = mapped_column(server_default=func.now())
    cancelled_at: Mapped[datetime | None]


class KfAccountStatus(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"  # 企业把客服账号收回或删除了


class WecomKfAccount(IdMixin, TimestampMixin, TenantMixin, Base):
    """微信客服账号：每个账号对应一个渠道（wecom_kf）；sync_msg 游标按账号保存。"""

    __tablename__ = "wecom_kf_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "open_kfid"),
        ForeignKeyConstraint(
            ["tenant_id", "channel_account_id"],
            ["channel_accounts.tenant_id", "channel_accounts.id"],
        ),
    )

    corp_id: Mapped[str] = mapped_column(String(64))
    open_kfid: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), server_default="")
    avatar: Mapped[str | None] = mapped_column(Text)
    channel_account_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(String(16), server_default=KfAccountStatus.ACTIVE.value)
    cursor: Mapped[str | None] = mapped_column(String(128))
    synced_at: Mapped[datetime | None]
    # 客服链接（欢迎语、网页等处放置的入口）。
    contact_url: Mapped[str | None] = mapped_column(Text)


class MemberStatus(StrEnum):
    ACTIVE = "active"
    LEFT = "left"


class WecomMember(IdMixin, TimestampMixin, TenantMixin, Base):
    """企业成员（通讯录同步）。与平台员工按 staff.wecom_userid 绑定。"""

    __tablename__ = "wecom_members"
    __table_args__ = (UniqueConstraint("tenant_id", "userid"),)

    userid: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), server_default="")
    departments: Mapped[list[int]] = mapped_column(ARRAY(Integer), server_default="{}")
    # 配置了客户联系功能（可以添加外部联系人）。
    follow: Mapped[bool] = mapped_column(server_default="false")
    status: Mapped[str] = mapped_column(String(16), server_default=MemberStatus.ACTIVE.value)
    synced_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WecomContactFollow(IdMixin, TimestampMixin, TenantMixin, Base):
    """外部联系人与添加他的成员。deleted_at 非空表示关系已解除（任一方删除了对方）。"""

    __tablename__ = "wecom_contact_follows"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_userid", "userid"),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            ondelete="CASCADE",
        ),
    )

    customer_id: Mapped[uuid.UUID]
    external_userid: Mapped[str] = mapped_column(String(64))
    userid: Mapped[str] = mapped_column(String(64))
    remark: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    tag_ids: Mapped[list[str]] = mapped_column(server_default="{}")
    add_way: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str | None] = mapped_column(String(64))
    added_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]


class WecomTag(TenantMixin, Base):
    """企业标签。"""

    __tablename__ = "wecom_tags"

    tag_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    group_id: Mapped[str | None] = mapped_column(String(64))
    group_name: Mapped[str | None] = mapped_column(String(64))
    sort: Mapped[int] = mapped_column(Integer, server_default="0")
    deleted: Mapped[bool] = mapped_column(server_default="false")
    synced_at: Mapped[datetime] = mapped_column(server_default=func.now())


class GroupChatStatus(StrEnum):
    NORMAL = "normal"
    DISMISSED = "dismissed"


class WecomGroupChat(IdMixin, TimestampMixin, TenantMixin, Base):
    """客户群。"""

    __tablename__ = "wecom_group_chats"
    __table_args__ = (UniqueConstraint("tenant_id", "chat_id"),)

    chat_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128), server_default="")
    owner_userid: Mapped[str | None] = mapped_column(String(64))
    notice: Mapped[str | None] = mapped_column(Text)
    member_count: Mapped[int] = mapped_column(Integer, server_default="0")
    status: Mapped[str] = mapped_column(String(16), server_default=GroupChatStatus.NORMAL.value)
    created_time: Mapped[datetime | None]
    synced_at: Mapped[datetime | None]


class GroupMemberType:
    MEMBER = 1  # 企业成员
    EXTERNAL = 2  # 外部联系人


class WecomGroupMember(TenantMixin, Base):
    __tablename__ = "wecom_group_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "chat_id"],
            ["wecom_group_chats.tenant_id", "wecom_group_chats.chat_id"],
            ondelete="CASCADE",
        ),
    )

    chat_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    member_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[int] = mapped_column(SmallInteger)
    name: Mapped[str | None] = mapped_column(String(128))
    customer_id: Mapped[uuid.UUID | None]
    join_time: Mapped[datetime | None]
    join_scene: Mapped[int | None] = mapped_column(SmallInteger)
    # 经"加入群聊"二维码进群时二维码的 state。
    state: Mapped[str | None] = mapped_column(String(64))


class TransferStatus(StrEnum):
    WAITING = "waiting"  # 等待接替（企业微信 24 小时后自动接替）
    SUCCESS = "success"
    FAILED = "failed"


class TransferKind(StrEnum):
    ONJOB = "onjob"  # 在职继承
    RESIGNED = "resigned"  # 离职继承


class WecomTransfer(IdMixin, TimestampMixin, TenantMixin, Base):
    """客户继承：把客户在企业微信里的添加人从原成员转给接替成员（设计 §14.3）。
    原成员已离职时走离职继承，否则走在职继承。"""

    __tablename__ = "wecom_transfers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            ondelete="CASCADE",
        ),
    )

    customer_id: Mapped[uuid.UUID]
    history_id: Mapped[uuid.UUID | None]
    external_userid: Mapped[str] = mapped_column(String(64))
    handover_userid: Mapped[str] = mapped_column(String(64))
    takeover_userid: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default=TransferStatus.WAITING.value)
    errcode: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None]
    takeover_at: Mapped[datetime | None]
    kind: Mapped[str] = mapped_column(String(16), server_default=TransferKind.ONJOB.value)


class WecomGroupTransfer(IdMixin, TimestampMixin, TenantMixin, Base):
    """客户群继承：把员工作为群主的客户群转给接替的员工（结果同步返回）。"""

    __tablename__ = "wecom_group_transfers"

    chat_id: Mapped[str] = mapped_column(String(64))
    handover_userid: Mapped[str] = mapped_column(String(64))
    takeover_userid: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    errcode: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None]


class WecomJoinWay(IdMixin, TimestampMixin, TenantMixin, Base):
    """ "加入群聊"二维码（客户群活码）。"""

    __tablename__ = "wecom_join_ways"

    config_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    chat_ids: Mapped[list[str]] = mapped_column(ARRAY(String(64)), server_default="{}")
    auto_create_room: Mapped[bool] = mapped_column(server_default="true")
    room_base_name: Mapped[str | None] = mapped_column(String(40))
    room_base_id: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(30))
    qr_code: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None]


class BroadcastKind(StrEnum):
    SINGLE = "single"  # 发给客户（员工确认后逐个发出）
    GROUP = "group"  # 发到客户群（群主确认后发出）


class BroadcastStatus(StrEnum):
    CREATED = "created"  # 企业微信已创建任务，等待员工确认发送
    FAILED = "failed"  # 创建失败
    CANCELLED = "cancelled"  # 已停止（还没发出的不再发送）


class WecomBroadcast(IdMixin, TimestampMixin, TenantMixin, Base):
    """群发任务（设计 §10.4）：不能经 API 直接给客户发消息，只能由员工或群主确认后发出。"""

    __tablename__ = "wecom_broadcasts"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    link: Mapped[dict[str, Any] | None]
    audience: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    target_count: Mapped[int] = mapped_column(Integer, server_default="0")
    msgids: Mapped[list[str]] = mapped_column(ARRAY(String(64)), server_default="{}")
    status: Mapped[str] = mapped_column(String(16))
    fail_list: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    error: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_by: Mapped[uuid.UUID | None]
    polled_at: Mapped[datetime | None]


class BroadcastResultStatus:
    UNSENT = 0
    SENT = 1
    NOT_FRIEND = 2  # 客户不是好友，发送失败
    RECEIVED_OTHER = 3  # 客户已经收到过其他群发消息，发送失败


class WecomBroadcastResult(IdMixin, TenantMixin, Base):
    __tablename__ = "wecom_broadcast_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "broadcast_id"],
            ["wecom_broadcasts.tenant_id", "wecom_broadcasts.id"],
            ondelete="CASCADE",
        ),
    )

    broadcast_id: Mapped[uuid.UUID]
    msgid: Mapped[str] = mapped_column(String(64))
    userid: Mapped[str] = mapped_column(String(64))
    external_userid: Mapped[str | None] = mapped_column(String(64))
    chat_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[int] = mapped_column(SmallInteger)
    send_time: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ZoneResultKind(StrEnum):
    SUMMARY = "summary"
    SENTIMENT = "sentiment"
    QA_CANDIDATES = "qa_candidates"
    TAGS = "tags"


class WecomZoneResult(IdMixin, TenantMixin, Base):
    """数据与智能专区里的分析程序返回的群聊分析结果（不含消息原文，设计 §10.6）。"""

    __tablename__ = "wecom_zone_results"

    chat_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    window_start: Mapped[datetime | None]
    window_end: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WecomSidebarMessage(IdMixin, TenantMixin, Base):
    """员工在聊天工具栏（侧边栏）发出的内容（设计 §10.5）。"""

    __tablename__ = "wecom_sidebar_messages"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "staff_id"], ["staff.tenant_id", "staff.id"]),
    )

    staff_id: Mapped[uuid.UUID]
    customer_id: Mapped[uuid.UUID | None]
    external_userid: Mapped[str | None] = mapped_column(String(64))
    chat_id: Mapped[str | None] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(16), server_default="manual")
    # 员工粘贴的客户问题（请求 AI 建议时填写），与发出的内容组成一问一答，用于沉淀知识。
    question: Mapped[str | None] = mapped_column(Text)
    extracted_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
