import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKeyConstraint, String, UniqueConstraint
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
