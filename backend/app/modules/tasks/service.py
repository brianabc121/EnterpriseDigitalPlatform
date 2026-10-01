"""个人待办的数据范围、查询与操作（设计文档 §27.2）。

- 数据范围：自己的事项和自己交办的事项；有 task:read_all 的人看全员。租户之间由 RLS 隔离。
- 交办：owner 不是自己时需要 task:assign，接收人立即收到提醒（站内信 + 企业微信 + AI 助理）。
- 提醒不在事务里发送：接口在提交之后调用 push。
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, case, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.db.counters import next_number
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.notifications import service as notifications
from app.modules.notifications.push import notify_staff
from app.modules.tasks import settings as task_settings
from app.modules.tasks.models import StaffTask, TaskPriority, TaskSource, TaskStatus
from app.modules.tasks.schemas import (
    StaffOption,
    StaffOptions,
    TaskAllowed,
    TaskCounts,
    TaskCreate,
    TaskDueFilter,
    TaskOut,
    TaskOverview,
    TaskOverviewRow,
    TaskPage,
    TaskUpdate,
    TaskView,
)
from app.modules.todos import sla
from app.modules.todos.models import ACTIVE as TODO_ACTIVE
from app.modules.todos.models import Todo

NOT_FOUND = "事项不存在"
NUMBER_SCOPE = "task"
NUMBER_PREFIX = "T"
LINK_PREFIX = "/tasks?id="


def utcnow() -> datetime:
    return datetime.now(UTC)


def link(task: StaffTask) -> str:
    return f"{LINK_PREFIX}{task.id}"


# ---- 数据范围 ----


def visible_to(principal: Principal) -> ColumnElement[bool]:
    if principal.has(Permission.TASK_READ_ALL):
        return true()
    me = principal.staff_id
    return or_(StaffTask.owner_id == me, StaffTask.created_by == me)


def can_edit(principal: Principal, task: StaffTask) -> bool:
    return (
        principal.has(Permission.TASK_READ_ALL)
        or task.owner_id == principal.staff_id
        or task.created_by == principal.staff_id
    )


async def get_visible(session: AsyncSession, principal: Principal, task_id: uuid.UUID) -> StaffTask:
    task = await session.scalar(
        select(StaffTask).where(StaffTask.id == task_id, visible_to(principal))
    )
    if task is None:
        raise NotFound(NOT_FOUND)
    return task


async def _editable(session: AsyncSession, principal: Principal, task_id: uuid.UUID) -> StaffTask:
    task = await get_visible(session, principal, task_id)
    if not can_edit(principal, task):
        raise Forbidden("只能处理自己的事项")
    return task


# ---- 查询 ----


def _priority_rank() -> Any:
    return case(
        {TaskPriority.URGENT.value: 4, TaskPriority.HIGH.value: 3, TaskPriority.NORMAL.value: 2},
        value=StaffTask.priority,
        else_=1,
    )


def _due(due: TaskDueFilter, now: datetime, tz: Any) -> ColumnElement[bool]:
    is_open = StaffTask.status == TaskStatus.OPEN
    if due == "overdue":
        return and_(is_open, StaffTask.due_at < now)
    if due == "soon":
        return and_(is_open, StaffTask.due_at >= now, StaffTask.due_at < now + timedelta(hours=24))
    today = now.astimezone(tz).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
    return and_(is_open, StaffTask.due_at >= start, StaffTask.due_at < start + timedelta(days=1))


async def _tz(session: AsyncSession) -> Any:
    return sla.tz_of(await sla.business_hours(session))


def _view(principal: Principal, view: TaskView) -> ColumnElement[bool]:
    me = principal.staff_id
    if view == "mine":
        return StaffTask.owner_id == me
    if view == "assigned":
        return and_(StaffTask.created_by == me, StaffTask.owner_id != me)
    if not principal.has(Permission.TASK_READ_ALL):
        raise Forbidden("没有查看全员事项的权限")
    return true()


async def list_tasks(
    session: AsyncSession,
    principal: Principal,
    *,
    view: TaskView = "mine",
    owner_id: uuid.UUID | None = None,
    status: TaskStatus | None = None,
    due: TaskDueFilter | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
    now: datetime | None = None,
) -> TaskPage:
    now = now or utcnow()
    where: list[ColumnElement[bool]] = [visible_to(principal), _view(principal, view)]
    if owner_id is not None:
        where.append(StaffTask.owner_id == owner_id)
    if status is not None:
        where.append(StaffTask.status == status)
    if due is not None:
        where.append(_due(due, now, await _tz(session)))
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        where.append(or_(StaffTask.no.ilike(pattern), StaffTask.title.ilike(pattern)))
    query = select(StaffTask).where(and_(*where))
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    # 未完成的在前：先到期的、优先级高的靠前；完成和取消的按最近处理的在前。
    rows = (
        await session.scalars(
            query.order_by(
                case({TaskStatus.OPEN.value: 0}, value=StaffTask.status, else_=1),
                StaffTask.due_at.asc().nulls_last(),
                _priority_rank().desc(),
                StaffTask.updated_at.desc(),
                StaffTask.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return TaskPage(items=await outs(session, principal, rows, now=now), total=total or 0)


async def _names(session: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(wanted)))
    return {staff_id: name for staff_id, name in rows}


async def outs(
    session: AsyncSession,
    principal: Principal,
    tasks: Iterable[StaffTask],
    *,
    now: datetime | None = None,
) -> list[TaskOut]:
    rows = list(tasks)
    now = now or utcnow()
    for row in rows:
        await session.refresh(row)
    names = await _names(session, [i for t in rows for i in (t.owner_id, t.created_by)])
    return [
        TaskOut(
            id=t.id,
            no=t.no,
            owner_id=t.owner_id,
            owner_name=names.get(t.owner_id),
            title=t.title,
            note=t.note,
            priority=TaskPriority(t.priority),
            status=TaskStatus(t.status),
            source=TaskSource(t.source),
            due_at=t.due_at,
            overdue=bool(t.due_at and t.due_at < now and t.status == TaskStatus.OPEN),
            remind_before_minutes=t.remind_before_minutes,
            link=t.link,
            done_note=t.done_note,
            done_at=t.done_at,
            created_by=t.created_by,
            created_by_name=names.get(t.created_by) if t.created_by else None,
            created_at=t.created_at,
            updated_at=t.updated_at,
            allowed=TaskAllowed(edit=can_edit(principal, t)),
        )
        for t in rows
    ]


async def out(session: AsyncSession, principal: Principal, task: StaffTask) -> TaskOut:
    [result] = await outs(session, principal, [task])
    return result


async def counts(
    session: AsyncSession, principal: Principal, *, now: datetime | None = None
) -> TaskCounts:
    now = now or utcnow()
    tz = await _tz(session)
    me = principal.staff_id
    row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(_due("today", now, tz)),
                func.count().filter(_due("overdue", now, tz)),
            ).where(StaffTask.owner_id == me, StaffTask.status == TaskStatus.OPEN)
        )
    ).one()
    work_todos = None
    if principal.has(Permission.TODO_READ):
        work_todos = await session.scalar(
            select(func.count())
            .select_from(Todo)
            .where(Todo.assignee_id == me, Todo.status.in_(TODO_ACTIVE))
        )
    return TaskCounts(open=row[0], due_today=row[1], overdue=row[2], work_todos=work_todos)


async def overview(
    session: AsyncSession, principal: Principal, *, now: datetime | None = None
) -> TaskOverview:
    if not principal.has(Permission.TASK_READ_ALL):
        raise Forbidden("没有查看全员事项的权限")
    now = now or utcnow()
    tz = await _tz(session)
    is_open = StaffTask.status == TaskStatus.OPEN
    per_owner = (
        select(
            StaffTask.owner_id.label("owner_id"),
            func.count().filter(is_open).label("open"),
            func.count().filter(_due("today", now, tz)).label("due_today"),
            func.count().filter(_due("overdue", now, tz)).label("overdue"),
        )
        .group_by(StaffTask.owner_id)
        .subquery()
    )
    rows = await session.execute(
        select(
            Staff.id,
            Staff.display_name,
            func.coalesce(per_owner.c.open, 0),
            func.coalesce(per_owner.c.due_today, 0),
            func.coalesce(per_owner.c.overdue, 0),
        )
        .outerjoin(per_owner, per_owner.c.owner_id == Staff.id)
        .where(Staff.status == StaffStatus.ACTIVE)
        .order_by(
            func.coalesce(per_owner.c.overdue, 0).desc(),
            func.coalesce(per_owner.c.open, 0).desc(),
            Staff.display_name,
            Staff.id,
        )
    )
    return TaskOverview(
        items=[
            TaskOverviewRow(
                staff_id=staff_id, name=name, open=open_, due_today=due_today, overdue=overdue
            )
            for staff_id, name, open_, due_today, overdue in rows
        ]
    )


async def staff_options(session: AsyncSession) -> StaffOptions:
    rows = await session.execute(
        select(Staff.id, Staff.display_name)
        .where(Staff.status == StaffStatus.ACTIVE)
        .order_by(Staff.display_name, Staff.id)
    )
    return StaffOptions(items=[StaffOption(id=i, name=n) for i, n in rows])


# ---- 操作 ----


def _check_due(due_at: datetime | None, now: datetime) -> None:
    if due_at is not None and due_at < now - timedelta(days=365):
        raise Unprocessable("截止时间不对")


async def create(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    payload: TaskCreate,
    *,
    source: TaskSource | None = None,
    now: datetime | None = None,
) -> StaffTask:
    """新建或交办一条事项（提交事务，并提醒接收人）。"""
    now = now or utcnow()
    tenant_id = principal.tenant_id
    owner_id = payload.owner_id or principal.staff_id
    assigning = owner_id != principal.staff_id
    if assigning:
        if not principal.has(Permission.TASK_ASSIGN):
            raise Forbidden("没有给别人布置事项的权限")
        owner = await session.get(Staff, owner_id)
        if owner is None or owner.status != StaffStatus.ACTIVE:
            raise Unprocessable("接收人不存在或已停用")
    _check_due(payload.due_at, now)
    settings = await task_settings.load(session, tenant_id)
    spec = await sla.business_hours(session)
    task = StaffTask(
        tenant_id=tenant_id,
        no=await next_number(
            session,
            tenant_id,
            scope=NUMBER_SCOPE,
            prefix=NUMBER_PREFIX,
            now=now,
            tz=sla.tz_of(spec),
        ),
        owner_id=owner_id,
        title=payload.title.strip(),
        note=payload.note.strip(),
        priority=payload.priority.value,
        status=TaskStatus.OPEN.value,
        source=(source or (TaskSource.ASSIGNED if assigning else TaskSource.SELF)).value,
        due_at=payload.due_at,
        remind_before_minutes=(
            payload.remind_before_minutes
            if payload.remind_before_minutes is not None
            else settings.remind_before_minutes
        ),
        link=payload.link,
        created_by=principal.staff_id,
    )
    session.add(task)
    await session.flush()
    if assigning:
        notifications.add(
            session,
            tenant_id,
            [owner_id],
            kind="task_assigned",
            title=f"{principal.display_name} 交办：{task.title}"[:200],
            body=_due_text(task, spec),
            link=link(task),
        )
    await session.commit()
    if assigning:
        await notify_staff(
            ctx,
            tenant_id,
            [owner_id],
            title=f"{principal.display_name} 交办：{task.title}"[:128],
            description=_due_text(task, spec),
            path=link(task),
        )
    return task


def _due_text(task: StaffTask, spec: dict[str, Any] | None) -> str:
    if task.due_at is None:
        return "没有截止时间。"
    return f"截止 {task.due_at.astimezone(sla.tz_of(spec)):%m-%d %H:%M}。"


async def update(
    session: AsyncSession,
    principal: Principal,
    task_id: uuid.UUID,
    payload: TaskUpdate,
    *,
    now: datetime | None = None,
) -> StaffTask:
    now = now or utcnow()
    task = await _editable(session, principal, task_id)
    if task.status != TaskStatus.OPEN:
        raise Conflict("已完成或已取消的事项不能修改，请先重新打开")
    changes = payload.model_dump(exclude_unset=True)
    if "title" in changes and changes["title"] is not None:
        task.title = changes["title"].strip()
    if "note" in changes and changes["note"] is not None:
        task.note = changes["note"].strip()
    if "priority" in changes and changes["priority"] is not None:
        task.priority = TaskPriority(changes["priority"]).value
    if "link" in changes:
        task.link = changes["link"]
    if "remind_before_minutes" in changes and changes["remind_before_minutes"] is not None:
        task.remind_before_minutes = changes["remind_before_minutes"]
        task.reminded_at = None
    if "due_at" in changes:
        _check_due(changes["due_at"], now)
        if changes["due_at"] != task.due_at:
            task.due_at = changes["due_at"]
            # 改了截止时间重新计算提醒。
            task.reminded_at = None
            task.overdue_notified_at = None
    await session.commit()
    return task


async def done(
    session: AsyncSession,
    principal: Principal,
    task_id: uuid.UUID,
    *,
    note: str | None = None,
    now: datetime | None = None,
) -> StaffTask:
    task = await _editable(session, principal, task_id)
    if task.status == TaskStatus.DONE:
        return task
    if task.status == TaskStatus.CANCELLED:
        raise Conflict("已取消的事项不能完成，请先重新打开")
    task.status = TaskStatus.DONE.value
    task.done_at = now or utcnow()
    task.done_note = (note or "").strip() or None
    await session.commit()
    return task


async def reopen(session: AsyncSession, principal: Principal, task_id: uuid.UUID) -> StaffTask:
    task = await _editable(session, principal, task_id)
    if task.status == TaskStatus.OPEN:
        return task
    task.status = TaskStatus.OPEN.value
    task.done_at = None
    task.done_note = None
    task.cancelled_at = None
    task.reminded_at = None
    task.overdue_notified_at = None
    await session.commit()
    return task


async def cancel(
    session: AsyncSession, principal: Principal, task_id: uuid.UUID, *, now: datetime | None = None
) -> StaffTask:
    task = await _editable(session, principal, task_id)
    if task.status == TaskStatus.CANCELLED:
        return task
    if task.status == TaskStatus.DONE:
        raise Conflict("已完成的事项不能取消")
    task.status = TaskStatus.CANCELLED.value
    task.cancelled_at = now or utcnow()
    await session.commit()
    return task


async def mark_done_by_no(
    session: AsyncSession, principal: Principal, no_or_title: str, *, now: datetime | None = None
) -> StaffTask | None:
    """AI 助理用：按编号或标题找到自己的一条未完成事项并完成；找不到或不唯一时返回 None。"""
    key = no_or_title.strip()
    if not key:
        return None
    rows = (
        await session.scalars(
            select(StaffTask)
            .where(
                StaffTask.owner_id == principal.staff_id,
                StaffTask.status == TaskStatus.OPEN,
                or_(StaffTask.no == key, StaffTask.title.ilike(f"%{key}%")),
            )
            .limit(2)
        )
    ).all()
    if len(rows) != 1:
        return None
    return await done(session, principal, rows[0].id, now=now)
