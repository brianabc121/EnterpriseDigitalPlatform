"""知识到期提醒（设计文档 §12.5）：有效期结束前 7 天提醒负责人确认是否需要更新内容或延期。

负责人没有设置或已停用时，提醒最后修改（或创建）这条知识的员工；都不在职时提醒有知识管理权限
的员工。站内信之外，有企业微信时另发应用消息。每个有效期只提醒一次（修改有效期后重新提醒）。
到期后由 expire_items 自动下线（kb/service.py）。
"""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.permissions import Permission
from app.modules.iam.models import Staff, StaffStatus
from app.modules.kb.distribution import audience
from app.modules.kb.models import ItemStatus, KbItem
from app.modules.notifications import service as notifications
from app.modules.wecom.notify import notify_staff

REMIND_BEFORE = timedelta(days=7)
BATCH = 200
KIND = "kb_expiring"


async def _recipients(ctx: AppContext, session: AsyncSession, item: KbItem) -> list[uuid.UUID]:
    """平台会话（不受行级安全限制），查询时都带上租户。"""
    for candidate in (item.owner_id, item.updated_by, item.created_by):
        if candidate is None:
            continue
        status = await session.scalar(
            select(Staff.status).where(Staff.tenant_id == item.tenant_id, Staff.id == candidate)
        )
        if status == StaffStatus.ACTIVE:
            return [candidate]
    async with ctx.db.tenant_session(item.tenant_id) as tenant_session:
        return [s.id for s in await audience(tenant_session, Permission.KB_MANAGE)]


async def remind_expiring(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：返回发出提醒的知识条数。"""
    now = now or datetime.now(UTC)
    tz = ZoneInfo(ctx.settings.usage_timezone)
    outgoing: list[tuple[uuid.UUID, list[uuid.UUID], str, str]] = []
    async with ctx.db.platform_sessionmaker() as session:
        items = (
            await session.scalars(
                select(KbItem)
                .where(
                    KbItem.status == ItemStatus.PUBLISHED,
                    KbItem.valid_to > now,
                    KbItem.valid_to <= now + REMIND_BEFORE,
                    KbItem.expiry_notified_at.is_(None),
                )
                .order_by(KbItem.valid_to)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for item in items:
            assert item.valid_to is not None
            recipients = await _recipients(ctx, session, item)
            title = f"知识即将到期：{item.title}"[:120]
            body = (
                f"将于 {item.valid_to.astimezone(tz):%Y-%m-%d %H:%M} 到期自动下线，"
                "请确认是否需要更新内容或延长有效期。"
            )
            notifications.add(
                session,
                item.tenant_id,
                recipients,
                kind=KIND,
                title=title,
                body=body,
                link=f"/knowledge?item={item.id}",
            )
            item.expiry_notified_at = now
            outgoing.append((item.tenant_id, recipients, title, body))
        await session.commit()
    for tenant_id, recipients, title, body in outgoing:
        await notify_staff(
            ctx, tenant_id, recipients, title=title, description=body, path="/knowledge"
        )
    return len(outgoing)
