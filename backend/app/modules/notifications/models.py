import uuid
from datetime import datetime

from sqlalchemy import ForeignKeyConstraint, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin


class StaffNotification(IdMixin, TenantMixin, Base):
    """站内信：知识到期提醒、知识周报、线索待确认等。企业微信应用消息另发（有企业微信时）。"""

    __tablename__ = "staff_notifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "staff_id"], ["staff.tenant_id", "staff.id"], ondelete="CASCADE"
        ),
    )

    staff_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    read_at: Mapped[datetime | None]
