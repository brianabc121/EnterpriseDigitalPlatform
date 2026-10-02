"""分派（设计文档 §24.6）：按类型的规则依次取第一个可用的处理人；
待确认的待办按同样的规则确定确认人。

规则的步骤：
- session_agent：来自人工会话的，交给会话坐席（坐席答应客户的事由他本人处理）；
- owner：客户的归属坐席；
- channel_group：渠道默认技能组（排队超时的留言用会话所在的技能组）；
- skill_group：类型指定的技能组；
- staff：类型指定的员工。

技能组按 group_mode 分派：least_loaded 交给组内未完成待办最少的成员，pool 放进组内的待认领池。
以上都没有时进入租户的公共待认领池，由主管或管理员分派。只分派给启用状态的员工。
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.modules.conversation.models import ChatSession
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.service import active_staff_permissions
from app.modules.routing.assign import PolicyResolver
from app.modules.routing.models import SkillGroup, SkillGroupMember
from app.modules.todos import events
from app.modules.todos.models import (
    UNFINISHED,
    ActorType,
    NotifyReason,
    Todo,
    TodoStatus,
    TodoType,
)

STEPS = ("session_agent", "owner", "channel_group", "skill_group", "staff")
GROUP_MODES = ("least_loaded", "pool")


@dataclass(frozen=True)
class Target:
    assignee_id: uuid.UUID | None
    skill_group_id: uuid.UUID | None
    by: str  # 命中的步骤；pool 表示待认领池


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


async def _active(
    session: AsyncSession, staff_id: uuid.UUID | None, exclude: Collection[uuid.UUID]
) -> bool:
    if staff_id is None or staff_id in exclude:
        return False
    status = await session.scalar(select(Staff.status).where(Staff.id == staff_id))
    return status == StaffStatus.ACTIVE


async def _group(
    session: AsyncSession,
    group_id: uuid.UUID | None,
    mode: str,
    exclude: Collection[uuid.UUID],
) -> Target | None:
    if group_id is None:
        return None
    if await session.scalar(select(SkillGroup.id).where(SkillGroup.id == group_id)) is None:
        return None
    if mode != "least_loaded":
        return Target(None, group_id, "pool")
    load = (
        select(Todo.assignee_id, func.count().label("n"))
        .where(Todo.status.in_(UNFINISHED), Todo.assignee_id.is_not(None))
        .group_by(Todo.assignee_id)
        .subquery()
    )
    query = (
        select(SkillGroupMember.staff_id)
        .join(Staff, Staff.id == SkillGroupMember.staff_id)
        .outerjoin(load, load.c.assignee_id == SkillGroupMember.staff_id)
        .where(SkillGroupMember.skill_group_id == group_id, Staff.status == StaffStatus.ACTIVE)
        .order_by(func.coalesce(load.c.n, 0), SkillGroupMember.created_at, Staff.id)
        .limit(1)
    )
    if exclude:
        query = query.where(SkillGroupMember.staff_id.not_in(list(exclude)))
    staff_id = await session.scalar(query)
    if staff_id is None:
        return Target(None, group_id, "pool")
    return Target(staff_id, group_id, "skill_group")


async def resolve(
    session: AsyncSession,
    type_: TodoType,
    *,
    customer_id: uuid.UUID | None,
    session_id: uuid.UUID | None,
    channel_account_id: uuid.UUID | None = None,
    group_hint: uuid.UUID | None = None,
    exclude: Collection[uuid.UUID] = (),
) -> Target:
    rule = type_.assign_rule or {}
    mode = str(rule.get("group_mode") or "pool")
    for step in rule.get("steps") or ["owner"]:
        if step == "session_agent" and session_id is not None:
            agent = await session.scalar(
                select(ChatSession.assignee_id).where(ChatSession.id == session_id)
            )
            if await _active(session, agent, exclude):
                return Target(agent, None, step)
        elif step == "owner" and customer_id is not None:
            owner = await session.scalar(
                select(Customer.owner_id).where(Customer.id == customer_id)
            )
            if await _active(session, owner, exclude):
                return Target(owner, None, step)
        elif step == "channel_group":
            group = group_hint
            if group is None:
                resolver = PolicyResolver(session)
                policy = (
                    await resolver.for_channel(channel_account_id)
                    if channel_account_id is not None
                    else await resolver.default()
                )
                group = policy.default_skill_group_id
            found = await _group(session, group, mode, exclude)
            if found is not None:
                return found
        elif step == "skill_group":
            found = await _group(session, _uuid(rule.get("skill_group_id")), mode, exclude)
            if found is not None:
                return found
        elif step == "staff":
            staff_id = _uuid(rule.get("staff_id"))
            if await _active(session, staff_id, exclude):
                return Target(staff_id, None, step)
    return Target(None, None, "pool")


# ---- 提醒的收件人 ----


async def group_members(session: AsyncSession, group_id: uuid.UUID) -> list[uuid.UUID]:
    """技能组里启用状态的成员。"""
    return list(
        (
            await session.scalars(
                select(SkillGroupMember.staff_id)
                .join(Staff, Staff.id == SkillGroupMember.staff_id)
                .where(
                    SkillGroupMember.skill_group_id == group_id,
                    Staff.status == StaffStatus.ACTIVE,
                )
                .order_by(SkillGroupMember.created_at)
            )
        ).all()
    )


async def staff_with(session: AsyncSession, permission: Permission) -> list[uuid.UUID]:
    """有某项权限的启用状态的员工（按员工设置的权限也算，§31.4）。"""
    return [s.id for s, granted in await active_staff_permissions(session) if permission in granted]


async def supervisors(session: AsyncSession, todo: Todo) -> list[uuid.UUID]:
    """升级和投诉提醒的主管：处理人所在技能组、待办所在技能组的组长；都没有时是能分派待办的人。"""
    groups = select(SkillGroupMember.skill_group_id).where(
        SkillGroupMember.staff_id == todo.assignee_id
    )
    conditions: list[ColumnElement[bool]] = (
        [SkillGroupMember.skill_group_id.in_(groups)] if todo.assignee_id else []
    )
    if todo.skill_group_id is not None:
        conditions.append(SkillGroupMember.skill_group_id == todo.skill_group_id)
    leads: list[uuid.UUID] = []
    for condition in conditions:
        leads += (
            await session.scalars(
                select(SkillGroupMember.staff_id)
                .join(Staff, Staff.id == SkillGroupMember.staff_id)
                .where(condition, SkillGroupMember.is_lead, Staff.status == StaffStatus.ACTIVE)
            )
        ).all()
    found = [s for s in dict.fromkeys(leads) if s != todo.assignee_id]
    if found:
        return found
    return [s for s in await staff_with(session, Permission.TODO_ASSIGN) if s != todo.assignee_id]


async def recipients(session: AsyncSession, todo: Todo) -> list[uuid.UUID]:
    """处理人（或确认人）；在技能组待认领池时是组员；在公共待认领池时是能分派待办的人。"""
    if todo.assignee_id is not None:
        return [todo.assignee_id]
    if todo.skill_group_id is not None:
        members = await group_members(session, todo.skill_group_id)
        if members:
            return members
    return await staff_with(session, Permission.TODO_ASSIGN)


async def reassign_from(
    session: AsyncSession, staff_id: uuid.UUID, *, actor_id: uuid.UUID | None
) -> int:
    """员工被停用：未完成的待办按规则重新分派（排除这名员工），返回数量。由调用方提交。"""
    todos = (
        await session.scalars(
            select(Todo)
            .where(Todo.assignee_id == staff_id, Todo.status.in_(UNFINISHED))
            .with_for_update()
        )
    ).all()
    types = {t.id: t for t in await session.scalars(select(TodoType))} if todos else {}
    for todo in todos:
        target = await resolve(
            session,
            types[todo.type_id],
            customer_id=todo.customer_id,
            session_id=todo.session_id,
            exclude={staff_id},
        )
        todo.assignee_id, todo.skill_group_id = target.assignee_id, target.skill_group_id
        todo.assigned_by = actor_id
        todo.notify_reason = (
            NotifyReason.PENDING if todo.status == TodoStatus.PENDING else NotifyReason.ASSIGNED
        )
        todo.notified_at = None
        events.record(
            session,
            todo,
            "assigned",
            actor_type=ActorType.SYSTEM,
            payload={
                "from": str(staff_id),
                "to": str(target.assignee_id) if target.assignee_id else None,
                "skill_group_id": str(target.skill_group_id) if target.skill_group_id else None,
                "reason": "staff_disabled",
            },
        )
    return len(todos)
