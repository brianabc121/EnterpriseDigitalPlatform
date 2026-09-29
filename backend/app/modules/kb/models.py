import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKeyConstraint, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin
from app.db.types import Vector

EMBED_DIM = 1024


class ItemKind(StrEnum):
    FAQ = "faq"
    DOC = "doc"


class ItemStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class Visibility(StrEnum):
    PUBLIC = "public"  # 对客 AI 与坐席可用
    AGENT = "agent"  # 仅坐席可见（Copilot、知识检索）
    ADMIN = "admin"  # 仅管理员可见


class ItemSource(StrEnum):
    MANUAL = "manual"
    IMPORT = "import"
    EXTRACTED = "extracted"


class KbItem(IdMixin, TimestampMixin, TenantMixin, Base):
    """知识条目（设计文档 §12.1）。FAQ：title 为标准问、questions 为相似问、content 为答案；
    文档：title 为标题、content 为正文。"""

    __tablename__ = "kb_items"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    kind: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    questions: Mapped[list[str]] = mapped_column(server_default="{}")
    category: Mapped[str] = mapped_column(String(64), server_default="")
    tags: Mapped[list[str]] = mapped_column(server_default="{}")
    status: Mapped[str] = mapped_column(String(16), server_default=ItemStatus.DRAFT.value)
    visibility: Mapped[str] = mapped_column(String(16), server_default=Visibility.PUBLIC.value)
    valid_from: Mapped[datetime | None]
    valid_to: Mapped[datetime | None]
    source: Mapped[str] = mapped_column(String(16), server_default=ItemSource.MANUAL.value)
    version: Mapped[int] = mapped_column(server_default="1")
    hits: Mapped[int] = mapped_column(server_default="0")
    last_hit_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None]
    updated_by: Mapped[uuid.UUID | None]
    published_at: Mapped[datetime | None]


class ChunkKind(StrEnum):
    QUESTION = "question"  # FAQ 的标准问或相似问
    PASSAGE = "passage"  # 文档切片


class KbChunk(IdMixin, TenantMixin, Base):
    """检索单元。只为已发布的条目生成，条目下线或修改时重建。"""

    __tablename__ = "kb_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "item_id"], ["kb_items.tenant_id", "kb_items.id"], ondelete="CASCADE"
        ),
    )

    item_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(String(12))
    text: Mapped[str] = mapped_column(Text)
    terms: Mapped[list[str]] = mapped_column(server_default="{}")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
