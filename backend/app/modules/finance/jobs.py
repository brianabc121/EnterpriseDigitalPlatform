"""应收账款的调度任务（设计文档 §28.6）：每天一次，给有 finance:manage 权限的员工提醒逾期的应收。"""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.context import AppContext
from app.core.permissions import Permission
from app.modules.finance import service
from app.modules.notifications import push
from app.modules.notifications import service as notifications
from app.modules.orders.models import Order
from app.modules.security.models import TenantSetting
from app.modules.todos import assign

logger = logging.getLogger(__name__)

REMIND_HOUR = 9
KIND = "finance_overdue"
PATH = "/receivables?view=overdue"
REMINDED_KEY = "overdue_reminded_on"


async def run_overdue_reminders(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：租户时区 9 点之后、当天还没提醒过，且有逾期的应收时，给有 finance:manage 权限的
    员工（没有财务角色时就是管理员）各发一条提醒。返回提醒的租户数。"""
    now = now or datetime.now(UTC)
    async with ctx.db.platform_sessionmaker() as session:
        tenants = list(
            (
                await session.scalars(
                    select(Order.tenant_id).where(service.receivable()).distinct()
                )
            ).all()
        )
    sent = 0
    for tenant_id in tenants:
        try:
            sent += await _remind_tenant(ctx, tenant_id, now)
        except Exception:
            logger.exception("overdue receivable reminder for tenant %s failed", tenant_id)
    return sent


async def _remind_tenant(ctx: AppContext, tenant_id: uuid.UUID, now: datetime) -> int:
    recipients: list[uuid.UUID] = []
    title = body = ""
    async with ctx.db.tenant_session(tenant_id) as session:
        cal = await service.calendar(session, tenant_id, now)
        if now.astimezone(cal.tz).hour < REMIND_HOUR:
            return 0
        row = await session.get(TenantSetting, tenant_id, with_for_update=True)
        marker = cal.today.isoformat()
        if row is not None and (row.finance or {}).get(REMINDED_KEY) == marker:
            return 0
        due = service.due_date(cal)
        count, amount, oldest = (
            await session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(service.outstanding()), 0),
                    func.min(due),
                )
                .select_from(Order)
                .where(service.receivable(), due < cal.today)
            )
        ).one()
        if count:
            recipients = await assign.staff_with(session, Permission.FINANCE_MANAGE)
        if recipients:
            title = f"有 {count} 笔应收逾期，合计 ¥{Decimal(amount):,.2f}"
            body = f"最久逾期 {(cal.today - oldest).days} 天，请及时跟进。"
            notifications.add(
                session, tenant_id, recipients, kind=KIND, title=title, body=body, link=PATH
            )
        if row is None:
            row = TenantSetting(tenant_id=tenant_id)
            session.add(row)
        row.finance = {**(row.finance or {}), REMINDED_KEY: marker}
        await session.commit()
    if not recipients:
        return 0
    await push.notify_staff(ctx, tenant_id, recipients, title=title, description=body, path=PATH)
    return 1
