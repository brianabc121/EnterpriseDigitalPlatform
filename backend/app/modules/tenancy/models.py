from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"  # 已注销：数据已删除，只保留租户记录和删除记录


class PlatformUserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class Tenant(IdMixin, TimestampMixin, Base):
    """平台级表（无 RLS）：租户。edp_app 只读。"""

    __tablename__ = "tenants"

    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), server_default=TenantStatus.ACTIVE.value)
    settings: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    # 注销：申请时间、计划删除数据的时间（保留期结束）、实际删除的时间。
    closing_requested_at: Mapped[datetime | None]
    deletion_scheduled_at: Mapped[datetime | None]
    purged_at: Mapped[datetime | None]


class PlatformUser(IdMixin, TimestampMixin, Base):
    """平台级表（无 RLS）：运营人员账号，与租户员工完全分开。"""

    __tablename__ = "platform_users"

    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default=PlatformUserStatus.ACTIVE.value)
    # 二次验证（TOTP）：密钥加密保存；启用时间为空表示没有启用。
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    mfa_enabled_at: Mapped[datetime | None]
