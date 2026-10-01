"""会话查询（带数据范围）以及坐席发起的会话操作。

会话的可见范围：
- session:read_all：全部会话；
- 否则：分配给自己的会话、自己名下客户的会话；
- 另有 session:read_team 时：所带技能组的会话、组员接待的会话、组员名下客户的会话。
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, true
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.context import AppContext
from app.core.errors import Forbidden, NotFound
from app.core.permissions import Permission
from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.conversation import outbox
from app.modules.conversation.models import (
    ChatSession,
    CloseReason,
    Direction,
    Message,
    Room,
    SenderType,
    SessionEvent,
    SessionStatus,
    SessionWatcher,
)
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.routing.scope import led_groups, team_members
from app.modules.sessions import engine
from app.modules.sessions.schemas import (
    SessionDetail,
    SessionEventOut,
    SessionOut,
    SessionPage,
    WatcherOut,
)

SESSION_NOT_FOUND = "会话不存在"
OPEN = "open"
# 接待中：AI 或人工正在接待（不含排队）。
SERVING = "serving"
SERVING_STATUSES = (
    SessionStatus.AI_SERVING,
    SessionStatus.HUMAN_SERVING,
    SessionStatus.TRANSFERRING,
)

_Assignee = aliased(Staff)


def watching(staff_id: UUID) -> Select[UUID]:
    """员工正在旁听或协助的会话。"""
    return select(SessionWatcher.session_id).where(
        SessionWatcher.staff_id == staff_id, SessionWatcher.left_at.is_(None)
    )


def session_visible_to(principal: Principal) -> ColumnElement[bool]:
    """需要与 Customer 连接查询（见 _sessions）。"""
    if principal.has(Permission.SESSION_READ_ALL):
        return true()
    me = principal.staff_id
    conditions: list[ColumnElement[bool]] = [
        ChatSession.assignee_id == me,
        Customer.owner_id == me,
        ChatSession.id.in_(watching(me)),
    ]
    if principal.has(Permission.SESSION_READ_TEAM):
        team = team_members(me)
        conditions += [
            ChatSession.assignee_id.in_(team),
            ChatSession.skill_group_id.in_(led_groups(me)),
            Customer.owner_id.in_(team),
        ]
    return or_(*conditions)


def _sessions(principal: Principal) -> Select[ChatSession, str, str | None, str]:
    return (
        select(ChatSession, Customer.display_name, _Assignee.display_name, Room.im_group_id)
        .join(
            Customer,
            and_(
                Customer.tenant_id == ChatSession.tenant_id, Customer.id == ChatSession.customer_id
            ),
        )
        .join(Room, and_(Room.tenant_id == ChatSession.tenant_id, Room.id == ChatSession.room_id))
        .outerjoin(
            _Assignee,
            and_(
                _Assignee.tenant_id == ChatSession.tenant_id,
                _Assignee.id == ChatSession.assignee_id,
            ),
        )
        .where(session_visible_to(principal))
    )


async def session_extras(
    session: AsyncSession, principal: Principal, chats: list[ChatSession]
) -> dict[UUID, dict[str, Any]]:
    """渠道类型、接待坐席的未读数、邮件会话最近一封客户邮件的主题。

    未读：坐席看过之后到达平台的客户消息。按到达平台的时间（created_at）而不是发送时间算：
    邮件的时间是邮件服务器收到的时间，收取有间隔，可能早于坐席看过的时间。
    """
    if not chats:
        return {}
    types: dict[UUID, str] = dict(
        (
            await session.execute(
                select(ChannelAccount.id, ChannelAccount.type).where(
                    ChannelAccount.id.in_({c.channel_account_id for c in chats})
                )
            )
        ).all()
    )
    mine = [
        c.id
        for c in chats
        if c.assignee_id == principal.staff_id and c.status != SessionStatus.CLOSED
    ]
    unread: dict[UUID, int] = {}
    if mine:
        rows = await session.execute(
            select(Message.session_id, func.count())
            .join(
                ChatSession,
                and_(
                    ChatSession.tenant_id == Message.tenant_id, ChatSession.id == Message.session_id
                ),
            )
            .where(
                Message.session_id.in_(mine),
                Message.sender_type == SenderType.CUSTOMER,
                or_(ChatSession.read_at.is_(None), Message.created_at > ChatSession.read_at),
            )
            .group_by(Message.session_id)
        )
        unread = {session_id: int(n) for session_id, n in rows if session_id}
    emails = [c.id for c in chats if types.get(c.channel_account_id) == ChannelType.EMAIL]
    subjects: dict[UUID, str] = {}
    if emails:
        found = await session.execute(
            select(Message.session_id, Message.content["subject"].astext)
            .where(
                Message.session_id.in_(emails),
                Message.content_type == "email",
                Message.direction == Direction.IN,
            )
            .order_by(Message.session_id, Message.sent_at.desc())
            .ext(distinct_on(Message.session_id))
        )
        subjects = {session_id: subject for session_id, subject in found if session_id}
    return {
        c.id: {
            "channel_type": types.get(c.channel_account_id),
            "unread": unread.get(c.id, 0),
            "email_subject": subjects.get(c.id),
        }
        for c in chats
    }


async def notify_typing(
    ctx: AppContext, session: AsyncSession, principal: Principal, session_id: UUID
) -> None:
    """接待坐席正在输入：给网页访客的 Widget 发"正在输入"（在线信令，不落库、不记未读）。

    只有接待中的坐席、网页渠道才发；其他情况静默忽略，前端不用区分。
    """
    chat, *_ = await visible_session(session, principal, session_id)
    if chat.assignee_id != principal.staff_id or chat.status != SessionStatus.HUMAN_SERVING:
        return
    channel_type = await session.scalar(
        select(ChannelAccount.type).where(ChannelAccount.id == chat.channel_account_id)
    )
    if channel_type != ChannelType.WEB:
        return
    outbox.enqueue_typing(session, chat.room_id, sender="staff", name=principal.display_name)
    await session.commit()
    # 马上发出去（信令只在线上送达，不等调度进程重试）。
    await outbox.flush_rooms(ctx, principal.tenant_id, [chat.room_id])


async def mark_read(session: AsyncSession, principal: Principal, session_id: UUID) -> None:
    """接待坐席看过了这个会话：之前客户的消息不再算未读。其他人查看不影响。"""
    chat, *_ = await visible_session(session, principal, session_id)
    if chat.assignee_id == principal.staff_id and chat.status != SessionStatus.CLOSED:
        chat.read_at = datetime.now(UTC)
        await session.commit()


def _session_out(
    chat: ChatSession,
    customer_name: str,
    assignee_name: str | None,
    group_id: str,
    role: str | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        **(extras or {}),
        "id": chat.id,
        "room_id": chat.room_id,
        "im_group_id": group_id,
        "customer_id": chat.customer_id,
        "customer_display_name": customer_name,
        "channel_account_id": chat.channel_account_id,
        "status": chat.status,
        "assignee_id": chat.assignee_id,
        "assignee_display_name": assignee_name,
        "skill_group_id": chat.skill_group_id,
        "priority": chat.priority,
        "queued_at": chat.queued_at,
        "assigned_at": chat.assigned_at,
        "first_response_at": chat.first_response_at,
        "closed_at": chat.closed_at,
        "close_reason": chat.close_reason,
        "handoff_reason": chat.handoff_reason,
        "ai_summary": chat.ai_summary,
        "last_customer_message_at": chat.last_customer_message_at,
        "last_agent_message_at": chat.last_agent_message_at,
        "csat": chat.csat,
        "csat_comment": chat.csat_comment,
        "intent": chat.intent,
        "overflowed_at": chat.overflowed_at,
        "my_role": role,
        "created_at": chat.created_at,
    }


async def my_roles(
    session: AsyncSession, principal: Principal, chats: list[ChatSession]
) -> dict[UUID, str]:
    """当前员工在这些会话里的身份：接待、旁听或协助。"""
    roles = {c.id: "assignee" for c in chats if c.assignee_id == principal.staff_id}
    rows = await session.execute(
        select(SessionWatcher.session_id, SessionWatcher.role).where(
            SessionWatcher.staff_id == principal.staff_id,
            SessionWatcher.left_at.is_(None),
            SessionWatcher.session_id.in_([c.id for c in chats]),
        )
    )
    for session_id, role in rows:
        roles.setdefault(session_id, role)
    return roles


async def list_sessions(
    session: AsyncSession,
    principal: Principal,
    *,
    status: str | None,
    mine: bool,
    customer_id: UUID | None,
    limit: int,
    offset: int,
    watching_only: bool = False,
) -> SessionPage:
    query = _sessions(principal)
    if watching_only:
        query = query.where(ChatSession.id.in_(watching(principal.staff_id)))
    if status == OPEN:
        query = query.where(ChatSession.status != SessionStatus.CLOSED)
    elif status == SERVING:
        query = query.where(ChatSession.status.in_(SERVING_STATUSES))
    elif status is not None:
        query = query.where(ChatSession.status == status)
    if mine:
        query = query.where(ChatSession.assignee_id == principal.staff_id)
    if customer_id is not None:
        query = query.where(ChatSession.customer_id == customer_id)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    order: tuple[Any, ...]
    if status == SessionStatus.QUEUED:
        order = (ChatSession.priority.desc(), ChatSession.queued_at, ChatSession.id)
    else:
        activity = func.greatest(
            ChatSession.created_at,
            ChatSession.last_customer_message_at,
            ChatSession.last_agent_message_at,
        )
        order = (activity.desc(), ChatSession.id.desc())
    rows = (await session.execute(query.order_by(*order).limit(limit).offset(offset))).all()
    chats = [row[0] for row in rows]
    roles = await my_roles(session, principal, chats)
    extras = await session_extras(session, principal, chats)
    return SessionPage(
        items=[
            SessionOut(**_session_out(*row, roles.get(row[0].id), extras.get(row[0].id)))
            for row in rows
        ],
        total=total or 0,
    )


async def visible_session(
    session: AsyncSession, principal: Principal, session_id: UUID
) -> tuple[ChatSession, str, str | None, str]:
    row = (await session.execute(_sessions(principal).where(ChatSession.id == session_id))).first()
    # 看不到与不存在返回同样的 404。
    if row is None:
        raise NotFound(SESSION_NOT_FOUND)
    return row


async def get_session(
    session: AsyncSession, principal: Principal, session_id: UUID
) -> SessionDetail:
    row = await visible_session(session, principal, session_id)
    events = (
        await session.scalars(
            select(SessionEvent)
            .where(SessionEvent.session_id == session_id)
            .order_by(SessionEvent.created_at, SessionEvent.id)
        )
    ).all()
    roles = await my_roles(session, principal, [row[0]])
    extras = await session_extras(session, principal, [row[0]])
    watchers = await session.execute(
        select(SessionWatcher, Staff.display_name)
        .join(
            Staff,
            and_(Staff.tenant_id == SessionWatcher.tenant_id, Staff.id == SessionWatcher.staff_id),
        )
        .where(SessionWatcher.session_id == session_id, SessionWatcher.left_at.is_(None))
        .order_by(SessionWatcher.joined_at)
    )
    return SessionDetail(
        **_session_out(*row, roles.get(session_id), extras.get(session_id)),
        watchers=[
            WatcherOut(staff_id=w.staff_id, display_name=name, role=w.role, joined_at=w.joined_at)
            for w, name in watchers
        ],
        events=[
            SessionEventOut(
                id=e.id,
                type=e.type,
                actor_type=e.actor_type,
                actor_id=e.actor_id,
                payload=e.payload,
                created_at=e.created_at,
            )
            for e in events
        ],
    )


async def close_session(
    ctx: AppContext, session: AsyncSession, principal: Principal, session_id: UUID
) -> SessionOut:
    """坐席结束自己接待的会话；有 session:transfer_any 的人可以结束可见范围内的任意会话。"""
    chat, *_ = await visible_session(session, principal, session_id)
    if chat.assignee_id != principal.staff_id and not principal.has(
        Permission.SESSION_TRANSFER_ANY
    ):
        raise Forbidden("只能结束自己接待的会话")
    await session.commit()
    await engine.close_session(
        ctx,
        principal.tenant_id,
        session_id,
        reason=CloseReason.AGENT,
        actor_type=engine.ActorType.STAFF,
        actor_id=principal.staff_id,
    )
    session.expire_all()
    row = await visible_session(session, principal, session_id)
    extras = await session_extras(session, principal, [row[0]])
    return SessionOut(**_session_out(*row, None, extras.get(session_id)))
