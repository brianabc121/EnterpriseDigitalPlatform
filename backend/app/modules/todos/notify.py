"""待办的提醒与通知客户（设计文档 §24.5–§24.7）。

提醒员工：站内信（控制台铃铛）和企业微信应用消息（点开后在企业微信里打开待办详情）。
- 新的待确认提醒确认人（投诉等类型同时提醒主管）；超过设定的工作时长还没处理，再提醒一次；
- 进入待办列表并分派后提醒处理人（技能组待认领的提醒组员，公共待认领池提醒能分派待办的人）；
- 客户催促、重新打开时提醒处理人；
- 截止前提醒一次、逾期时提醒一次，逾期超过设定时长升级给主管；
- 每个工作日上班时发送"今日待办"汇总。
同一条待办每种提醒只发一次（重新安排截止时间后重新计算）。

通知客户按渠道的规则：官网 Widget 发系统消息；微信客服在回复窗口内直接发送，窗口已关闭时记
"未能通知"；企业微信客户联系不能经接口发送，记为"待员工发送"，由员工在侧边栏一键发送。
"""

import logging
import uuid
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.permissions import Permission
from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.conversation import outbox
from app.modules.conversation.models import ChatSession, Room
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff
from app.modules.notifications import service as notifications
from app.modules.routing.hours import day_start, is_business_day
from app.modules.routing.models import SkillGroupMember
from app.modules.todos import assign, events, sla
from app.modules.todos import settings as todo_settings
from app.modules.todos.models import (
    ACTIVE,
    SOURCE_LABELS,
    TIMED,
    UNFINISHED,
    ActorType,
    NotifyReason,
    Todo,
    TodoStatus,
    TodoType,
)
from app.modules.wecom.notify import notify_staff

logger = logging.getLogger(__name__)

BATCH = 200
DIGEST_KEY = "edp:todo-digest:{tenant}:{day}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def link(todo: Todo) -> str:
    view = "pending" if todo.status == TodoStatus.PENDING else "all"
    return f"/todos?view={view}&id={todo.id}"


@dataclass(frozen=True)
class Message:
    staff_ids: list[uuid.UUID]
    kind: str
    title: str
    body: str
    path: str


def _when(value: datetime | None, spec: dict[str, Any] | None) -> str:
    return value.astimezone(sla.tz_of(spec)).strftime("%m-%d %H:%M") if value else ""


async def _customer_name(session: AsyncSession, todo: Todo) -> str:
    if todo.customer_id is None:
        return "无关联客户"
    name = await session.scalar(
        select(Customer.display_name).where(Customer.id == todo.customer_id)
    )
    return name or "客户"


async def _compose(
    session: AsyncSession,
    todo: Todo,
    type_: TodoType,
    reason: str,
    spec: dict[str, Any] | None,
) -> Message | None:
    recipients = await assign.recipients(session, todo)
    customer = await _customer_name(session, todo)
    subject = f"{type_.name}「{todo.title}」"
    match reason:
        case NotifyReason.PENDING:
            if type_.notify_supervisor:
                recipients = [*recipients, *await assign.supervisors(session, todo)]
            source = SOURCE_LABELS.get(todo.source, todo.source)
            return Message(
                recipients,
                "todo_pending",
                f"待确认：{subject}",
                f"{customer} · {source}生成，请确认后进入待办列表。",
                link(todo),
            )
        case NotifyReason.ASSIGNED:
            due = f"截止 {_when(todo.due_at, spec)}。" if todo.due_at else "请尽快处理。"
            head = "新待办" if todo.assignee_id else "待认领"
            return Message(
                recipients, "todo_assigned", f"{head}：{subject}", f"{customer} · {due}", link(todo)
            )
        case NotifyReason.NUDGED if todo.status == TodoStatus.DONE:
            return Message(
                recipients,
                "todo_nudged",
                f"客户再次提出：{subject}",
                f"{customer} 在完成后再次提出这件事，需要时请重新打开。",
                link(todo),
            )
        case NotifyReason.NUDGED:
            return Message(
                recipients,
                "todo_nudged",
                f"客户催促：{subject}",
                f"{customer} 第 {todo.nudge_count} 次催促，请尽快处理。",
                link(todo),
            )
        case NotifyReason.REOPENED:
            return Message(
                recipients,
                "todo_reopened",
                f"待办重新打开：{subject}",
                f"{customer} 再次提出，请跟进。",
                link(todo),
            )
    return None


async def _send(ctx: AppContext, tenant_id: uuid.UUID, messages: list[Message]) -> None:
    """企业微信应用消息（在站内信提交之后发送，尽力而为）。"""
    for message in messages:
        if message.staff_ids:
            await notify_staff(
                ctx,
                tenant_id,
                message.staff_ids,
                title=message.title[:128],
                description=message.body,
                path=message.path,
            )


def _write(session: AsyncSession, tenant_id: uuid.UUID, message: Message) -> None:
    notifications.add(
        session,
        tenant_id,
        message.staff_ids,
        kind=message.kind,
        title=message.title,
        body=message.body,
        link=message.path,
    )


async def dispatch(
    ctx: AppContext,
    *,
    tenant_id: uuid.UUID | None = None,
    ids: Collection[uuid.UUID] | None = None,
    now: datetime | None = None,
) -> int:
    """发送待发送的提醒（notify_reason 且 notified_at 为空）。接口在提交后对刚处理的待办调用，
    调度进程定期兜底。每条提醒只发一次（按行加锁、跳过别人正在处理的）。"""
    now = now or utcnow()
    by_tenant: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    if tenant_id is not None and ids is not None:
        by_tenant[tenant_id] = list(ids)
    else:
        async with ctx.db.platform_sessionmaker() as session:
            query = select(Todo.tenant_id, Todo.id).where(
                Todo.notified_at.is_(None), Todo.notify_reason.is_not(None)
            )
            if tenant_id is not None:
                query = query.where(Todo.tenant_id == tenant_id)
            for tenant, todo_id in await session.execute(
                query.order_by(Todo.created_at).limit(BATCH)
            ):
                by_tenant[tenant].append(todo_id)
    sent = 0
    for tenant, todo_ids in by_tenant.items():
        if todo_ids:
            sent += await _dispatch_tenant(ctx, tenant, todo_ids, now)
    return sent


async def _dispatch_tenant(
    ctx: AppContext, tenant_id: uuid.UUID, todo_ids: list[uuid.UUID], now: datetime
) -> int:
    outgoing: list[Message] = []
    async with ctx.db.tenant_session(tenant_id) as session:
        todos = (
            await session.scalars(
                select(Todo)
                .where(Todo.id.in_(todo_ids), Todo.notified_at.is_(None))
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not todos:
            return 0
        spec = await sla.business_hours(session)
        types = await _types(session, {t.type_id for t in todos})
        for todo in todos:
            reason, todo.notified_at = todo.notify_reason, now
            # 已完成的待办只提醒"客户再次提出"。
            done_nudge = reason == NotifyReason.NUDGED and todo.status == TodoStatus.DONE
            if reason is None or (todo.status not in UNFINISHED and not done_nudge):
                continue
            message = await _compose(session, todo, types[todo.type_id], reason, spec)
            if message is not None and message.staff_ids:
                _write(session, tenant_id, message)
                outgoing.append(message)
        await session.commit()
    await _send(ctx, tenant_id, outgoing)
    return len(outgoing)


async def _types(session: AsyncSession, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, TodoType]:
    return {t.id: t for t in await session.scalars(select(TodoType).where(TodoType.id.in_(ids)))}


# ---- 到期提醒、逾期与升级 ----


async def _due(ctx: AppContext, query: Any) -> dict[uuid.UUID, list[uuid.UUID]]:
    by_tenant: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    async with ctx.db.platform_sessionmaker() as session:
        for tenant, todo_id in await session.execute(query.limit(BATCH)):
            by_tenant[tenant].append(todo_id)
    return by_tenant


async def run_timers(ctx: AppContext, *, now: datetime | None = None) -> dict[str, int]:
    """调度任务：发送待发送的提醒、待确认再提醒、到期提醒、逾期提醒和升级。"""
    now = now or utcnow()
    report = {"notified": await dispatch(ctx, now=now)}
    base = select(Todo.tenant_id, Todo.id)
    kinds: dict[str, Any] = {
        "pending": base.where(Todo.status == TodoStatus.PENDING, Todo.pending_remind_at <= now),
        "due": base.where(Todo.status.in_(TIMED), Todo.remind_at <= now, Todo.due_at > now),
        "overdue": base.where(
            Todo.status.in_(TIMED), Todo.due_at <= now, Todo.overdue_notified_at.is_(None)
        ),
        "escalated": base.where(
            Todo.status.in_(TIMED), Todo.escalate_at <= now, Todo.escalated_at.is_(None)
        ),
    }
    for kind, query in kinds.items():
        count = 0
        for tenant_id, ids in (await _due(ctx, query)).items():
            count += await _remind_tenant(ctx, tenant_id, ids, kind, now)
        report[kind] = count
    return report


async def _remind_tenant(
    ctx: AppContext, tenant_id: uuid.UUID, ids: list[uuid.UUID], kind: str, now: datetime
) -> int:
    outgoing: list[Message] = []
    done = 0
    async with ctx.db.tenant_session(tenant_id) as session:
        todos = (
            await session.scalars(
                select(Todo).where(Todo.id.in_(ids)).with_for_update(skip_locked=True)
            )
        ).all()
        spec = await sla.business_hours(session)
        types = await _types(session, {t.type_id for t in todos})
        for todo in todos:
            message = await _timer_message(session, todo, types[todo.type_id], kind, spec, now)
            if message is None:
                continue
            done += 1
            if message.staff_ids:
                _write(session, tenant_id, message)
                outgoing.append(message)
        await session.commit()
    await _send(ctx, tenant_id, outgoing)
    return done


async def _timer_message(
    session: AsyncSession,
    todo: Todo,
    type_: TodoType,
    kind: str,
    spec: dict[str, Any] | None,
    now: datetime,
) -> Message | None:
    """重新检查条件（加锁之后），更新标记并生成提醒；条件已经不满足时返回空。"""
    subject = f"{type_.name}「{todo.title}」"
    customer = await _customer_name(session, todo)
    if kind == "pending":
        if todo.status != TodoStatus.PENDING or not todo.pending_remind_at:
            return None
        if todo.pending_remind_at > now:
            return None
        todo.pending_remind_at = None
        events.record(session, todo, "reminded", payload={"kind": kind})
        recipients = await assign.recipients(session, todo)
        return Message(
            recipients,
            "todo_pending",
            f"待确认提醒：{subject}",
            f"{customer} · 已经等待确认一段时间了，请尽快确认或驳回。",
            link(todo),
        )
    if todo.status not in TIMED or todo.due_at is None:
        return None
    if kind == "due":
        if todo.remind_at is None or todo.remind_at > now or todo.due_at <= now:
            return None
        todo.remind_at = None
        events.record(session, todo, "reminded", payload={"kind": kind})
        return Message(
            await assign.recipients(session, todo),
            "todo_due",
            f"即将到期：{subject}",
            f"{customer} · 截止 {_when(todo.due_at, spec)}。",
            link(todo),
        )
    if kind == "overdue":
        if todo.due_at > now or todo.overdue_notified_at is not None:
            return None
        todo.overdue_notified_at = now
        todo.remind_at = None
        events.record(session, todo, "reminded", payload={"kind": kind})
        return Message(
            await assign.recipients(session, todo),
            "todo_overdue",
            f"已逾期：{subject}",
            f"{customer} · 截止时间 {_when(todo.due_at, spec)} 已过，请尽快处理。",
            link(todo),
        )
    if kind == "escalated":
        if todo.escalate_at is None or todo.escalate_at > now or todo.escalated_at is not None:
            return None
        todo.escalated_at = now
        todo.escalate_at = None
        leads = await assign.supervisors(session, todo)
        events.record(
            session,
            todo,
            "escalated",
            payload={"to": [str(s) for s in leads]},
        )
        handler = "待认领"
        if todo.assignee_id is not None:
            handler = (
                await session.scalar(select(Staff.display_name).where(Staff.id == todo.assignee_id))
                or "处理人"
            )
        return Message(
            leads,
            "todo_escalated",
            f"待办逾期升级：{subject}",
            f"{customer} · 处理人 {handler}，截止 {_when(todo.due_at, spec)}，已逾期。",
            link(todo),
        )
    return None


# ---- 今日待办汇总 ----


async def run_digest(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：每个工作日上班后发送一次"今日待办"汇总。返回发送的租户数。"""
    now = now or utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        tenants = list(
            (
                await session.scalars(
                    select(Todo.tenant_id).where(Todo.status.in_(UNFINISHED)).distinct()
                )
            ).all()
        )
    sent = 0
    for tenant_id in tenants:
        try:
            sent += await _digest_tenant(ctx, tenant_id, now)
        except Exception:
            logger.exception("to-do digest for tenant %s failed", tenant_id)
    return sent


async def _digest_tenant(ctx: AppContext, tenant_id: uuid.UUID, now: datetime) -> int:
    outgoing: list[Message] = []
    async with ctx.db.tenant_session(tenant_id) as session:
        settings = await todo_settings.load(session, tenant_id)
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
        counts: dict[uuid.UUID, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        mine = await session.execute(
            select(
                Todo.assignee_id,
                func.count().filter(Todo.status == TodoStatus.PENDING),
                func.count().filter(
                    Todo.status.in_(ACTIVE), Todo.due_at >= now, Todo.due_at < day_end
                ),
                func.count().filter(Todo.status.in_(ACTIVE), Todo.due_at < now),
            )
            .where(Todo.assignee_id.is_not(None), Todo.status.in_(UNFINISHED))
            .group_by(Todo.assignee_id)
        )
        for staff_id, pending, due_today, overdue in mine:
            if staff_id is not None:
                counts[staff_id].update(pending=pending, due_today=due_today, overdue=overdue)
        pools = await session.execute(
            select(SkillGroupMember.staff_id, func.count())
            .join(Todo, Todo.skill_group_id == SkillGroupMember.skill_group_id)
            .where(Todo.assignee_id.is_(None), Todo.status.in_(UNFINISHED))
            .group_by(SkillGroupMember.staff_id)
        )
        for staff_id, pool in pools:
            counts[staff_id]["pool"] = pool
        active = set(await assign.staff_with(session, Permission.TODO_READ))
        for staff_id, values in counts.items():
            if staff_id not in active or not any(values.values()):
                continue
            body = "，".join(
                f"{label} {values.get(key, 0)} 条"
                for key, label in (
                    ("pending", "待确认"),
                    ("due_today", "今日到期"),
                    ("overdue", "已逾期"),
                    ("pool", "待认领"),
                )
            )
            message = Message([staff_id], "todo_digest", f"今日待办（{day:%m-%d}）", body, "/todos")
            _write(session, tenant_id, message)
            outgoing.append(message)
        await session.commit()
    await ctx.redis.set(key, "1", ex=int(timedelta(days=2).total_seconds()))
    await _send(ctx, tenant_id, outgoing)
    return 1


# ---- 通知客户 ----


@dataclass(frozen=True)
class CustomerNotice:
    status: str  # sent（已发送）、manual（待员工发送）、unreachable（未能通知）
    channel: str | None
    reason: str | None
    room_id: uuid.UUID | None


async def _room_of(session: AsyncSession, todo: Todo) -> Room | None:
    if todo.session_id is not None:
        room_id = await session.scalar(
            select(ChatSession.room_id).where(ChatSession.id == todo.session_id)
        )
        if room_id is not None:
            return await session.get(Room, room_id)
    if todo.customer_id is None:
        return None
    return await session.scalar(
        select(Room)
        .where(Room.customer_id == todo.customer_id)
        .order_by(Room.updated_at.desc())
        .limit(1)
    )


async def notify_customer(
    session: AsyncSession,
    todo: Todo,
    text: str,
    *,
    actor_type: str = ActorType.STAFF,
    actor_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> CustomerNotice:
    """按客户所在渠道的规则通知客户，结果记入待办动态（由调用方提交并刷新发件箱）。"""
    from app.modules.wecom.kf import reply_window

    now = now or utcnow()
    room = await _room_of(session, todo)
    channel = await session.get(ChannelAccount, room.channel_account_id) if room else None
    if room is None or channel is None:
        notice = CustomerNotice("unreachable", None, "客户没有可以联系的对话", None)
    elif channel.type == ChannelType.WEB:
        outbox.enqueue_notice(session, room.id, text)
        notice = CustomerNotice("sent", channel.type, None, room.id)
    elif channel.type == ChannelType.WECOM_KF:
        window = await reply_window(session, room.id, now)
        if window.open:
            outbox.enqueue_notice(session, room.id, text)
            notice = CustomerNotice("sent", channel.type, None, room.id)
        else:
            notice = CustomerNotice("unreachable", channel.type, window.reason, room.id)
    else:
        notice = CustomerNotice(
            "manual", channel.type, "企业微信客户联系不能直接发送，请在侧边栏发送", room.id
        )
    events.record(
        session,
        todo,
        "customer_notified",
        actor_type=actor_type,
        actor_id=actor_id,
        payload={
            "status": notice.status,
            "channel": notice.channel,
            "reason": notice.reason,
            "text": text[:1000],
        },
    )
    return notice
