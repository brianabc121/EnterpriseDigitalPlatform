import uuid

from sqlalchemy import ForeignKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class QuickReply(IdMixin, TimestampMixin, TenantMixin, Base):
    """快捷话术。owner_id 为空表示全员共享。"""

    __tablename__ = "quick_replies"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "owner_id"], ["staff.tenant_id", "staff.id"]),
    )

    owner_id: Mapped[uuid.UUID | None]
    category: Mapped[str] = mapped_column(String(32), server_default="")
    title: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    sort: Mapped[int] = mapped_column(server_default="0")
