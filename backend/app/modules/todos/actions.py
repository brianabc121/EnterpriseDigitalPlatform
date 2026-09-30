"""员工对待办的操作（设计文档 §24.5）：新建、确认、驳回、合并、认领、开始处理、等待客户、恢复、
完成、取消、重新打开、分派与转交、改期、评论、修改和通知客户。所有操作写入待办动态。

权限：todo:handle 处理自己的待办、确认交给自己确认的待办；todo:assign 可以处理、分派数据范围内的
任何待办。每个操作提交后立即发送提醒（调度进程另有兜底）。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import AppError, Conflict, Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.conversation import outbox
from app.modules.conversation.models import ChatSession
from app.modules.customer.models import Customer
from app.modules.customer.service import visible_to as customer_visible_to
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.notifications import service as notifications
from app.modules.routing.models import SkillGroup, SkillGroupMember
from app.modules.todos import assign, events, notify, sla
from app.modules.todos import fields as todo_fields
from app.modules.todos import service as todos
from app.modules.todos import settings as todo_settings
from app.modules.todos.models import (
    ACTIVE,
    TIMED,
    UNFINISHED,
    ActorType,
    NotifyReason,
    RejectReason,
    Todo,
    TodoSource,
    TodoStatus,
    TodoType,
)
from app.modules.todos.schemas import (
    AssignRequest,
    BatchFailure,
    BatchRequest,
    BatchResult,
    CommentRequest,
    ConfirmRequest,
    DoneRequest,
    FieldValue,
    MergeRequest,
    NotifyResult,
    RejectRequest,
    RescheduleRequest,
    TodoCreate,
    TodoUpdate,
)
from app.modules.wecom.notify import notify_staff


def utcnow() -> datetime:
    return datetime.now(UTC)


async def _type(session: AsyncSession, type_id: uuid.UUID) -> TodoType:
    row = await session.get(TodoType, type_id)
    if row is None:
        raise NotFound("待办类型不存在")
    return row


async def _in_group(session: AsyncSession, staff_id: uuid.UUID, group_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(SkillGroupMember.staff_id).where(
            SkillGroupMember.staff_id == staff_id, SkillGroupMember.skill_group_id == group_id
        )
    )
    return found is not None


async def _active_staff(session: AsyncSession, staff_id: uuid.UUID) -> None:
    status = await session.scalar(select(Staff.status).where(Staff.id == staff_id))
    if status != StaffStatus.ACTIVE:
        raise Unprocessable("处理人不存在或已停用")


async def _group_exists(session: AsyncSession, group_id: uuid.UUID) -> None:
    if await session.get(SkillGroup, group_id) is None:
        raise Unprocessable("技能组不存在")


# ---- 能做什么 ----


async def can_confirm(session: AsyncSession, principal: Principal, todo: Todo) -> bool:
    """确认人（按分派规则确定）、待认领池所在技能组的成员，以及能分派待办的人。"""
    if todo.status != TodoStatus.PENDING:
        return False
    if principal.has(Permission.TODO_ASSIGN):
        return True
    if not principal.has(Permission.TODO_HANDLE):
        return False
    if todo.assignee_id is not None:
        return todo.assignee_id == principal.staff_id
    if todo.skill_group_id is not None:
        return await _in_group(session, principal.staff_id, todo.skill_group_id)
    return True


def can_handle(principal: Principal, todo: Todo) -> bool:
    if principal.has(Permission.TODO_ASSIGN):
        return True
    return principal.has(Permission.TODO_HANDLE) and todo.assignee_id == principal.staff_id


async def can_claim(session: AsyncSession, principal: Principal, todo: Todo) -> bool:
    if todo.status not in ACTIVE or todo.assignee_id is not None:
        return False
    if not principal.has(Permission.TODO_HANDLE):
        return False
    if todo.skill_group_id is None or principal.has(Permission.TODO_ASSIGN):
        return True
    return await _in_group(session, principal.staff_id, todo.skill_group_id)


def _require_handle(principal: Principal, todo: Todo) -> None:
    if not can_handle(principal, todo):
        raise Forbidden("只能处理分派给自己的待办")


def _status(todo: Todo, allowed: tuple[str, ...], message: str) -> None:
    if todo.status not in allowed:
        raise Conflict(message)


def _stop_timers(todo: Todo) -> None:
    todo.pending_remind_at = todo.remind_at = todo.escalate_at = None


def _notify_later(todo: Todo, reason: NotifyReason, actor: uuid.UUID) -> None:
    """提交后提醒处理人（或确认人）；分派给操作人自己时不提醒。"""
    todo.notify_reason = reason
    todo.notified_at = utcnow() if todo.assignee_id == actor else None


async def _after(ctx: AppContext, todo: Todo, rooms: set[uuid.UUID] | None = None) -> None:
    await notify.dispatch(ctx, tenant_id=todo.tenant_id, ids=[todo.id])
    if rooms:
        await outbox.flush_rooms(ctx, todo.tenant_id, rooms)


async def _plain_fields(ctx: AppContext, todo: Todo) -> dict[str, str]:
    return await todo_fields.reveal(ctx.keys, todo.tenant_id, todo.fields or {})


def _clean_or_raise(type_: TodoType, values: dict[str, Any]) -> dict[str, str]:
    cleaned = todo_fields.clean(type_, values)
    problems = cleaned.problems()
    if problems:
        raise Unprocessable("；".join(problems))
    return cleaned.values


# ---- 新建 ----


async def create(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: TodoCreate
) -> Todo:
    """员工新建（包括由 AI 预填、员工核对后保存的）：直接进入待办列表。"""
    type_ = await _type(session, payload.type_id)
    if not type_.enabled or type_.system:
        raise Unprocessable("这个类型不能手工新建")
    customer_id = payload.customer_id
    channel_account_id = None
    if payload.session_id is not None:
        chat = await session.get(ChatSession, payload.session_id)
        if chat is None:
            raise Unprocessable("会话不存在")
        customer_id = customer_id or chat.customer_id
        channel_account_id = chat.channel_account_id
        if customer_id != chat.customer_id:
            raise Unprocessable("会话不属于这位客户")
    if customer_id is not None:
        visible = await session.scalar(
            select(Customer.id).where(Customer.id == customer_id, customer_visible_to(principal))
        )
        if visible is None:
            raise Unprocessable("客户不存在或不在你的数据范围内")
    explicit = payload.assignee_id is not None or payload.skill_group_id is not None
    if payload.assignee_id is not None:
        if payload.assignee_id != principal.staff_id and not principal.has(Permission.TODO_ASSIGN):
            raise Forbidden("没有分派待办的权限，只能新建给自己或按规则分派")
        await _active_staff(session, payload.assignee_id)
    if payload.skill_group_id is not None:
        await _group_exists(session, payload.skill_group_id)
    now = utcnow()
    if payload.due_at is not None and payload.due_at <= now:
        raise Unprocessable("截止时间必须晚于现在")
    todo = await todos.create(
        session,
        ctx.keys,
        todos.Draft(
            type=type_,
            title=payload.title,
            detail=payload.detail,
            fields=_clean_or_raise(type_, payload.fields),
            source=TodoSource(payload.source),
            created_by_type=ActorType.STAFF,
            created_by=principal.staff_id,
            customer_id=customer_id,
            session_id=payload.session_id,
            channel_account_id=channel_account_id,
            evidence_message_ids=list(payload.evidence_message_ids),
            expected_at=payload.expected_at,
            due_at=payload.due_at,
            priority=payload.priority,
            explicit=explicit,
            assignee_id=payload.assignee_id,
            skill_group_id=payload.skill_group_id,
        ),
        now=now,
    )
    await session.commit()
    await _after(ctx, todo)
    return todo


# ---- 待确认 ----


async def confirm(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    payload: ConfirmRequest,
    *,
    commit: bool = True,
) -> Todo:
    """确认（可以先修改类型、标题、字段、处理人和截止时间）：进入待办列表，按规则分派并提醒处理人。"""
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _status(todo, (TodoStatus.PENDING,), "只有待确认的待办可以确认")
    if not await can_confirm(session, principal, todo):
        raise Forbidden("这条待办不是交给你确认的")
    me = principal.staff_id
    now = utcnow()
    type_ = await _type(session, payload.type_id or todo.type_id)
    if not type_.enabled:
        raise Unprocessable("这个类型已停用")
    changes: dict[str, Any] = {}
    type_changed = type_.id != todo.type_id
    if type_changed:
        changes["type_id"] = {"from": str(todo.type_id), "to": str(type_.id)}
        todo.type_id = type_.id
    if payload.title is not None and payload.title.strip() != todo.title:
        changes["title"] = {"from": todo.title, "to": payload.title.strip()}
        todo.title = payload.title.strip()
    if payload.detail is not None and payload.detail.strip() != todo.detail:
        changes["detail"] = True
        todo.detail = payload.detail.strip()
    # 会话后解析的待办可能缺少必填字段：确认时必须补全（批量确认、会话里的快速确认也一样）。
    stored = await _plain_fields(ctx, todo)
    values = {**stored, **(payload.fields or {})}
    cleaned = _clean_or_raise(type_, values)
    if payload.fields is not None or type_changed:
        if cleaned != stored:
            changes["fields"] = sorted(cleaned)
        todo.fields = await todo_fields.seal(ctx.keys, todo.tenant_id, type_, cleaned)
    if payload.priority is not None and payload.priority != todo.priority:
        changes["priority"] = {"from": todo.priority, "to": payload.priority.value}
        todo.priority = payload.priority
    previous = todo.assignee_id
    if payload.assignee_id is not None:
        await _active_staff(session, payload.assignee_id)
        todo.assignee_id, todo.skill_group_id = payload.assignee_id, payload.skill_group_id
    elif payload.skill_group_id is not None:
        await _group_exists(session, payload.skill_group_id)
        todo.assignee_id, todo.skill_group_id = None, payload.skill_group_id
    elif type_changed or todo.assignee_id is None:
        # 类型改了，或者还没有具体的处理人（例如 AI 登记时会话还没有坐席接待）：按规则重新分派，
        # 规则仍然没有具体的人时留在原来的待认领池。
        target = await assign.resolve(
            session, type_, customer_id=todo.customer_id, session_id=todo.session_id
        )
        if type_changed or target.assignee_id is not None:
            todo.assignee_id, todo.skill_group_id = target.assignee_id, target.skill_group_id
    if todo.assignee_id != previous:
        changes["assignee_id"] = {
            "from": str(previous) if previous else None,
            "to": str(todo.assignee_id) if todo.assignee_id else None,
        }
        if todo.assignee_id not in (None, me):
            todo.assigned_by = me
    if payload.due_at is not None and payload.due_at <= now:
        raise Unprocessable("截止时间必须晚于现在")
    todo.status = TodoStatus.OPEN
    todo.confirmed_at, todo.confirmed_by = now, me
    todo.pending_remind_at = None
    sla.start_clock(todo, type_, await sla.business_hours(session), now, due_at=payload.due_at)
    _notify_later(todo, NotifyReason.ASSIGNED, me)
    events.record(
        session,
        todo,
        "confirmed",
        actor_type=ActorType.STAFF,
        actor_id=me,
        payload={"modified": bool(changes), "changes": changes},
    )
    if commit:
        await session.commit()
        await _after(ctx, todo)
    return todo


async def reject(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    payload: RejectRequest,
    *,
    commit: bool = True,
) -> Todo:
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _status(todo, (TodoStatus.PENDING,), "只有待确认的待办可以驳回")
    if not await can_confirm(session, principal, todo):
        raise Forbidden("这条待办不是交给你确认的")
    todo.status = TodoStatus.REJECTED
    todo.reject_reason = payload.reason
    todo.close_note = payload.note
    todo.closed_at = utcnow()
    todo.notified_at = todo.notified_at or todo.closed_at
    _stop_timers(todo)
    events.record(
        session,
        todo,
        "rejected",
        actor_type=ActorType.STAFF,
        actor_id=principal.staff_id,
        payload={"reason": payload.reason.value, "note": payload.note},
    )
    if commit:
        await session.commit()
    return todo


async def merge(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    payload: MergeRequest,
) -> Todo:
    """把待确认的待办并入这位客户已有的待办：原待办记一次催促并追加依据，这条记为重复驳回。"""
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _status(todo, (TodoStatus.PENDING,), "只有待确认的待办可以合并")
    if not await can_confirm(session, principal, todo):
        raise Forbidden("这条待办不是交给你确认的")
    target = await todos.get_visible(session, principal, payload.target_id, lock=True)
    if target.id == todo.id or target.customer_id != todo.customer_id:
        raise Unprocessable("只能并入同一位客户的其他待办")
    if target.status not in (*UNFINISHED, TodoStatus.DONE):
        raise Unprocessable("不能并入已取消或已驳回的待办")
    todos.nudge(
        session,
        target,
        actor_type=ActorType.STAFF,
        detail=todo.detail or todo.title,
        source="merge",
        evidence=list(todo.evidence_message_ids or []),
    )
    todo.status = TodoStatus.REJECTED
    todo.reject_reason = RejectReason.DUPLICATE
    todo.close_note = f"合并到 {target.no}"
    todo.closed_at = utcnow()
    todo.notified_at = todo.notified_at or todo.closed_at
    _stop_timers(todo)
    events.record(
        session,
        todo,
        "merged",
        actor_type=ActorType.STAFF,
        actor_id=principal.staff_id,
        payload={"into": str(target.id), "no": target.no},
    )
    await session.commit()
    await _after(ctx, target)
    return todo


async def batch(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: BatchRequest
) -> BatchResult:
    """批量确认或批量驳回：逐条处理，失败的不影响其他的。"""
    if payload.action == "reject" and payload.reason is None:
        raise Unprocessable("批量驳回需要选择原因")
    done: list[uuid.UUID] = []
    failed: list[BatchFailure] = []
    for todo_id in dict.fromkeys(payload.ids):
        try:
            if payload.action == "confirm":
                await confirm(ctx, session, principal, todo_id, ConfirmRequest(), commit=False)
            else:
                assert payload.reason is not None
                await reject(
                    ctx,
                    session,
                    principal,
                    todo_id,
                    RejectRequest(reason=payload.reason, note=payload.note),
                    commit=False,
                )
            await session.commit()
            done.append(todo_id)
        except AppError as exc:
            await session.rollback()
            failed.append(BatchFailure(id=todo_id, error=exc.message))
    if done and payload.action == "confirm":
        await notify.dispatch(ctx, tenant_id=principal.tenant_id, ids=done)
    return BatchResult(done=done, failed=failed)


# ---- 处理 ----


async def claim(
    ctx: AppContext, session: AsyncSession, principal: Principal, todo_id: uuid.UUID
) -> Todo:
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    if todo.assignee_id is not None:
        raise Conflict("这条待办已经有处理人了")
    if not await can_claim(session, principal, todo):
        raise Forbidden("只能认领所在技能组待认领的待办")
    now = utcnow()
    todo.assignee_id = principal.staff_id
    todo.first_response_at = todo.first_response_at or now
    todo.notified_at = todo.notified_at or now
    events.record(session, todo, "claimed", actor_type=ActorType.STAFF, actor_id=principal.staff_id)
    await session.commit()
    return todo


async def start(
    ctx: AppContext, session: AsyncSession, principal: Principal, todo_id: uuid.UUID
) -> Todo:
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _status(todo, (TodoStatus.OPEN,), "只有待处理的待办可以开始处理")
    if todo.assignee_id is None:
        raise Conflict("请先认领")
    _require_handle(principal, todo)
    todo.status = TodoStatus.IN_PROGRESS
    todo.first_response_at = todo.first_response_at or utcnow()
    events.record(session, todo, "started", actor_type=ActorType.STAFF, actor_id=principal.staff_id)
    await session.commit()
    return todo


async def wait(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    note: str | None,
) -> Todo:
    """等待客户补充信息：暂停计时。"""
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _status(todo, TIMED, "只有待处理或处理中的待办可以改为等待客户")
    _require_handle(principal, todo)
    now = utcnow()
    todo.status = TodoStatus.WAITING
    todo.paused_at = now
    todo.first_response_at = todo.first_response_at or now
    events.record(
        session,
        todo,
        "waiting",
        actor_type=ActorType.STAFF,
        actor_id=principal.staff_id,
        payload={"note": note},
    )
    await session.commit()
    return todo


async def resume(
    ctx: AppContext, session: AsyncSession, principal: Principal, todo_id: uuid.UUID
) -> Todo:
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _status(todo, (TodoStatus.WAITING,), "只有等待客户的待办可以恢复")
    _require_handle(principal, todo)
    type_ = await _type(session, todo.type_id)
    todo.status = TodoStatus.IN_PROGRESS
    sla.resume_clock(todo, type_, await sla.business_hours(session), utcnow())
    events.record(session, todo, "resumed", actor_type=ActorType.STAFF, actor_id=principal.staff_id)
    await session.commit()
    return todo


def render_done(template: str, result: str) -> str:
    """完成通知：类型的模板里 {result} 换成处理结果；没有模板时直接用处理结果。"""
    return template.replace("{result}", result) if template.strip() else result


async def complete(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    payload: DoneRequest,
) -> tuple[Todo, NotifyResult | None]:
    """完成：填写处理结果，可以同时通知客户。"""
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _status(todo, ACTIVE, "只有未完成的待办可以完成")
    if todo.assignee_id is None and await can_claim(session, principal, todo):
        todo.assignee_id = principal.staff_id
    _require_handle(principal, todo)
    now = utcnow()
    todo.status = TodoStatus.DONE
    todo.result = payload.result.strip()
    todo.closed_at = now
    todo.paused_at = None
    todo.first_response_at = todo.first_response_at or now
    _stop_timers(todo)
    events.record(
        session,
        todo,
        "done",
        actor_type=ActorType.STAFF,
        actor_id=principal.staff_id,
        payload={"result": todo.result},
    )
    result = None
    rooms: set[uuid.UUID] = set()
    if payload.notify_customer:
        type_ = await _type(session, todo.type_id)
        text = (payload.notice or "").strip() or render_done(type_.done_template, todo.result)
        notice = await notify.notify_customer(
            session, todo, text, actor_id=principal.staff_id, now=now
        )
        result = NotifyResult(status=notice.status, channel=notice.channel, reason=notice.reason)
        if notice.status == "sent" and notice.room_id is not None:
            rooms.add(notice.room_id)
    await session.commit()
    await _after(ctx, todo, rooms)
    return todo, result


async def cancel(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    reason: str,
) -> Todo:
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _status(todo, ACTIVE, "只有未完成的待办可以取消")
    _require_handle(principal, todo)
    todo.status = TodoStatus.CANCELLED
    todo.close_note = reason.strip()
    todo.closed_at = utcnow()
    todo.paused_at = None
    _stop_timers(todo)
    events.record(
        session,
        todo,
        "cancelled",
        actor_type=ActorType.STAFF,
        actor_id=principal.staff_id,
        payload={"reason": todo.close_note},
    )
    await session.commit()
    return todo


async def reopen(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    reason: str,
) -> Todo:
    """完成后一段时间内（默认 7 天）客户再次提出同一件事：重新打开原待办，重新计时。"""
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _status(todo, (TodoStatus.DONE,), "只有已完成的待办可以重新打开")
    if not principal.has(Permission.TODO_ASSIGN) and not principal.has(Permission.TODO_HANDLE):
        raise Forbidden("没有处理待办的权限")
    settings = await todo_settings.load(session, todo.tenant_id)
    now = utcnow()
    if todo.closed_at is None or todo.closed_at < now - timedelta(days=settings.reopen_days):
        raise Conflict(f"完成超过 {settings.reopen_days} 天的待办不能重新打开，请新建")
    type_ = await _type(session, todo.type_id)
    previous = todo.result
    todo.status = TodoStatus.OPEN
    todo.closed_at = None
    todo.expected_at = None
    sla.start_clock(todo, type_, await sla.business_hours(session), now)
    _notify_later(todo, NotifyReason.REOPENED, principal.staff_id)
    events.record(
        session,
        todo,
        "reopened",
        actor_type=ActorType.STAFF,
        actor_id=principal.staff_id,
        payload={"reason": reason.strip(), "previous_result": previous},
    )
    await session.commit()
    await _after(ctx, todo)
    return todo


async def assign_to(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    payload: AssignRequest,
    *,
    ip: str | None,
) -> Todo:
    """分派、改派或转交（处理人可以把自己的待办转交他人，附说明）。"""
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _status(todo, UNFINISHED, "已结束的待办不能分派")
    me = principal.staff_id
    mine = principal.has(Permission.TODO_HANDLE) and todo.assignee_id == me
    if not principal.has(Permission.TODO_ASSIGN) and not mine:
        raise Forbidden("只能转交自己的待办")
    if payload.assignee_id is None and payload.skill_group_id is None:
        raise Unprocessable("请选择处理人或技能组")
    if payload.assignee_id is not None:
        await _active_staff(session, payload.assignee_id)
    if payload.skill_group_id is not None:
        await _group_exists(session, payload.skill_group_id)
    previous = todo.assignee_id
    todo.assignee_id, todo.skill_group_id = payload.assignee_id, payload.skill_group_id
    todo.assigned_by = me
    reason = NotifyReason.PENDING if todo.status == TodoStatus.PENDING else NotifyReason.ASSIGNED
    _notify_later(todo, reason, me)
    detail = {
        "from": str(previous) if previous else None,
        "to": str(todo.assignee_id) if todo.assignee_id else None,
        "skill_group_id": str(todo.skill_group_id) if todo.skill_group_id else None,
        "note": payload.note,
    }
    events.record(
        session, todo, "assigned", actor_type=ActorType.STAFF, actor_id=me, payload=detail
    )
    record_audit(
        session,
        action="todo.assign",
        actor_type="staff",
        actor_id=me,
        tenant_id=principal.tenant_id,
        resource_type="todo",
        resource_id=str(todo.id),
        detail={"no": todo.no, **detail},
        ip=ip,
    )
    await session.commit()
    await _after(ctx, todo)
    return todo


async def reschedule(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    payload: RescheduleRequest,
) -> Todo:
    """改期：附原因，动态里保留原截止时间。"""
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _status(todo, ACTIVE, "只有未完成的待办可以改期")
    _require_handle(principal, todo)
    now = utcnow()
    if payload.due_at <= now:
        raise Unprocessable("新的截止时间必须晚于现在")
    previous = todo.due_at
    todo.due_at = payload.due_at
    type_ = await _type(session, todo.type_id)
    sla.schedule_reminders(todo, type_, await sla.business_hours(session), now)
    events.record(
        session,
        todo,
        "rescheduled",
        actor_type=ActorType.STAFF,
        actor_id=principal.staff_id,
        payload={
            "from": previous.isoformat() if previous else None,
            "to": payload.due_at.isoformat(),
            "reason": payload.reason,
        },
    )
    await session.commit()
    return todo


async def comment(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    payload: CommentRequest,
) -> Todo:
    """评论，可以 @ 同事（被 @ 的同事收到提醒）。"""
    todo = await todos.get_visible(session, principal, todo_id)
    if not (principal.has(Permission.TODO_HANDLE) or principal.has(Permission.TODO_ASSIGN)):
        raise Forbidden("没有处理待办的权限")
    mentions = [m for m in dict.fromkeys(payload.mentions) if m != principal.staff_id]
    if mentions:
        found = set(
            (
                await session.scalars(
                    select(Staff.id).where(
                        Staff.id.in_(mentions), Staff.status == StaffStatus.ACTIVE
                    )
                )
            ).all()
        )
        mentions = [m for m in mentions if m in found]
    events.record(
        session,
        todo,
        "commented",
        actor_type=ActorType.STAFF,
        actor_id=principal.staff_id,
        payload={"text": payload.text, "mentions": [str(m) for m in mentions]},
    )
    author = await session.scalar(select(Staff.display_name).where(Staff.id == principal.staff_id))
    title = f"{author or '同事'} 在待办「{todo.title}」里提到了你"
    if mentions:
        notifications.add(
            session,
            principal.tenant_id,
            mentions,
            kind="todo_mention",
            title=title,
            body=payload.text[:200],
            link=notify.link(todo),
        )
    await session.commit()
    if mentions:
        await notify_staff(
            ctx,
            principal.tenant_id,
            mentions,
            title=title[:128],
            description=payload.text[:200],
            path=notify.link(todo),
        )
    return todo


async def update(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    payload: TodoUpdate,
) -> Todo:
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _status(todo, UNFINISHED, "已结束的待办不能修改")
    if todo.status == TodoStatus.PENDING:
        if not await can_confirm(session, principal, todo):
            raise Forbidden("这条待办不是交给你确认的")
    else:
        _require_handle(principal, todo)
    type_ = await _type(session, todo.type_id)
    changed: list[str] = []
    if payload.title is not None and payload.title.strip() != todo.title:
        todo.title = payload.title.strip()
        changed.append("title")
    if payload.detail is not None and payload.detail.strip() != todo.detail:
        todo.detail = payload.detail.strip()
        changed.append("detail")
    if payload.fields is not None:
        values = await _plain_fields(ctx, todo)
        values.update(payload.fields)
        cleaned = _clean_or_raise(type_, {k: v for k, v in values.items() if v})
        todo.fields = await todo_fields.seal(ctx.keys, todo.tenant_id, type_, cleaned)
        changed.append("fields")
    if payload.priority is not None and payload.priority != todo.priority:
        todo.priority = payload.priority
        changed.append("priority")
    if payload.progress_note is not None and payload.progress_note != (todo.progress_note or ""):
        todo.progress_note = payload.progress_note.strip() or None
        changed.append("progress_note")
    if payload.expected_at is not None and payload.expected_at != todo.expected_at:
        todo.expected_at = payload.expected_at
        changed.append("expected_at")
        if todo.status in ACTIVE:
            todo.due_at = payload.expected_at
            sla.schedule_reminders(todo, type_, await sla.business_hours(session), utcnow())
    if changed:
        events.record(
            session,
            todo,
            "updated",
            actor_type=ActorType.STAFF,
            actor_id=principal.staff_id,
            payload={"changed": changed},
        )
        await session.commit()
    return todo


async def notify_customer(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    text: str,
) -> NotifyResult:
    todo = await todos.get_visible(session, principal, todo_id, lock=True)
    _require_handle(principal, todo)
    if todo.customer_id is None:
        raise Unprocessable("这条待办没有关联客户")
    notice = await notify.notify_customer(session, todo, text.strip(), actor_id=principal.staff_id)
    await session.commit()
    if notice.status == "sent" and notice.room_id is not None:
        await outbox.flush_rooms(ctx, todo.tenant_id, [notice.room_id])
    return NotifyResult(status=notice.status, channel=notice.channel, reason=notice.reason)


async def reveal(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    todo_id: uuid.UUID,
    *,
    ip: str | None,
) -> list[FieldValue]:
    """查看敏感字段的完整内容：需要 customer:view_sensitive，记审计日志。"""
    if not principal.has(Permission.CUSTOMER_VIEW_SENSITIVE):
        raise Forbidden("没有查看敏感信息的权限")
    todo = await todos.get_visible(session, principal, todo_id)
    type_ = await session.get(TodoType, todo.type_id)
    plain = await _plain_fields(ctx, todo)
    sensitive = {s["key"] for s in (type_.fields if type_ else []) or [] if s.get("sensitive")}
    record_audit(
        session,
        action="todo.view_sensitive",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="todo",
        resource_id=str(todo.id),
        detail={"no": todo.no, "fields": sorted(k for k in plain if k in sensitive)},
        ip=ip,
    )
    await session.commit()
    return [
        FieldValue(key=key, label=label, value=plain.get(key, value), sensitive=key in sensitive)
        for key, label, value in todo_fields.labelled(type_, todo.fields or {})
    ]
