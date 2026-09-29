"""会话引擎：消息归入会话、排队与分配、结束、超时与断线处理（设计文档 §8.2、§11.3）。

- 状态变化都写入 session_events。
- 需要在 OpenIM 里执行的操作（拉坐席进群、踢出、给客户的提示、给坐席的信令）写入 IM 发件箱，
  与状态变化在同一个事务里提交，提交后立即尝试执行（见 conversation/outbox.py）。
- 分配、超时处理按租户加咨询锁串行执行，避免同一个坐席被并发分配超过并发上限。
"""

import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, union, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.ids import new_id
from app.events.bus import Event
from app.modules.ai import reasons
from app.modules.ai import service as ai_service
from app.modules.ai.schedule import schedule_reply
from app.modules.conversation import outbox
from app.modules.conversation.ingest import message_received
from app.modules.conversation.models import (
    ChatSession,
    CloseReason,
    Message,
    Room,
    SenderType,
    SessionEvent,
    SessionStatus,
    SessionTransfer,
    Ticket,
    TicketSource,
    TicketStatus,
    TransferStatus,
)
from app.modules.customer.models import Customer
from app.modules.routing.assign import (
    HEARTBEAT_TTL,
    AgentSlot,
    PolicyResolver,
    available_agents,
    pick_agent,
)
from app.modules.routing.hours import in_business_hours
from app.modules.routing.models import AgentState, AgentStatus, RoutingMode
from app.modules.wecom import menus
from app.modules.wecom.notify import notify_staff

logger = logging.getLogger(__name__)

_ASSIGN_LOCK = 1002
# 非工作时间的留言：这段时间内客户的后续消息追加到同一条留言。
_OFF_HOURS_TICKET_WINDOW = timedelta(hours=12)
_TICKET_TEXT_LIMIT = 2000
_MAX_REQUEUE_PRIORITY = 10


class Notice:
    """发给客户的系统提示。"""

    QUEUED = "正在为您转接人工客服，您前面还有 {ahead} 位，请稍候。"
    ASSIGNED = "客服 {name} 为您服务。"
    OFF_HOURS = "您好，现在是非工作时间。您的留言已记录，我们会在工作时间尽快联系您。"
    QUEUE_TIMEOUT = "当前咨询较多，您的问题已登记为留言，我们会尽快联系您。"
    IDLE_CLOSED = "由于长时间没有新消息，本次会话已结束。如需帮助，请随时留言。"
    CLOSED = "本次会话已结束，感谢您的咨询。"


class Signal:
    """发给坐席工作台的在线信令类型。"""

    ASSIGNED = "session.assigned"
    CLOSED = "session.closed"
    REVOKED = "session.revoked"


class ActorType:
    SYSTEM = "system"
    STAFF = "staff"
    VISITOR = "visitor"
    AI = "ai"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _AfterCommit:
    """事务提交后要做的事。"""

    rooms: set[uuid.UUID] = field(default_factory=set)
    assign: bool = False
    newly_queued: set[uuid.UUID] = field(default_factory=set)


async def _run_after_commit(ctx: AppContext, tenant_id: uuid.UUID, todo: _AfterCommit) -> None:
    await outbox.flush_rooms(ctx, tenant_id, todo.rooms)
    if todo.assign:
        await assign_queued(ctx, tenant_id, newly_queued=todo.newly_queued)


def record_event(
    session: AsyncSession,
    chat: ChatSession,
    type_: str,
    *,
    actor_type: str = ActorType.SYSTEM,
    actor_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        SessionEvent(
            tenant_id=chat.tenant_id,
            session_id=chat.id,
            type=type_,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload or {},
        )
    )


# ---- 消息归入会话 ----


async def on_message_received(ctx: AppContext, event: Event) -> None:
    """message.received 事件：把消息归入会话，客户发起的新对话创建会话并进入路由。

    幂等：消息已归入会话时直接返回。
    """
    message_id = uuid.UUID(event.data["message_id"])
    todo = _AfterCommit()
    now = utcnow()
    async with ctx.db.tenant_session(event.tenant_id) as session:
        message = await session.get(Message, message_id)
        if message is None or message.session_id is not None:
            return
        # 同一个 Room 的会话创建串行执行（事件分区已保证顺序，这里防的是与 API 并发）。
        room = await session.scalar(
            select(Room).where(Room.id == message.room_id).with_for_update()
        )
        if room is None:
            return
        chat = await open_session_of_room(session, room.id)
        if chat is None:
            if message.sender_type == SenderType.CUSTOMER:

                async def ai_blocker() -> str | None:
                    settings = await ai_service.load(session, event.tenant_id)
                    return await ai_service.unavailable_reason(ctx, session, settings, now)

                chat = await start_session(
                    session,
                    room,
                    now,
                    todo,
                    text=_message_text(message),
                    reason="human_first",
                    ai_blocker=ai_blocker,
                )
            else:
                # 系统提示、结束后坐席补发的消息等，归入这个 Room 最近的会话。
                chat = await session.scalar(
                    select(ChatSession)
                    .where(ChatSession.room_id == room.id)
                    .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
                    .limit(1)
                )
        if chat is not None:
            message.session_id = chat.id
            touch_session(chat, message)
            if (
                chat.status == SessionStatus.AI_SERVING
                and message.sender_type == SenderType.CUSTOMER
            ):
                # AI 接待中：稍等片刻（合并客户连续发的消息）后由 AI 回复，见 ai/responder.py。
                delay = timedelta(seconds=ctx.settings.ai_debounce_seconds)
                await schedule_reply(session, chat, now + delay)
        await session.commit()
    await _run_after_commit(ctx, event.tenant_id, todo)


async def open_session_of_room(session: AsyncSession, room_id: uuid.UUID) -> ChatSession | None:
    return await session.scalar(
        select(ChatSession)
        .where(ChatSession.room_id == room_id, ChatSession.status != SessionStatus.CLOSED)
        .with_for_update()
    )


def touch_session(chat: ChatSession, message: Message) -> None:
    """按新消息更新会话的最近消息时间和首次响应时间。"""
    if message.sender_type == SenderType.CUSTOMER:
        chat.last_customer_message_at = _later(chat.last_customer_message_at, message.sent_at)
    elif message.sender_type == SenderType.AGENT:
        chat.last_agent_message_at = _later(chat.last_agent_message_at, message.sent_at)
        if (
            chat.first_response_at is None
            and chat.status == SessionStatus.HUMAN_SERVING
            and message.sender_id == chat.assignee_id
        ):
            chat.first_response_at = message.sent_at


def _later(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate > current else current


async def start_session(
    session: AsyncSession,
    room: Room,
    now: datetime,
    todo: _AfterCommit,
    *,
    text: str,
    reason: str,
    ai_blocker: Callable[[], Awaitable[str | None]] | None = None,
) -> ChatSession:
    """客户发起新的服务过程。

    策略为 AI 优先、且 AI 可以接待（ai_blocker 返回空）时由 AI 接待，不受工作时间限制；
    否则工作时间内进入排队，非工作时间转为留言。text 用于留言内容。
    AI 优先却不能接待时（额度用完、未启用等），原因记为会话的转人工原因，便于管理员排查。
    """
    policy = await PolicyResolver(session).for_channel(room.channel_account_id)
    blocker: str | None = None
    if ai_blocker is not None and policy.mode == RoutingMode.AI_FIRST:
        blocker = await ai_blocker()
        if blocker is None:
            chat = ChatSession(
                id=new_id(),
                tenant_id=room.tenant_id,
                room_id=room.id,
                customer_id=room.customer_id,
                channel_account_id=room.channel_account_id,
                status=SessionStatus.AI_SERVING,
                skill_group_id=policy.default_skill_group_id,
            )
            session.add(chat)
            await session.flush()
            record_event(session, chat, "created", actor_type=ActorType.VISITOR)
            record_event(session, chat, "ai_serving", actor_type=ActorType.AI)
            return chat
    if not in_business_hours(policy.business_hours, now):
        return await _leave_off_hours_message(
            session, room, text, now, todo, policy.default_skill_group_id
        )

    chat = ChatSession(
        id=new_id(),
        tenant_id=room.tenant_id,
        room_id=room.id,
        customer_id=room.customer_id,
        channel_account_id=room.channel_account_id,
        status=SessionStatus.QUEUED,
        skill_group_id=policy.default_skill_group_id,
        queued_at=now,
        handoff_reason=blocker,
    )
    session.add(chat)
    await session.flush()
    record_event(session, chat, "created", actor_type=ActorType.VISITOR)
    payload = {"reason": reason} if blocker is None else {"reason": "ai_unavailable", "ai": blocker}
    record_event(session, chat, "queued", payload=payload)
    todo.assign = True
    todo.newly_queued.add(chat.id)
    return chat


async def _leave_off_hours_message(
    session: AsyncSession,
    room: Room,
    text: str,
    now: datetime,
    todo: _AfterCommit,
    skill_group_id: uuid.UUID | None,
) -> ChatSession:
    """非工作时间：会话直接转为留言。同一段非工作时间里的后续消息追加到同一条留言。"""
    row = (
        await session.execute(
            select(Ticket, ChatSession)
            .join(ChatSession, ChatSession.id == Ticket.session_id)
            .where(
                ChatSession.room_id == room.id,
                Ticket.source == TicketSource.OFF_HOURS,
                Ticket.status == TicketStatus.OPEN,
                Ticket.created_at >= now - _OFF_HOURS_TICKET_WINDOW,
            )
            .order_by(Ticket.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is not None:
        ticket, chat = row
        ticket.content = _clip(f"{ticket.content}\n{text}")
        return chat

    chat = ChatSession(
        id=new_id(),
        tenant_id=room.tenant_id,
        room_id=room.id,
        customer_id=room.customer_id,
        channel_account_id=room.channel_account_id,
        status=SessionStatus.CLOSED,
        skill_group_id=skill_group_id,
        closed_at=now,
        close_reason=CloseReason.LEAVE_MESSAGE,
    )
    session.add(chat)
    await session.flush()
    record_event(session, chat, "created", actor_type=ActorType.VISITOR)
    record_event(
        session, chat, "closed", payload={"reason": CloseReason.LEAVE_MESSAGE, "off_hours": True}
    )
    session.add(
        Ticket(
            tenant_id=room.tenant_id,
            customer_id=room.customer_id,
            session_id=chat.id,
            source=TicketSource.OFF_HOURS,
            content=_clip(text),
            assignee_id=await _owner_of(session, room.customer_id),
            skill_group_id=skill_group_id,
        )
    )
    outbox.enqueue_notice(session, room.id, Notice.OFF_HOURS)
    todo.rooms.add(room.id)
    return chat


async def request_handoff(
    ctx: AppContext, tenant_id: uuid.UUID, room_id: uuid.UUID, *, reason: str, actor_type: str
) -> ChatSession | None:
    """请求人工：AI 接待中的会话转入排队；还没有会话时直接开始排队（非工作时间转为留言）。

    已在排队或人工接待中时不做任何改变。访客点"转人工"、AI 决定转人工（P3）都走这里。
    """
    todo = _AfterCommit()
    now = utcnow()
    async with ctx.db.tenant_session(tenant_id) as session:
        room = await session.scalar(select(Room).where(Room.id == room_id).with_for_update())
        if room is None:
            return None
        chat = await open_session_of_room(session, room.id)
        if chat is None:
            chat = await start_session(
                session, room, now, todo, text="（访客请求人工服务）", reason=reason
            )
        elif chat.status == SessionStatus.AI_SERVING:
            chat.handoff_reason = reason
            record_event(
                session, chat, "handoff", actor_type=actor_type, payload={"reason": reason}
            )
            policy = await PolicyResolver(session).for_channel(room.channel_account_id)
            if in_business_hours(policy.business_hours, now):
                chat.status = SessionStatus.QUEUED
                chat.queued_at = now
                record_event(session, chat, "queued", payload={"reason": "handoff"})
                todo.assign = True
                todo.newly_queued.add(chat.id)
            else:
                await _handoff_off_hours(session, chat, now, todo)
        await session.commit()
    await _run_after_commit(ctx, tenant_id, todo)
    return chat


async def _handoff_off_hours(
    session: AsyncSession, chat: ChatSession, now: datetime, todo: _AfterCommit
) -> None:
    """非工作时间转人工：结束 AI 接待，以交接摘要（或客户消息）生成留言，工作时间跟进。"""
    texts = (
        await session.scalars(
            select(Message.text_plain)
            .where(
                Message.session_id == chat.id,
                Message.sender_type == SenderType.CUSTOMER,
                Message.text_plain.is_not(None),
            )
            .order_by(Message.sent_at.desc())
            .limit(10)
        )
    ).all()
    content = chat.ai_summary or "\n".join(t for t in reversed(texts) if t)
    if chat.handoff_reason:
        content = f"【{reasons.label(chat.handoff_reason)}】{content}"
    session.add(
        Ticket(
            tenant_id=chat.tenant_id,
            customer_id=chat.customer_id,
            session_id=chat.id,
            source=TicketSource.OFF_HOURS,
            content=_clip(content or "（客户没有留下文字内容）"),
            assignee_id=await _owner_of(session, chat.customer_id),
            skill_group_id=chat.skill_group_id,
        )
    )
    await mark_closed(
        session,
        chat,
        now,
        reason=CloseReason.LEAVE_MESSAGE,
        actor_type=ActorType.SYSTEM,
        notice=Notice.OFF_HOURS,
    )
    todo.rooms.add(chat.room_id)


def _message_text(message: Message) -> str:
    return message.text_plain or f"[{message.content_type}]"


def _clip(text: str) -> str:
    return text if len(text) <= _TICKET_TEXT_LIMIT else text[: _TICKET_TEXT_LIMIT - 1] + "…"


async def _owner_of(session: AsyncSession, customer_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(select(Customer.owner_id).where(Customer.id == customer_id))


# ---- 排队与分配 ----


async def assign_queued(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    *,
    newly_queued: Iterable[uuid.UUID] = (),
    now: datetime | None = None,
) -> int:
    """把排队中的会话分配给可接待的坐席，返回分配数量。

    排队顺序：优先级高的在前，同优先级按进入排队的时间。newly_queued 中没能立即分配的会话，
    向客户提示排队位置。
    """
    now = now or utcnow()
    notify = set(newly_queued)
    rooms: set[uuid.UUID] = set()
    assigned = 0
    notices: list[tuple[uuid.UUID, uuid.UUID]] = []  # (坐席, 客户)：企业微信应用消息提醒
    async with ctx.db.tenant_session(tenant_id) as session:
        await lock_tenant_routing(session, tenant_id)
        queued = list(
            (
                await session.scalars(
                    select(ChatSession)
                    .where(ChatSession.status == SessionStatus.QUEUED)
                    .order_by(ChatSession.priority.desc(), ChatSession.queued_at, ChatSession.id)
                    .limit(200)
                    .with_for_update()
                )
            ).all()
        )
        if not queued:
            return 0
        agents = await available_agents(session, now)
        owners: dict[uuid.UUID, uuid.UUID | None] = {}
        previous: dict[uuid.UUID, tuple[uuid.UUID, datetime]] = {}
        if agents:
            rows = await session.execute(
                select(Customer.id, Customer.owner_id).where(
                    Customer.id.in_({c.customer_id for c in queued})
                )
            )
            owners = {customer_id: owner_id for customer_id, owner_id in rows}
            previous = await _previous_assignees(session, {c.room_id for c in queued})
        policies = PolicyResolver(session)
        waiting: list[ChatSession] = []
        for chat in queued:
            picked = None
            if agents:
                policy = await policies.for_channel(chat.channel_account_id)
                last = previous.get(chat.room_id)
                resume = (
                    last[0]
                    if last is not None
                    and policy.resume_window_minutes > 0
                    and last[1] >= now - timedelta(minutes=policy.resume_window_minutes)
                    else None
                )
                picked = pick_agent(
                    agents,
                    owner_id=owners.get(chat.customer_id) if policy.owner_first else None,
                    skill_group_id=chat.skill_group_id or policy.default_skill_group_id,
                    previous_id=resume,
                )
            if picked is None:
                waiting.append(chat)
                continue
            agent, via = picked
            await assign_to(session, chat, agent, now, via=via)
            rooms.add(chat.room_id)
            assigned += 1
            notices.append((agent.staff_id, chat.customer_id))
        for ahead, chat in enumerate(waiting):
            if chat.id in notify:
                outbox.enqueue_notice(session, chat.room_id, Notice.QUEUED.format(ahead=ahead))
                rooms.add(chat.room_id)
        names = (
            dict(
                (
                    await session.execute(
                        select(Customer.id, Customer.display_name).where(
                            Customer.id.in_({c for _, c in notices})
                        )
                    )
                ).all()
            )
            if notices and ctx.wecom is not None
            else {}
        )
        await session.commit()
    await outbox.flush_rooms(ctx, tenant_id, rooms)
    for staff_id, customer_id in notices:
        await notify_staff(
            ctx,
            tenant_id,
            [staff_id],
            title="新会话分配",
            description=f"客户「{names.get(customer_id, '')}」的会话已分配给您，请及时回复。",
            path="/workbench",
        )
    return assigned


async def _previous_assignees(
    session: AsyncSession, room_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[uuid.UUID, datetime]]:
    """各 Room 最近一次由坐席接待并结束的会话：(坐席, 结束时间)。用于会话续接。"""
    rows = await session.execute(
        select(ChatSession.room_id, ChatSession.assignee_id, ChatSession.closed_at)
        .where(
            ChatSession.room_id.in_(room_ids),
            ChatSession.status == SessionStatus.CLOSED,
            ChatSession.assignee_id.is_not(None),
            ChatSession.close_reason.in_((CloseReason.AGENT, CloseReason.IDLE_TIMEOUT)),
        )
        .order_by(ChatSession.room_id, ChatSession.closed_at.desc())
        .ext(distinct_on(ChatSession.room_id))
    )
    return {
        room_id: (assignee_id, closed_at)
        for room_id, assignee_id, closed_at in rows
        if assignee_id is not None and closed_at is not None
    }


async def lock_tenant_routing(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """租户级的分配锁（事务结束时释放）。"""
    await session.execute(
        select(func.pg_advisory_xact_lock(_ASSIGN_LOCK, func.hashtext(str(tenant_id))))
    )


async def assign_to(
    session: AsyncSession,
    chat: ChatSession,
    agent: AgentSlot,
    now: datetime,
    *,
    via: str,
    actor_type: str = ActorType.SYSTEM,
    actor_id: uuid.UUID | None = None,
) -> None:
    chat.status = SessionStatus.HUMAN_SERVING
    chat.assignee_id = agent.staff_id
    chat.assigned_at = now
    agent.load += 1
    agent.last_assigned_at = now
    state = await session.get(AgentState, agent.staff_id)
    if state is not None:
        state.last_assigned_at = now
    record_event(
        session,
        chat,
        "assigned",
        actor_type=actor_type,
        actor_id=actor_id,
        payload={"staff_id": str(agent.staff_id), "via": via},
    )
    outbox.enqueue_invite(session, chat.room_id, agent.staff_id, agent.display_name)
    outbox.enqueue_notice(session, chat.room_id, Notice.ASSIGNED.format(name=agent.display_name))
    outbox.enqueue_signal(
        session,
        chat.room_id,
        agent.staff_id,
        {"type": Signal.ASSIGNED, "session_id": str(chat.id), "room_id": str(chat.room_id)},
    )


# ---- 结束 ----


async def close_session(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    reason: str,
    actor_type: str,
    actor_id: uuid.UUID | None = None,
    notice: str | None = Notice.CLOSED,
) -> ChatSession | None:
    """结束会话（幂等）。坐席被移出服务群，空出的名额立即用于分配排队中的会话。"""
    now = utcnow()
    async with ctx.db.tenant_session(tenant_id) as session:
        chat = await session.scalar(
            select(ChatSession).where(ChatSession.id == session_id).with_for_update()
        )
        if chat is None or chat.status == SessionStatus.CLOSED:
            return chat
        await mark_closed(
            session,
            chat,
            now,
            reason=reason,
            actor_type=actor_type,
            actor_id=actor_id,
            notice=notice,
        )
        await session.commit()
    await _run_after_commit(ctx, tenant_id, _AfterCommit(rooms={chat.room_id}, assign=True))
    return chat


async def mark_closed(
    session: AsyncSession,
    chat: ChatSession,
    now: datetime,
    *,
    reason: str,
    actor_type: str,
    actor_id: uuid.UUID | None = None,
    notice: str | None,
) -> None:
    chat.status = SessionStatus.CLOSED
    chat.closed_at = now
    chat.close_reason = reason
    # 会话结束时，待确认的转接一并撤销。
    await session.execute(
        update(SessionTransfer)
        .where(
            SessionTransfer.session_id == chat.id,
            SessionTransfer.status == TransferStatus.PENDING,
        )
        .values(status=TransferStatus.CANCELLED, decided_at=now)
    )
    record_event(
        session,
        chat,
        "closed",
        actor_type=actor_type,
        actor_id=actor_id,
        payload={"reason": reason},
    )
    if notice:
        menu = None
        if chat.assigned_at is not None and chat.csat is None:
            # 微信客服：人工接待过的会话，结束提示带满意度评价按钮（与提示合并成一条）。
            kf = await menus.kf_settings(session, chat.channel_account_id)
            if kf is not None and kf.kf_csat_menu:
                notice = f"{notice}{menus.CSAT_PROMPT}"
                menu = menus.csat_menu()
        outbox.enqueue_notice(session, chat.room_id, notice, menu=menu)
    if chat.assignee_id is not None:
        outbox.enqueue_signal(
            session,
            chat.room_id,
            chat.assignee_id,
            {"type": Signal.CLOSED, "session_id": str(chat.id), "room_id": str(chat.room_id)},
        )
        outbox.enqueue_kick(session, chat.room_id, chat.assignee_id)


async def requeue_unanswered(
    session: AsyncSession, staff_id: uuid.UUID, now: datetime, *, reason: str
) -> set[uuid.UUID]:
    """把分配给坐席、坐席还没回复过的会话退回队列，返回涉及的 Room。"""
    chats = (
        await session.scalars(
            select(ChatSession)
            .where(
                ChatSession.assignee_id == staff_id,
                ChatSession.status == SessionStatus.HUMAN_SERVING,
                ChatSession.first_response_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    for chat in chats:
        # 重新计算排队超时，并排在同优先级的新会话前面。
        chat.status = SessionStatus.QUEUED
        chat.assignee_id = None
        chat.assigned_at = None
        chat.queued_at = now
        chat.priority = min(chat.priority + 1, _MAX_REQUEUE_PRIORITY)
        record_event(
            session, chat, "requeued", payload={"staff_id": str(staff_id), "reason": reason}
        )
        outbox.enqueue_signal(
            session,
            chat.room_id,
            staff_id,
            {"type": Signal.REVOKED, "session_id": str(chat.id), "room_id": str(chat.room_id)},
        )
        outbox.enqueue_kick(session, chat.room_id, staff_id)
    return {chat.room_id for chat in chats}


# ---- 定时处理 ----


@dataclass
class TimerReport:
    tenants: int = 0
    agents_offline: int = 0
    requeued_rooms: int = 0
    queue_timeouts: int = 0
    idle_closed: int = 0
    assigned: int = 0
    errors: int = 0


async def run_session_timers(ctx: AppContext, *, now: datetime | None = None) -> TimerReport:
    """断线坐席下线并退回未回复的会话；排队超时转留言；人工接待空闲超时结束；再分配一轮。"""
    now = now or utcnow()
    report = TimerReport()
    async with ctx.db.platform_sessionmaker() as session:
        tenant_ids = (
            await session.scalars(
                union(
                    select(ChatSession.tenant_id).where(ChatSession.status != SessionStatus.CLOSED),
                    select(AgentState.tenant_id).where(AgentState.status != AgentStatus.OFFLINE),
                )
            )
        ).all()
    for tenant_id in tenant_ids:
        report.tenants += 1
        try:
            await _run_tenant_timers(ctx, tenant_id, now, report)
        except Exception:
            report.errors += 1
            logger.exception("session timers failed for tenant %s", tenant_id)
    return report


async def _run_tenant_timers(
    ctx: AppContext, tenant_id: uuid.UUID, now: datetime, report: TimerReport
) -> None:
    rooms: set[uuid.UUID] = set()
    async with ctx.db.tenant_session(tenant_id) as session:
        await lock_tenant_routing(session, tenant_id)

        stale = (
            await session.scalars(
                select(AgentState).where(
                    AgentState.status != AgentStatus.OFFLINE,
                    func.coalesce(AgentState.last_seen_at, AgentState.status_changed_at)
                    < now - HEARTBEAT_TTL,
                )
            )
        ).all()
        for state in stale:
            state.status = AgentStatus.OFFLINE
            state.status_changed_at = now
            requeued = await requeue_unanswered(
                session, state.staff_id, now, reason="agent_offline"
            )
            rooms |= requeued
            report.agents_offline += 1
            report.requeued_rooms += len(requeued)

        policies = PolicyResolver(session)
        open_chats = (
            await session.scalars(
                select(ChatSession)
                .where(
                    ChatSession.status.in_(
                        (
                            SessionStatus.QUEUED,
                            SessionStatus.HUMAN_SERVING,
                            SessionStatus.AI_SERVING,
                        )
                    )
                )
                .with_for_update()
            )
        ).all()
        for chat in open_chats:
            policy = await policies.for_channel(chat.channel_account_id)
            if chat.status == SessionStatus.QUEUED:
                waited_since = chat.queued_at or chat.created_at
                if waited_since <= now - timedelta(seconds=policy.max_wait_seconds):
                    await _queue_timeout(session, chat, now)
                    rooms.add(chat.room_id)
                    report.queue_timeouts += 1
            elif chat.status == SessionStatus.AI_SERVING:
                last = chat.last_customer_message_at or chat.created_at
                if last <= now - timedelta(minutes=policy.idle_close_minutes):
                    await mark_closed(
                        session,
                        chat,
                        now,
                        reason=CloseReason.AI_RESOLVED,
                        actor_type=ActorType.AI,
                        notice=Notice.CLOSED,
                    )
                    rooms.add(chat.room_id)
                    report.idle_closed += 1
            else:
                last = max(
                    t
                    for t in (
                        chat.assigned_at,
                        chat.last_customer_message_at,
                        chat.last_agent_message_at,
                        chat.created_at,
                    )
                    if t is not None
                )
                if last <= now - timedelta(minutes=policy.idle_close_minutes):
                    await mark_closed(
                        session,
                        chat,
                        now,
                        reason=CloseReason.IDLE_TIMEOUT,
                        actor_type=ActorType.SYSTEM,
                        notice=Notice.IDLE_CLOSED,
                    )
                    rooms.add(chat.room_id)
                    report.idle_closed += 1
        await session.commit()
    await outbox.flush_rooms(ctx, tenant_id, rooms)
    report.assigned += await assign_queued(ctx, tenant_id, now=now)


async def _queue_timeout(session: AsyncSession, chat: ChatSession, now: datetime) -> None:
    texts = (
        await session.scalars(
            select(Message.text_plain)
            .where(
                Message.session_id == chat.id,
                Message.sender_type == SenderType.CUSTOMER,
                Message.text_plain.is_not(None),
            )
            .order_by(Message.sent_at.desc())
            .limit(10)
        )
    ).all()
    session.add(
        Ticket(
            tenant_id=chat.tenant_id,
            customer_id=chat.customer_id,
            session_id=chat.id,
            source=TicketSource.QUEUE_TIMEOUT,
            content=_clip("\n".join(t for t in reversed(texts) if t) or "（客户没有留下文字内容）"),
            assignee_id=await _owner_of(session, chat.customer_id),
            skill_group_id=chat.skill_group_id,
        )
    )
    await mark_closed(
        session,
        chat,
        now,
        reason=CloseReason.LEAVE_MESSAGE,
        actor_type=ActorType.SYSTEM,
        notice=Notice.QUEUE_TIMEOUT,
    )


async def republish_orphans(ctx: AppContext, *, now: datetime | None = None) -> int:
    """重新发布没有归入会话的消息的 message.received 事件（事件发布失败或丢失时兜底）。

    只看最近一天、30 秒以前入库的消息；非客户消息只在 Room 已经有会话时才需要归入。
    """
    now = now or utcnow()
    has_session = select(ChatSession.id).where(ChatSession.room_id == Message.room_id).exists()
    async with ctx.db.platform_sessionmaker() as session:
        rows = (
            await session.execute(
                select(Message.tenant_id, Message.room_id, Message.id)
                .where(
                    Message.session_id.is_(None),
                    Message.created_at >= now - timedelta(days=1),
                    Message.created_at <= now - timedelta(seconds=30),
                    (Message.sender_type == SenderType.CUSTOMER) | has_session,
                )
                .order_by(Message.created_at)
                .limit(1000)
            )
        ).all()
    for tenant_id, room_id, message_id in rows:
        await ctx.bus.publish(message_received(tenant_id, room_id, message_id))
    return len(rows)
