import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKeyConstraint, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class StaffStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class Staff(IdMixin, TimestampMixin, TenantMixin, Base):
    """租户员工（坐席、管理员等）。"""

    __tablename__ = "staff"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "username"),
    )

    username: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default=StaffStatus.ACTIVE.value)


class Role(IdMixin, TimestampMixin, TenantMixin, Base):
    """角色 = 一组权限点。系统角色在开通租户时创建。"""

    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "code"),
    )

    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))
    permissions: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    is_system: Mapped[bool] = mapped_column(server_default=text("false"))


class StaffRole(TenantMixin, Base):
    __tablename__ = "staff_roles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "staff_id"], ["staff.tenant_id", "staff.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "role_id"], ["roles.tenant_id", "roles.id"], ondelete="CASCADE"
        ),
    )

    staff_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    role_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)


class RefreshToken(TenantMixin, Base):
    """员工的刷新令牌。id 即 JWT 的 jti；同一次登录产生的令牌属于同一个 family。"""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "staff_id"], ["staff.tenant_id", "staff.id"], ondelete="CASCADE"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    staff_id: Mapped[uuid.UUID]
    family_id: Mapped[uuid.UUID]
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    replaced_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
