"""个人待办的提醒（设计文档 §27.2）：截止前提醒一次、逾期提醒一次，每个工作日上班后发"今日个人
待办"汇总。站内信 + 企业微信应用消息 + AI 助理（notifications.push）。"""

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from app.context import AppContext
from app.modules.iam.models import Staff, StaffStatus
from app.modules.notifications import service as notifications
from app.modules.notifications.push import notify_staff
from app.modules.routing.hours import day_start, is_business_day
from app.modules.tasks import service
from app.modules.tasks import settings as task_settings
from app.modules.tasks.models import StaffTask, TaskStatus
from app.modules.todos import sla

logger = logging.getLogger(__name__)

BATCH = 500
# 提前提醒最多提前这么久：调度进程只取这个范围内到期的事项。
LOOKAHEAD = timedelta(days=7)
DIGEST_KEY = "edp:task-digest:{tenant}:{day}"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Message:
    staff_id: uuid.UUID
    kind: str
    title: str
    body: str
    path: str


async def run_timers(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：发送到期和逾期的提醒，返回发送的条数。"""
    now = now or utcnow()
    by_tenant: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    async with ctx.db.platform_sessionmaker() as session:
        rows = await session.execute(
            select(StaffTask.tenant_id, StaffTask.id)
            .where(
                StaffTask.status == TaskStatus.OPEN,
                StaffTask.due_at.is_not(None),
                StaffTask.due_at < now + LOOKAHEAD,
                or_(StaffTask.reminded_at.is_(None), StaffTask.overdue_notified_at.is_(None)),
            )
            .order_by(StaffTask.due_at)
            .limit(BATCH)
        )
        for tenant_id, task_id in rows:
            by_tenant[tenant_id].append(task_id)
    sent = 0
    for tenant_id, ids in by_tenant.items():
        try:
            sent += await _timers_tenant(ctx, tenant_id, ids, now)
        except Exception:
            logger.exception("task reminders for tenant %s failed", tenant_id)
    return sent


def _due_reminder(task: StaffTask, now: datetime) -> bool:
    if task.due_at is None or task.reminded_at is not None or task.due_at <= now:
        return False
    minutes = task.remind_before_minutes or 0
    return task.due_at - timedelta(minutes=minutes) <= now


async def _timers_tenant(
    ctx: AppContext, tenant_id: uuid.UUID, ids: list[uuid.UUID], now: datetime
) -> int:
    outgoing: list[Message] = []
    async with ctx.db.tenant_session(tenant_id) as session:
        spec = await sla.business_hours(session)
        tz = sla.tz_of(spec)
        tasks = (
            await session.scalars(
                select(StaffTask)
                .where(StaffTask.id.in_(ids), StaffTask.status == TaskStatus.OPEN)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for task in tasks:
            assert task.due_at is not None
            when = task.due_at.astimezone(tz).strftime("%m-%d %H:%M")
            if _due_reminder(task, now):
                task.reminded_at = now
                outgoing.append(
                    Message(
                        task.owner_id,
                        "task_due",
                        f"快到期：{task.title}"[:200],
                        f"截止 {when}。",
                        service.link(task),
                    )
                )
            elif task.due_at < now and task.overdue_notified_at is None:
                task.overdue_notified_at = now
                outgoing.append(
                    Message(
                        task.owner_id,
                        "task_overdue",
                        f"已逾期：{task.title}"[:200],
                        f"截止 {when}，还没完成。",
                        service.link(task),
                    )
                )
        for message in outgoing:
            notifications.add(
                session,
                tenant_id,
                [message.staff_id],
                kind=message.kind,
                title=message.title,
                body=message.body,
                link=message.path,
            )
        await session.commit()
    for message in outgoing:
        await notify_staff(
            ctx,
            tenant_id,
            [message.staff_id],
            title=message.title[:128],
            description=message.body,
            path=message.path,
        )
    return len(outgoing)


async def run_digest(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：每个工作日上班后给有事项的员工发一次"今日个人待办"。返回发送的租户数。"""
    now = now or utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        tenants = list(
            (
                await session.scalars(
                    select(StaffTask.tenant_id)
                    .where(StaffTask.status == TaskStatus.OPEN, StaffTask.due_at.is_not(None))
                    .distinct()
                )
            ).all()
        )
    sent = 0
    for tenant_id in tenants:
        try:
            sent += await _digest_tenant(ctx, tenant_id, now)
        except Exception:
            logger.exception("task digest for tenant %s failed", tenant_id)
    return sent


async def _digest_tenant(ctx: AppContext, tenant_id: uuid.UUID, now: datetime) -> int:
    outgoing: list[Message] = []
    async with ctx.db.tenant_session(tenant_id) as session:
        settings = await task_settings.load(session, tenant_id)
        spec = await sla.business_hours(session)
        start = day_start(spec, now)
        if not settings.digest_enabled or not is_business_day(spec, now) or start is None:
            return 0
        if now < start:
            return 0
        tz = sla.tz_of(spec)
        day = now.astimezone(tz).date()
        key = DIGEST_KEY.format(tenant=tenant_id, day=day.isoformat())
        if await ctx.redis.exists(key):
            return 0
        day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
        rows = await session.execute(
            select(
                StaffTask.owner_id,
                func.count().filter(StaffTask.due_at >= now, StaffTask.due_at < day_end),
                func.count().filter(StaffTask.due_at < now),
            )
            .join(Staff, Staff.id == StaffTask.owner_id)
            .where(
                StaffTask.status == TaskStatus.OPEN,
                StaffTask.due_at.is_not(None),
                Staff.status == StaffStatus.ACTIVE,
            )
            .group_by(StaffTask.owner_id)
        )
        for staff_id, due_today, overdue in rows:
            if not due_today and not overdue:
                continue
            body = f"今日到期 {due_today} 条，已逾期 {overdue} 条。"
            message = Message(
                staff_id, "task_digest", f"今日个人待办（{day:%m-%d}）", body, "/tasks"
            )
            notifications.add(
                session,
                tenant_id,
                [staff_id],
                kind=message.kind,
                title=message.title,
                body=message.body,
                link=message.path,
            )
            outgoing.append(message)
        await session.commit()
    await ctx.redis.set(key, "1", ex=int(timedelta(days=2).total_seconds()))
    for message in outgoing:
        await notify_staff(
            ctx,
            tenant_id,
            [message.staff_id],
            title=message.title,
            description=message.body,
            path=message.path,
        )
    return 1
