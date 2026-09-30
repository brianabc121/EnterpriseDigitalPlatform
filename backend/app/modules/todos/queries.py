"""待办中心的查询（设计文档 §24.9）：视图、筛选、详情和菜单角标的数量。"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, case, func, inspect, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.modules.conversation.models import Message
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.routing.models import SkillGroup
from app.modules.todos import actions, sla
from app.modules.todos import fields as todo_fields
from app.modules.todos.models import (
    ACTIVE,
    Priority,
    Todo,
    TodoEvent,
    TodoStatus,
    TodoType,
)
from app.modules.todos.schemas import (
    AssigneeOption,
    AssigneeOptions,
    DueFilter,
    EvidenceMessage,
    FieldValue,
    TodoAllowed,
    TodoCounts,
    TodoDetail,
    TodoEventOut,
    TodoOut,
    TodoPage,
    View,
)
from app.modules.todos.service import get_visible, my_groups, visible_to

EVIDENCE_LIMIT = 20


def utcnow() -> datetime:
    return datetime.now(UTC)


def _pending_for(principal: Principal) -> ColumnElement[bool]:
    """等我确认的：交给我确认的、所在技能组待认领的；能分派待办的人是数据范围内的全部。"""
    if principal.has(Permission.TODO_ASSIGN):
        return Todo.status == TodoStatus.PENDING
    me = principal.staff_id
    return and_(
        Todo.status == TodoStatus.PENDING,
        or_(
            Todo.assignee_id == me,
            and_(Todo.assignee_id.is_(None), Todo.skill_group_id.in_(my_groups(me))),
            and_(Todo.assignee_id.is_(None), Todo.skill_group_id.is_(None)),
        ),
    )


def _pool_for(principal: Principal) -> ColumnElement[bool]:
    """我能认领的：所在技能组待认领的；能分派待办的人另外能看到公共待认领池。"""
    me = principal.staff_id
    pools: list[ColumnElement[bool]] = [Todo.skill_group_id.in_(my_groups(me))]
    if principal.has(Permission.TODO_ASSIGN):
        pools.append(Todo.skill_group_id.is_(None))
    return and_(Todo.assignee_id.is_(None), Todo.status.in_(ACTIVE), or_(*pools))


def _view(principal: Principal, view: View) -> ColumnElement[bool] | None:
    me = principal.staff_id
    match view:
        case "pending":
            return _pending_for(principal)
        case "mine":
            return and_(Todo.assignee_id == me, Todo.status.in_(ACTIVE))
        case "pool":
            return _pool_for(principal)
        case "assigned":
            return and_(Todo.assigned_by == me, Todo.assignee_id.is_distinct_from(me))
    return None


def _priority_rank() -> Any:
    return case(
        {Priority.URGENT.value: 4, Priority.HIGH.value: 3, Priority.NORMAL.value: 2},
        value=Todo.priority,
        else_=1,
    )


async def _tz(session: AsyncSession) -> Any:
    return sla.tz_of(await sla.business_hours(session))


def _due(due: DueFilter, now: datetime, tz: Any) -> ColumnElement[bool]:
    active = Todo.status.in_(ACTIVE)
    if due == "overdue":
        return and_(active, Todo.due_at < now)
    if due == "soon":
        return and_(active, Todo.due_at >= now, Todo.due_at < now + timedelta(hours=24))
    today = now.astimezone(tz).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
    return and_(active, Todo.due_at >= start, Todo.due_at < start + timedelta(days=1))


async def conditions(
    session: AsyncSession,
    principal: Principal,
    *,
    view: View = "all",
    status: TodoStatus | None = None,
    type_id: uuid.UUID | None = None,
    priority: Priority | None = None,
    source: str | None = None,
    customer_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    due: DueFilter | None = None,
    q: str | None = None,
    now: datetime | None = None,
) -> ColumnElement[bool]:
    """待办中心的筛选条件（数据范围内），列表和导出共用。"""
    now = now or utcnow()
    where: list[ColumnElement[bool]] = [visible_to(principal)]
    condition = _view(principal, view)
    if condition is not None:
        where.append(condition)
    if status is not None:
        where.append(Todo.status == status)
    elif view == "all":
        where.append(Todo.status != TodoStatus.PENDING)
    for column, value in (
        (Todo.type_id, type_id),
        (Todo.priority, priority),
        (Todo.source, source),
        (Todo.customer_id, customer_id),
        (Todo.session_id, session_id),
        (Todo.assignee_id, assignee_id),
    ):
        if value is not None:
            where.append(column == value)
    if due is not None:
        where.append(_due(due, now, await _tz(session)))
    if q:
        pattern = f"%{q.strip()}%"
        where.append(or_(Todo.no.ilike(pattern), Todo.title.ilike(pattern)))
    return and_(*where)


async def list_todos(
    session: AsyncSession,
    principal: Principal,
    *,
    view: View = "all",
    status: TodoStatus | None = None,
    type_id: uuid.UUID | None = None,
    priority: Priority | None = None,
    source: str | None = None,
    customer_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    due: DueFilter | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
    now: datetime | None = None,
) -> TodoPage:
    now = now or utcnow()
    query: Select[Todo] = select(Todo).where(
        await conditions(
            session,
            principal,
            view=view,
            status=status,
            type_id=type_id,
            priority=priority,
            source=source,
            customer_id=customer_id,
            session_id=session_id,
            assignee_id=assignee_id,
            due=due,
            q=q,
            now=now,
        )
    )
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    if view in ("mine", "pool"):
        order: list[Any] = [Todo.due_at.asc().nulls_last(), _priority_rank().desc()]
    else:
        order = [Todo.created_at.desc()]
    rows = (
        await session.scalars(query.order_by(*order, Todo.id.desc()).limit(limit).offset(offset))
    ).all()
    return TodoPage(items=await outs(session, rows, now=now), total=total or 0)


async def _names(
    session: AsyncSession, model: Any, column: Any, ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.execute(select(model.id, column).where(model.id.in_(wanted)))
    return {row_id: name for row_id, name in rows}


async def outs(
    session: AsyncSession, todos: Iterable[Todo], *, now: datetime | None = None
) -> list[TodoOut]:
    rows = list(todos)
    now = now or utcnow()
    for row in rows:
        # 提交后由数据库生成的值（updated_at）已过期，先重新读取。
        if inspect(row).expired_attributes:
            await session.refresh(row)
    types = {
        t.id: t
        for t in await session.scalars(
            select(TodoType).where(TodoType.id.in_({r.type_id for r in rows}))
        )
    }
    customers = await _names(
        session, Customer, Customer.display_name, (r.customer_id for r in rows)
    )
    staff = await _names(
        session,
        Staff,
        Staff.display_name,
        [s for r in rows for s in (r.assignee_id, r.created_by)],
    )
    groups = await _names(session, SkillGroup, SkillGroup.name, (r.skill_group_id for r in rows))
    return [_out(r, types.get(r.type_id), customers, staff, groups, now) for r in rows]


def _out(
    todo: Todo,
    type_: TodoType | None,
    customers: dict[uuid.UUID, str],
    staff: dict[uuid.UUID, str],
    groups: dict[uuid.UUID, str],
    now: datetime,
) -> TodoOut:
    sensitive = {s["key"] for s in (type_.fields if type_ else []) or [] if s.get("sensitive")}
    return TodoOut(
        id=todo.id,
        no=todo.no,
        type_id=todo.type_id,
        type_code=type_.code if type_ else "",
        type_name=type_.name if type_ else "",
        title=todo.title,
        detail=todo.detail,
        fields=[
            FieldValue(key=key, label=label, value=value, sensitive=key in sensitive)
            for key, label, value in todo_fields.labelled(type_, todo.fields or {})
        ],
        customer_id=todo.customer_id,
        customer_name=customers.get(todo.customer_id) if todo.customer_id else None,
        session_id=todo.session_id,
        order_id=todo.order_id,
        source=todo.source,
        confidence=todo.confidence,
        priority=todo.priority,
        status=todo.status,
        assignee_id=todo.assignee_id,
        assignee_name=staff.get(todo.assignee_id) if todo.assignee_id else None,
        skill_group_id=todo.skill_group_id,
        skill_group_name=groups.get(todo.skill_group_id) if todo.skill_group_id else None,
        assigned_by=todo.assigned_by,
        expected_at=todo.expected_at,
        due_at=todo.due_at,
        overdue=bool(todo.due_at and todo.due_at < now and todo.status in ACTIVE),
        respond_due_at=todo.respond_due_at,
        first_response_at=todo.first_response_at,
        confirmed_at=todo.confirmed_at,
        confirmed_by=todo.confirmed_by,
        closed_at=todo.closed_at,
        result=todo.result,
        reject_reason=todo.reject_reason,
        close_note=todo.close_note,
        progress_note=todo.progress_note,
        nudge_count=todo.nudge_count,
        evidence_count=len(todo.evidence_message_ids or []),
        created_by_type=todo.created_by_type,
        created_by=todo.created_by,
        created_by_name=staff.get(todo.created_by) if todo.created_by else None,
        created_at=todo.created_at,
        updated_at=todo.updated_at,
    )


async def out(session: AsyncSession, todo: Todo) -> TodoOut:
    [result] = await outs(session, [todo])
    return result


async def detail(session: AsyncSession, principal: Principal, todo_id: uuid.UUID) -> TodoDetail:
    todo = await get_visible(session, principal, todo_id)
    base = await out(session, todo)
    type_ = await session.get(TodoType, todo.type_id)
    rows = (
        await session.scalars(
            select(TodoEvent)
            .where(TodoEvent.todo_id == todo.id)
            .order_by(TodoEvent.created_at, TodoEvent.id)
        )
    ).all()
    actors = await _names(session, Staff, Staff.display_name, (e.actor_id for e in rows))
    evidence: list[EvidenceMessage] = []
    ids = list(todo.evidence_message_ids or [])[-EVIDENCE_LIMIT:]
    if ids:
        messages = await session.scalars(
            select(Message).where(Message.id.in_(ids)).order_by(Message.sent_at, Message.id)
        )
        evidence = [
            EvidenceMessage(
                id=m.id,
                session_id=m.session_id,
                sender_type=m.sender_type,
                text=m.text_plain or f"[{m.content_type}]",
                sent_at=m.sent_at,
            )
            for m in messages
        ]
    return TodoDetail(
        **base.model_dump(),
        type_fields=list(type_.fields or []) if type_ else [],
        events=[
            TodoEventOut(
                id=e.id,
                type=e.type,
                actor_type=e.actor_type,
                actor_id=e.actor_id,
                actor_name=actors.get(e.actor_id) if e.actor_id else None,
                payload=e.payload or {},
                created_at=e.created_at,
            )
            for e in rows
        ],
        evidence=evidence,
        allowed=TodoAllowed(
            confirm=await actions.can_confirm(session, principal, todo),
            handle=todo.status in (*ACTIVE,) and actions.can_handle(principal, todo),
            claim=await actions.can_claim(session, principal, todo),
            assign=principal.has(Permission.TODO_ASSIGN)
            or (principal.has(Permission.TODO_HANDLE) and todo.assignee_id == principal.staff_id),
        ),
    )


async def counts(
    session: AsyncSession, principal: Principal, *, now: datetime | None = None
) -> TodoCounts:
    now = now or utcnow()
    tz = await _tz(session)
    me = principal.staff_id
    visible = visible_to(principal)
    mine = and_(Todo.assignee_id == me, Todo.status.in_(ACTIVE))
    row = (
        await session.execute(
            select(
                func.count().filter(_pending_for(principal)),
                func.count().filter(mine),
                func.count().filter(mine, _due("today", now, tz)),
                func.count().filter(mine, _due("overdue", now, tz)),
                func.count().filter(_pool_for(principal)),
            ).where(visible, Todo.status.in_((TodoStatus.PENDING, *ACTIVE)))
        )
    ).one()
    pending, mine_count, due_today, overdue, pool = row
    return TodoCounts(
        pending=pending, mine=mine_count, due_today=due_today, overdue=overdue, pool=pool
    )


async def assignees(session: AsyncSession) -> AssigneeOptions:
    staff = await session.execute(
        select(Staff.id, Staff.display_name)
        .where(Staff.status == StaffStatus.ACTIVE)
        .order_by(Staff.display_name, Staff.id)
    )
    groups = await session.execute(
        select(SkillGroup.id, SkillGroup.name).order_by(SkillGroup.name, SkillGroup.id)
    )
    return AssigneeOptions(
        staff=[AssigneeOption(id=i, name=n) for i, n in staff],
        groups=[AssigneeOption(id=i, name=n) for i, n in groups],
    )
