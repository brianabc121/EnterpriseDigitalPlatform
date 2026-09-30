"""按天递增的业务编号，如待办 TD20260930-0001（设计文档 §24、§25）。"""

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import ForeignKey, String, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NumberCounter(Base):
    __tablename__ = "number_counters"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), primary_key=True, server_default=text("app_current_tenant()")
    )
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    day: Mapped[date] = mapped_column(primary_key=True)
    value: Mapped[int] = mapped_column(server_default="0")


async def next_number(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    scope: str,
    prefix: str,
    now: datetime,
    tz: ZoneInfo,
) -> str:
    """取下一个编号（在当前事务里递增，事务回滚时编号也回滚）。"""
    day = now.astimezone(tz).date()
    value = await session.scalar(
        insert(NumberCounter)
        .values(tenant_id=tenant_id, scope=scope, day=day, value=1)
        .on_conflict_do_update(
            index_elements=["tenant_id", "scope", "day"],
            set_={"value": NumberCounter.value + 1},
        )
        .returning(NumberCounter.value)
    )
    assert value is not None
    return f"{prefix}{day:%Y%m%d}-{value:04d}"
