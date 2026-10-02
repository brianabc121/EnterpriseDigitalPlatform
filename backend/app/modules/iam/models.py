import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
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
        CheckConstraint(
            "diagram_direction IS NULL OR diagram_direction IN ('left', 'right', 'down')",
            name="diagram_direction",
        ),
        CheckConstraint(
            "diagram_parent_id IS NULL OR diagram_direction IS NOT NULL",
            name="diagram_branch",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "diagram_parent_id"],
            ["staff.tenant_id", "staff.id"],
            name="fk_staff_diagram_parent",
        ),
    )

    # 图形来源不表示管理归属，也不参与权限计算。
    diagram_parent_id: Mapped[uuid.UUID | None]
    diagram_direction: Mapped[str | None] = mapped_column(String(8))
    username: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default=StaffStatus.ACTIVE.value)
    # 绑定的企业微信成员（扫码登录、企业微信内免登、应用消息）。
    wecom_userid: Mapped[str | None] = mapped_column(String(64))
    # 按员工设置的页面和权限（设计文档 §31）：比角色多给的、去掉的权限；自定义的页面（为空表示按
    # 岗位）和登录后打开的页面。租户管理员不能单独调整。
    extra_permissions: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    revoked_permissions: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    menus: Mapped[list[str] | None]
    home_menu: Mapped[str | None] = mapped_column(String(16))


class StaffDiagramNode(IdMixin, TimestampMixin, TenantMixin, Base):
    """独立图形卡片；staff_id 为空时为不占席位、不可登录的待完善卡片。"""

    __tablename__ = "staff_diagram_nodes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "staff_id"),
        CheckConstraint("direction IN ('left', 'right', 'down')", name="direction"),
        ForeignKeyConstraint(
            ["tenant_id", "staff_id"],
            ["staff.tenant_id", "staff.id"],
            ondelete="CASCADE",
        ),
    )

    parent_id: Mapped[uuid.UUID | None]
    direction: Mapped[str] = mapped_column(String(8))
    staff_id: Mapped[uuid.UUID | None]


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
    # 自定义角色选择的岗位（设计文档 §25.15）；为空时按权限判断。系统角色的岗位以代码为准。
    console: Mapped[str | None] = mapped_column(String(16))


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
