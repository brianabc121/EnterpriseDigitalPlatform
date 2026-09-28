import uuid

from sqlalchemy import ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class Customer(IdMixin, TimestampMixin, TenantMixin, Base):
    """客户档案主记录。归属坐席（owner）决定坐席能否看到这个客户。"""

    __tablename__ = "customers"
    __table_args__ = (
        # 复合外键保证归属坐席与客户属于同一租户。
        ForeignKeyConstraint(
            ["tenant_id", "owner_id"], ["staff.tenant_id", "staff.id"], ondelete="SET NULL"
        ),
    )

    display_name: Mapped[str] = mapped_column(String(128))
    owner_id: Mapped[uuid.UUID | None]
    source_channel: Mapped[str] = mapped_column(String(32), server_default="manual")
