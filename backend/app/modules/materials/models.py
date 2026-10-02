"""企业资料（设计文档 §36.7）：文件夹、资料、分享链接。文件本身在阿里云 OSS 上。"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class MaterialKind(StrEnum):
    VIDEO = "video"
    DOCUMENT = "document"
    IMAGE = "image"
    TEXT = "text"  # 在平台里写的文字资料（Markdown），也存在 OSS 上
    OTHER = "other"


class MaterialStatus(StrEnum):
    UPLOADING = "uploading"
    READY = "ready"
    BLOCKED = "blocked"  # 含有病毒，文件已删除


class ScanStatus(StrEnum):
    PENDING = "pending"
    CLEAN = "clean"
    INFECTED = "infected"
    SKIPPED = "skipped"  # 超过扫描上限，没有扫描
    MISSING = "missing"  # 扫描时 OSS 上没有这个文件


KIND_LABELS: dict[str, str] = {
    MaterialKind.VIDEO: "视频",
    MaterialKind.DOCUMENT: "文档",
    MaterialKind.IMAGE: "图片",
    MaterialKind.TEXT: "文字资料",
    MaterialKind.OTHER: "其他文件",
}


class MaterialFolder(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "material_folders"

    parent_id: Mapped[uuid.UUID | None]
    name: Mapped[str] = mapped_column(String(64))
    sort: Mapped[int] = mapped_column(server_default="0")
    created_by: Mapped[uuid.UUID | None]


class Material(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "materials"

    folder_id: Mapped[uuid.UUID | None]
    kind: Mapped[str] = mapped_column(String(12))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(32)), server_default="{}")
    file_name: Mapped[str] = mapped_column(String(255))
    ext: Mapped[str] = mapped_column(String(16))
    content_type: Mapped[str] = mapped_column(String(128))
    size: Mapped[int] = mapped_column(BigInteger)
    object_key: Mapped[str] = mapped_column(Text)
    upload_id: Mapped[str | None] = mapped_column(String(128))
    part_size: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(12), server_default=MaterialStatus.UPLOADING.value)
    scan_status: Mapped[str | None] = mapped_column(String(12))
    scan_signature: Mapped[str | None] = mapped_column(String(200))
    scanned_at: Mapped[datetime | None]
    excerpt: Mapped[str | None] = mapped_column(Text)
    views: Mapped[int] = mapped_column(server_default="0")
    downloads: Mapped[int] = mapped_column(server_default="0")
    created_by: Mapped[uuid.UUID | None]
    updated_by: Mapped[uuid.UUID | None]
    uploaded_at: Mapped[datetime | None]


class MaterialShare(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "material_shares"

    material_id: Mapped[uuid.UUID]
    token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime]
    disabled_at: Mapped[datetime | None]
    disabled_by: Mapped[uuid.UUID | None]
    created_by: Mapped[uuid.UUID | None]
    opens: Mapped[int] = mapped_column(server_default="0")
    last_opened_at: Mapped[datetime | None]
