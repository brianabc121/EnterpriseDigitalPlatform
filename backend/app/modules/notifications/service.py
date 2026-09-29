"""站内信（设计文档 §12.6）：知识到期提醒、线索待确认、知识周报等，控制台右上角的铃铛里查看。

有企业微信时，业务代码另外通过应用消息提醒（wecom/notify.py）。
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.modules.notifications.models import StaffNotification
from app.modules.notifications.schemas import NotificationList, NotificationOut

KEEP = 100


def add(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    staff_ids: Iterable[uuid.UUID],
    *,
    kind: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
) -> int:
    """写入站内信（加入当前事务，由调用方提交），返回收件人数。"""
    recipients = list(dict.fromkeys(staff_ids))
    for staff_id in recipients:
        session.add(
            StaffNotification(
                tenant_id=tenant_id,
                staff_id=staff_id,
                kind=kind,
                title=title[:200],
                body=body,
                link=link,
            )
        )
    return len(recipients)


def _out(row: StaffNotification) -> NotificationOut:
    return NotificationOut(
        id=row.id,
        kind=row.kind,
        title=row.title,
        body=row.body,
        link=row.link,
        created_at=row.created_at,
        read_at=row.read_at,
    )


async def list_for(
    session: AsyncSession, staff_id: uuid.UUID, *, unread_only: bool, limit: int
) -> NotificationList:
    query = select(StaffNotification).where(StaffNotification.staff_id == staff_id)
    if unread_only:
        query = query.where(StaffNotification.read_at.is_(None))
    rows = await session.scalars(
        query.order_by(StaffNotification.created_at.desc(), StaffNotification.id.desc()).limit(
            limit
        )
    )
    unread = await session.scalar(
        select(func.count())
        .select_from(StaffNotification)
        .where(StaffNotification.staff_id == staff_id, StaffNotification.read_at.is_(None))
    )
    return NotificationList(items=[_out(r) for r in rows.all()], unread=unread or 0)


async def mark_read(session: AsyncSession, staff_id: uuid.UUID, notification_id: uuid.UUID) -> None:
    row = await session.get(StaffNotification, notification_id)
    if row is None or row.staff_id != staff_id:
        raise NotFound("站内信不存在")
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
    await session.commit()


async def mark_all_read(session: AsyncSession, staff_id: uuid.UUID) -> None:
    await session.execute(
        update(StaffNotification)
        .where(StaffNotification.staff_id == staff_id, StaffNotification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await session.commit()
