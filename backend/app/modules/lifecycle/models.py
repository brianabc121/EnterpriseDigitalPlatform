import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin


class ExportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class TenantExport(IdMixin, TenantMixin, Base):
    """一次数据导出：打包成 ZIP 放在对象存储里，保留几天后删除。"""

    __tablename__ = "tenant_exports"

    status: Mapped[str] = mapped_column(String(16), server_default=ExportStatus.PENDING.value)
    requested_by: Mapped[uuid.UUID | None]
    object_key: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(BigInteger)
    tables: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]


class SupportGrant(IdMixin, TenantMixin, Base):
    """租户授权平台运维在有效期内只读查看会话（设计文档 §7.5），查看记录写入审计日志。"""

    __tablename__ = "support_grants"

    granted_by: Mapped[uuid.UUID | None]
    reason: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TenantDeletion(IdMixin, Base):
    """平台级表：租户数据删除记录（删除证明）。digest 是删除明细的 SHA-256，可以交给客户核对。"""

    __tablename__ = "tenant_deletions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    requested_at: Mapped[datetime | None]
    scheduled_at: Mapped[datetime | None]
    purged_at: Mapped[datetime] = mapped_column(server_default=func.now())
    export_id: Mapped[uuid.UUID | None]
    counts: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
