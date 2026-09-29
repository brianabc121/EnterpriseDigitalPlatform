import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Customer(IdMixin, TimestampMixin, TenantMixin, Base):
    """客户档案主记录。归属坐席（owner）决定坐席能否看到这个客户。"""

    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        # 复合外键保证归属坐席与客户属于同一租户。
        ForeignKeyConstraint(
            ["tenant_id", "owner_id"], ["staff.tenant_id", "staff.id"], ondelete="SET NULL"
        ),
    )

    display_name: Mapped[str] = mapped_column(String(128))
    owner_id: Mapped[uuid.UUID | None]
    source_channel: Mapped[str] = mapped_column(String(32), server_default="manual")
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(server_default="{}")
    # 敏感字段：用租户密钥加密，默认掩码展示；*_hash 是盲索引，用于按手机号、邮箱精确查找。
    phone_enc: Mapped[str | None] = mapped_column(Text)
    phone_hash: Mapped[str | None] = mapped_column(String(64))
    email_enc: Mapped[str | None] = mapped_column(Text)
    email_hash: Mapped[str | None] = mapped_column(String(64))
    company: Mapped[str | None] = mapped_column(String(128))


class CustomerIdentity(IdMixin, TimestampMixin, TenantMixin, Base):
    """客户在某个渠道上的身份（访客、微信客服 external_userid 等）。IM 用户按身份注册。"""

    __tablename__ = "customer_identities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "channel_account_id", "external_id"),
        UniqueConstraint("tenant_id", "im_user_id"),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "channel_account_id"],
            ["channel_accounts.tenant_id", "channel_accounts.id"],
        ),
    )

    customer_id: Mapped[uuid.UUID]
    channel_account_id: Mapped[uuid.UUID]
    # 渠道内的身份标识。匿名访客用身份自己的 ID。
    external_id: Mapped[str] = mapped_column(String(128))
    im_user_id: Mapped[str] = mapped_column(String(128))
    # IM 用户注册成功的时间；为空表示还没注册（访客初始化时补注册）。
    im_registered_at: Mapped[datetime | None]
    profile: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    verified: Mapped[bool] = mapped_column(server_default="false")
    last_seen_at: Mapped[datetime | None]


class OwnerChangeReason(StrEnum):
    SESSION_TRANSFER = "session_transfer"  # 会话转接时勾选了同时转移归属
    MANUAL = "manual"  # 管理员转移
    HANDOVER = "handover"  # 离职或调岗交接
    WECOM = "wecom"  # 企业微信里添加客户的员工成为默认归属坐席
    REQUEST = "request"  # 坐席申请、管理员审批通过的转移


class CustomerOwnerHistory(IdMixin, TenantMixin, Base):
    """客户归属变更记录。"""

    __tablename__ = "customer_owner_history"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
    )

    customer_id: Mapped[uuid.UUID]
    from_owner_id: Mapped[uuid.UUID | None]
    to_owner_id: Mapped[uuid.UUID | None]
    actor_id: Mapped[uuid.UUID | None]
    reason: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    # 同步到企业微信（在职继承）的状态：waiting、success、failed；为空表示没有同步。
    wecom_sync_status: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TransferRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class CustomerTransferRequest(IdMixin, TenantMixin, Base):
    """客户转移申请（设计文档 §14.1）：坐席申请变更客户的归属坐席，有分配权限的员工审批。"""

    __tablename__ = "customer_transfer_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            ondelete="CASCADE",
        ),
    )

    customer_id: Mapped[uuid.UUID]
    from_owner_id: Mapped[uuid.UUID | None]
    to_owner_id: Mapped[uuid.UUID | None]
    requested_by: Mapped[uuid.UUID | None]
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), server_default=TransferRequestStatus.PENDING.value
    )
    decided_by: Mapped[uuid.UUID | None]
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LeadDraftStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DISCARDED = "discarded"


class CustomerLeadDraft(IdMixin, TenantMixin, Base):
    """AI 接待时登记的线索（工具 save_lead_info，设计文档 §11.1）。只写白名单字段，坐席确认后
    才写入客户档案。手机号、邮箱用租户数据密钥加密保存。"""

    __tablename__ = "customer_lead_drafts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            ondelete="CASCADE",
        ),
    )

    customer_id: Mapped[uuid.UUID]
    session_id: Mapped[uuid.UUID | None]
    fields: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    status: Mapped[str] = mapped_column(String(12), server_default=LeadDraftStatus.PENDING.value)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    decided_by: Mapped[uuid.UUID | None]
    decided_at: Mapped[datetime | None]
