from enum import StrEnum
from typing import Any

from sqlalchemy import String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


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


class PlatformUser(IdMixin, TimestampMixin, Base):
    """平台级表（无 RLS）：运营人员账号，与租户员工完全分开。"""

    __tablename__ = "platform_users"

    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default=PlatformUserStatus.ACTIVE.value)
