import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, MetaData, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.ids import new_id

# 表结构由手写迁移（alembic/versions）定义；这里的元数据只供 ORM 使用。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012  SQLAlchemy 约定的类属性
        dict[str, Any]: JSONB,
        list[str]: ARRAY(Text),
        list[uuid.UUID]: ARRAY(PG_UUID(as_uuid=True)),
        datetime: DateTime(timezone=True),
        uuid.UUID: PG_UUID(as_uuid=True),
    }


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class TenantMixin:
    """租户数据表。tenant_id 未显式赋值时，由数据库按当前事务的租户上下文填充。"""

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), server_default=text("app_current_tenant()")
    )
