"""会话转接（设计文档 §14.2）。

- 转给坐席：会话进入"转接中"，目标坐席在 60 秒内接受后生效；拒绝、超时或撤回时会话留在原坐席。
- 转给技能组：会话重新排队，按技能组分配，不需要对方确认。
- 强制转接（session:transfer_any）：直接转给目标坐席，不需要对方确认。
- 生效时把新坐席拉进服务群、把原坐席移出，客户收到"已为您转接至 XX"；可以同时转移客户归属。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.ids import new_id
from app.core.permissions import Permission
from app.modules.conversation import outbox
from app.modules.conversation.models import (
    ChatSession,
    SessionStatus,
    SessionTransfer,
    TransferStatus,
)
from app.modules.customer.models import Customer, OwnerChangeReason
from app.modules.customer.ownership import change_owner
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.routing.assign import LOAD_STATUSES
from app.modules.routing.models import AgentState, AgentStatus, SkillGroup
from app.modules.sessions.engine import (
    ActorType,
    Signal,
    assign_queued,
    leave_as_watcher,
    lock_tenant_routing,
    record_event,
    utcnow,
)
from app.modules.sessions.schemas import (
    TransferAgent,
    TransferGroup,
    TransferOut,
    TransferRequest,
    TransferTargets,
)
from app.modules.sessions.service import visible_session
from app.modules.wecom.notify import notify_staff

TRANSFER_TIMEOUT = timedelta(seconds=60)
TRANSFER_NOT_FOUND = "转接不存在或已处理"


class TransferSignal:
    REQUESTED = "transfer.requested"  # 发给目标坐席：请求接受
    ACCEPTED = "transfer.accepted"  # 发给发起人
    REJECTED = "transfer.rejected"
    EXPIRED = "transfer.expired"
    CANCELLED = "transfer.cancelled"  # 发给目标坐席：请求已撤回


class TransferNotice:
    TO_AGENT = "已为您转接至客服 {name}，请稍候。"
    TO_GROUP = "正在为您转接其他客服，请稍候。"


@dataclass
class _Target:
    staff: Staff | None
    group: SkillGroup | None


def _out(t: SessionTransfer) -> TransferOut:
    return TransferOut(
        id=t.id,
        session_id=t.session_id,
        from_staff_id=t.from_staff_id,
        to_staff_id=t.to_staff_id,
        to_group_id=t.to_group_id,
        note=t.note,
        forced=t.forced,
        transfer_ownership=t.transfer_ownership,
        status=TransferStatus(t.status),
        expires_at=t.expires_at,
        decided_at=t.decided_at,
        created_at=t.created_at,
    )


async def _target(session: AsyncSession, payload: TransferRequest) -> _Target:
    if (payload.to_staff_id is None) == (payload.to_group_id is None):
        raise Unprocessable("请指定一个目标坐席或技能组")
    if payload.to_staff_id is not None:
        staff = await session.get(Staff, payload.to_staff_id)
        if staff is None or staff.status != StaffStatus.ACTIVE:
            raise Unprocessable("目标坐席不存在或已停用")
        return _Target(staff, None)
    group = await session.get(SkillGroup, payload.to_group_id)
    if group is None:
        raise Unprocessable("技能组不存在")
    return _Target(None, group)


async def request_transfer(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    session_id: uuid.UUID,
    payload: TransferRequest,
) -> TransferOut:
    chat, *_ = await visible_session(session, principal, session_id)
    is_mine = chat.assignee_id == principal.staff_id
    if not (
        (is_mine and principal.has(Permission.SESSION_TRANSFER))
        or principal.has(Permission.SESSION_TRANSFER_ANY)
    ):
        raise Forbidden("只能转接自己接待中的会话")
    if payload.force and not principal.has(Permission.SESSION_TRANSFER_ANY):
        raise Forbidden("没有强制转接的权限")
    if payload.transfer_ownership and not principal.has(Permission.CUSTOMER_ASSIGN):
        raise Forbidden("没有变更客户归属的权限")
    target = await _target(session, payload)
    if target.staff is not None and target.staff.id == chat.assignee_id:
        raise Unprocessable("会话已经由该坐席接待")
    if payload.transfer_ownership and target.staff is None:
        raise Unprocessable("转给技能组时不能同时转移归属")

    now = utcnow()
    await lock_tenant_routing(session, principal.tenant_id)
    chat = await _lock(session, chat.id)
    if chat.status == SessionStatus.TRANSFERRING:
        raise Conflict("这个会话已有待确认的转接")
    if chat.status != SessionStatus.HUMAN_SERVING:
        raise Conflict("只有人工接待中的会话可以转接")

    transfer = SessionTransfer(
        id=new_id(),
        tenant_id=chat.tenant_id,
        session_id=chat.id,
        from_staff_id=chat.assignee_id,
        to_staff_id=target.staff.id if target.staff else None,
        to_group_id=target.group.id if target.group else None,
        note=payload.note,
        forced=payload.force,
        transfer_ownership=payload.transfer_ownership,
        created_by=principal.staff_id,
    )
    session.add(transfer)
    assign = False
    if target.group is not None:
        _to_group(session, chat, transfer, target.group, now, principal)
        assign = True
    elif payload.force:
        assert target.staff is not None
        await _hand_over(session, chat, transfer, target.staff, now, actor_id=principal.staff_id)
    else:
        assert target.staff is not None
        state = await session.get(AgentState, target.staff.id)
        if state is None or state.status != AgentStatus.ONLINE:
            raise Conflict("目标坐席不在线")
        transfer.expires_at = now + TRANSFER_TIMEOUT
        chat.status = SessionStatus.TRANSFERRING
        record_event(
            session,
            chat,
            "transfer_requested",
            actor_type=ActorType.STAFF,
            actor_id=principal.staff_id,
            payload={"transfer_id": str(transfer.id), "to_staff_id": str(target.staff.id)},
        )
        outbox.enqueue_signal(
            session,
            chat.room_id,
            target.staff.id,
            {
                "type": TransferSignal.REQUESTED,
                "transfer_id": str(transfer.id),
                "session_id": str(chat.id),
                "from": principal.display_name,
                "note": payload.note or "",
                "expires_at": transfer.expires_at.isoformat(),
            },
        )
    await session.commit()
    await session.refresh(transfer)
    await outbox.flush_rooms(ctx, chat.tenant_id, [chat.room_id])
    if assign:
        await assign_queued(ctx, chat.tenant_id)
    if transfer.status == TransferStatus.PENDING and target.staff is not None:
        await notify_staff(
            ctx,
            chat.tenant_id,
            [target.staff.id],
            title="会话转接请求",
            description=f"{principal.display_name} 请您接手一个会话。{payload.note or ''}".strip(),
            path="/workbench",
        )
    return _out(transfer)


async def _lock(session: AsyncSession, session_id: uuid.UUID) -> ChatSession:
    # populate_existing：拿到锁之后读取最新状态，而不是会话里缓存的对象。
    chat = await session.scalar(
        select(ChatSession)
        .where(ChatSession.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if chat is None:
        raise NotFound("会话不存在")
    return chat


def _to_group(
    session: AsyncSession,
    chat: ChatSession,
    transfer: SessionTransfer,
    group: SkillGroup,
    now: datetime,
    principal: Principal,
) -> None:
    """转给技能组：会话重新排队（排在同优先级的新会话前面），原坐席移出服务群。"""
    previous = chat.assignee_id
    chat.status = SessionStatus.QUEUED
    chat.assignee_id = None
    chat.assigned_at = None
    chat.skill_group_id = group.id
    chat.queued_at = now
    chat.priority = min(chat.priority + 1, 10)
    transfer.status = TransferStatus.COMPLETED
    transfer.decided_at = now
    record_event(
        session,
        chat,
        "transferred",
        actor_type=ActorType.STAFF,
        actor_id=principal.staff_id,
        payload={
            "transfer_id": str(transfer.id),
            "from_staff_id": str(previous) if previous else None,
            "to_group_id": str(group.id),
        },
    )
    outbox.enqueue_notice(session, chat.room_id, TransferNotice.TO_GROUP)
    if previous is not None:
        _revoke(session, chat, previous)


def _revoke(session: AsyncSession, chat: ChatSession, staff_id: uuid.UUID) -> None:
    outbox.enqueue_signal(
        session,
        chat.room_id,
        staff_id,
        {"type": Signal.REVOKED, "session_id": str(chat.id), "room_id": str(chat.room_id)},
    )
    outbox.enqueue_kick(session, chat.room_id, staff_id)


async def _hand_over(
    session: AsyncSession,
    chat: ChatSession,
    transfer: SessionTransfer,
    target: Staff,
    now: datetime,
    *,
    actor_id: uuid.UUID | None,
    accepted: bool = False,
) -> None:
    """会话交给目标坐席：拉入服务群、提示客户、通知双方、移出原坐席；按需转移客户归属。"""
    previous = transfer.from_staff_id
    chat.status = SessionStatus.HUMAN_SERVING
    chat.assignee_id = target.id
    chat.assigned_at = now
    await leave_as_watcher(session, chat.id, target.id, now)
    transfer.status = TransferStatus.ACCEPTED if accepted else TransferStatus.COMPLETED
    transfer.decided_at = now
    state = await session.get(AgentState, target.id)
    if state is not None:
        state.last_assigned_at = now
    record_event(
        session,
        chat,
        "transferred",
        actor_type=ActorType.STAFF,
        actor_id=actor_id,
        payload={
            "transfer_id": str(transfer.id),
            "from_staff_id": str(previous) if previous else None,
            "to_staff_id": str(target.id),
            "forced": transfer.forced,
        },
    )
    outbox.enqueue_invite(session, chat.room_id, target.id, target.display_name)
    outbox.enqueue_notice(
        session, chat.room_id, TransferNotice.TO_AGENT.format(name=target.display_name)
    )
    outbox.enqueue_signal(
        session,
        chat.room_id,
        target.id,
        {
            "type": Signal.ASSIGNED,
            "session_id": str(chat.id),
            "room_id": str(chat.room_id),
            "transfer_id": str(transfer.id),
        },
    )
    if previous is not None and previous != target.id:
        if accepted:
            outbox.enqueue_signal(
                session,
                chat.room_id,
                previous,
                {
                    "type": TransferSignal.ACCEPTED,
                    "transfer_id": str(transfer.id),
                    "session_id": str(chat.id),
                },
            )
        _revoke(session, chat, previous)
    customer = await session.get(Customer, chat.customer_id)
    if transfer.transfer_ownership and customer is not None:
        await change_owner(
            session,
            customer,
            target.id,
            actor_id=actor_id,
            reason=OwnerChangeReason.SESSION_TRANSFER,
        )


async def _pending_for(
    session: AsyncSession, transfer_id: uuid.UUID, *, to_staff_id: uuid.UUID | None = None
) -> tuple[SessionTransfer, ChatSession]:
    conditions = [
        SessionTransfer.id == transfer_id,
        SessionTransfer.status == TransferStatus.PENDING,
    ]
    if to_staff_id is not None:
        conditions.append(SessionTransfer.to_staff_id == to_staff_id)
    transfer = await session.scalar(
        select(SessionTransfer).where(and_(*conditions)).with_for_update()
    )
    if transfer is None:
        raise NotFound(TRANSFER_NOT_FOUND)
    chat = await _lock(session, transfer.session_id)
    return transfer, chat


async def accept_transfer(
    ctx: AppContext, session: AsyncSession, principal: Principal, transfer_id: uuid.UUID
) -> TransferOut:
    now = utcnow()
    await lock_tenant_routing(session, principal.tenant_id)
    transfer, chat = await _pending_for(session, transfer_id, to_staff_id=principal.staff_id)
    if transfer.expires_at is not None and transfer.expires_at <= now:
        await _settle(session, chat, transfer, TransferStatus.EXPIRED, now)
        await session.commit()
        await outbox.flush_rooms(ctx, chat.tenant_id, [chat.room_id])
        raise Conflict("转接已超时")
    me = await session.get(Staff, principal.staff_id)
    assert me is not None
    await _hand_over(session, chat, transfer, me, now, actor_id=principal.staff_id, accepted=True)
    await session.commit()
    await session.refresh(transfer)
    await outbox.flush_rooms(ctx, chat.tenant_id, [chat.room_id])
    return _out(transfer)


async def reject_transfer(
    ctx: AppContext, session: AsyncSession, principal: Principal, transfer_id: uuid.UUID
) -> TransferOut:
    transfer, chat = await _pending_for(session, transfer_id, to_staff_id=principal.staff_id)
    await _settle(session, chat, transfer, TransferStatus.REJECTED, utcnow())
    await session.commit()
    await session.refresh(transfer)
    await outbox.flush_rooms(ctx, chat.tenant_id, [chat.room_id])
    return _out(transfer)


async def cancel_transfer(
    ctx: AppContext, session: AsyncSession, principal: Principal, transfer_id: uuid.UUID
) -> TransferOut:
    transfer, chat = await _pending_for(session, transfer_id)
    if transfer.created_by != principal.staff_id and not principal.has(
        Permission.SESSION_TRANSFER_ANY
    ):
        raise NotFound(TRANSFER_NOT_FOUND)
    await _settle(session, chat, transfer, TransferStatus.CANCELLED, utcnow())
    await session.commit()
    await session.refresh(transfer)
    await outbox.flush_rooms(ctx, chat.tenant_id, [chat.room_id])
    return _out(transfer)


async def _settle(
    session: AsyncSession,
    chat: ChatSession,
    transfer: SessionTransfer,
    status: TransferStatus,
    now: datetime,
) -> None:
    """转接没有生效（拒绝、超时、撤回）：会话留在原坐席。"""
    transfer.status = status
    transfer.decided_at = now
    if chat.status == SessionStatus.TRANSFERRING:
        chat.status = SessionStatus.HUMAN_SERVING
    record_event(
        session,
        chat,
        f"transfer_{status}",
        payload={"transfer_id": str(transfer.id)},
    )
    signal = {
        TransferStatus.REJECTED: TransferSignal.REJECTED,
        TransferStatus.EXPIRED: TransferSignal.EXPIRED,
        TransferStatus.CANCELLED: TransferSignal.CANCELLED,
    }[status]
    notify = transfer.to_staff_id if status == TransferStatus.CANCELLED else transfer.from_staff_id
    if notify is not None:
        outbox.enqueue_signal(
            session,
            chat.room_id,
            notify,
            {"type": signal, "transfer_id": str(transfer.id), "session_id": str(chat.id)},
        )


async def expire_transfers(session: AsyncSession, now: datetime) -> set[uuid.UUID]:
    """超时未接受的转接（调度进程调用，调用方负责提交），返回涉及的 Room。"""
    transfers = (
        await session.scalars(
            select(SessionTransfer)
            .where(
                SessionTransfer.status == TransferStatus.PENDING,
                SessionTransfer.expires_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
    ).all()
    rooms: set[uuid.UUID] = set()
    for transfer in transfers:
        chat = await _lock(session, transfer.session_id)
        await _settle(session, chat, transfer, TransferStatus.EXPIRED, now)
        rooms.add(chat.room_id)
    return rooms


async def list_pending_for_me(session: AsyncSession, principal: Principal) -> list[TransferOut]:
    """发给我、还在等待确认的转接（工作台刷新或重连后恢复提醒）。"""
    rows = await session.execute(
        select(SessionTransfer, Staff.display_name, Customer.display_name)
        .join(ChatSession, ChatSession.id == SessionTransfer.session_id)
        .join(Customer, Customer.id == ChatSession.customer_id)
        .outerjoin(Staff, Staff.id == SessionTransfer.from_staff_id)
        .where(
            SessionTransfer.to_staff_id == principal.staff_id,
            SessionTransfer.status == TransferStatus.PENDING,
            SessionTransfer.expires_at > utcnow(),
        )
        .order_by(SessionTransfer.created_at)
    )
    return [
        _out(t).model_copy(update={"from_staff_name": sender, "customer_display_name": customer})
        for t, sender, customer in rows
    ]


async def run_transfer_timers(ctx: AppContext, *, now: datetime | None = None) -> int:
    """处理超时未接受的转接（调度进程每 5 秒调用），返回处理数量。"""
    now = now or utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        tenant_ids = (
            await session.scalars(
                select(SessionTransfer.tenant_id)
                .where(
                    SessionTransfer.status == TransferStatus.PENDING,
                    SessionTransfer.expires_at <= now,
                )
                .distinct()
            )
        ).all()
    expired = 0
    for tenant_id in tenant_ids:
        async with ctx.db.tenant_session(tenant_id) as session:
            await lock_tenant_routing(session, tenant_id)
            rooms = await expire_transfers(session, now)
            await session.commit()
        expired += len(rooms)
        await outbox.flush_rooms(ctx, tenant_id, rooms)
    return expired


async def transfer_targets(session: AsyncSession, principal: Principal) -> TransferTargets:
    """可以转给的对象：在线的其他坐席（附当前负载）和全部技能组。"""
    load = (
        select(func.count())
        .select_from(ChatSession)
        .where(
            ChatSession.assignee_id == AgentState.staff_id,
            ChatSession.status.in_(LOAD_STATUSES),
        )
        .correlate(AgentState)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(AgentState.staff_id, Staff.display_name, load, AgentState.max_concurrency)
        .join(Staff, and_(Staff.tenant_id == AgentState.tenant_id, Staff.id == AgentState.staff_id))
        .where(
            AgentState.status == AgentStatus.ONLINE,
            AgentState.staff_id != principal.staff_id,
            Staff.status == StaffStatus.ACTIVE,
        )
        .order_by(Staff.display_name)
    )
    groups = await session.execute(select(SkillGroup.id, SkillGroup.name).order_by(SkillGroup.name))
    return TransferTargets(
        agents=[
            TransferAgent(
                staff_id=staff_id,
                display_name=name,
                active_sessions=count,
                max_concurrency=max_concurrency,
            )
            for staff_id, name, count, max_concurrency in rows
        ],
        groups=[TransferGroup(id=group_id, name=name) for group_id, name in groups],
    )
