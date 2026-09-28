"""会话与留言查询（带数据范围）以及坐席发起的会话操作。

会话的可见范围：
- session:read_all：全部会话；
- 否则：分配给自己的会话、自己名下客户的会话；
- 另有 session:read_team 时：所带技能组的会话、组员接待的会话、组员名下客户的会话。
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.context import AppContext
from app.core.errors import Forbidden, NotFound
from app.core.permissions import Permission
from app.modules.conversation.models import (
    ChatSession,
    CloseReason,
    Room,
    SessionEvent,
    SessionStatus,
    Ticket,
    TicketSource,
    TicketStatus,
)
from app.modules.customer.models import Customer
from app.modules.customer.service import visible_to as customer_visible_to
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.routing.models import SkillGroupMember
from app.modules.routing.scope import led_groups, team_members
from app.modules.sessions import engine
from app.modules.sessions.schemas import (
    SessionDetail,
    SessionEventOut,
    SessionOut,
    SessionPage,
    TicketOut,
    TicketPage,
)

SESSION_NOT_FOUND = "会话不存在"
TICKET_NOT_FOUND = "留言不存在"
OPEN = "open"

_Assignee = aliased(Staff)


def session_visible_to(principal: Principal) -> ColumnElement[bool]:
    """需要与 Customer 连接查询（见 _sessions）。"""
    if principal.has(Permission.SESSION_READ_ALL):
        return true()
    me = principal.staff_id
    conditions: list[ColumnElement[bool]] = [
        ChatSession.assignee_id == me,
        Customer.owner_id == me,
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


def _session_out(
    chat: ChatSession, customer_name: str, assignee_name: str | None, group_id: str
) -> dict[str, Any]:
    return {
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
        "last_customer_message_at": chat.last_customer_message_at,
        "last_agent_message_at": chat.last_agent_message_at,
        "csat": chat.csat,
        "csat_comment": chat.csat_comment,
        "created_at": chat.created_at,
    }


async def list_sessions(
    session: AsyncSession,
    principal: Principal,
    *,
    status: str | None,
    mine: bool,
    customer_id: UUID | None,
    limit: int,
    offset: int,
) -> SessionPage:
    query = _sessions(principal)
    if status == OPEN:
        query = query.where(ChatSession.status != SessionStatus.CLOSED)
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
    rows = await session.execute(query.order_by(*order).limit(limit).offset(offset))
    return SessionPage(items=[SessionOut(**_session_out(*row)) for row in rows], total=total or 0)


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
    return SessionDetail(
        **_session_out(*row),
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
    return SessionOut(**_session_out(*await visible_session(session, principal, session_id)))


# ---- 留言 ----


def ticket_visible_to(principal: Principal) -> ColumnElement[bool]:
    """留言：指派给自己的、自己能看到其客户的、所在技能组的。"""
    if principal.has(Permission.SESSION_READ_ALL) or principal.has(Permission.CUSTOMER_READ_ALL):
        return true()
    my_groups = select(SkillGroupMember.skill_group_id).where(
        SkillGroupMember.staff_id == principal.staff_id
    )
    return or_(
        Ticket.assignee_id == principal.staff_id,
        Ticket.skill_group_id.in_(my_groups),
        customer_visible_to(principal),
    )


def _tickets(principal: Principal) -> Select[Ticket, str]:
    return (
        select(Ticket, Customer.display_name)
        .join(
            Customer,
            and_(Customer.tenant_id == Ticket.tenant_id, Customer.id == Ticket.customer_id),
        )
        .where(ticket_visible_to(principal))
    )


def _ticket_out(ticket: Ticket, customer_name: str) -> TicketOut:
    return TicketOut(
        id=ticket.id,
        customer_id=ticket.customer_id,
        customer_display_name=customer_name,
        session_id=ticket.session_id,
        source=TicketSource(ticket.source),
        content=ticket.content,
        contact=ticket.contact,
        status=TicketStatus(ticket.status),
        assignee_id=ticket.assignee_id,
        skill_group_id=ticket.skill_group_id,
        created_at=ticket.created_at,
        closed_at=ticket.closed_at,
    )


async def list_tickets(
    session: AsyncSession,
    principal: Principal,
    *,
    status: TicketStatus | None,
    limit: int,
    offset: int,
) -> TicketPage:
    query = _tickets(principal)
    if status is not None:
        query = query.where(Ticket.status == status)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.execute(
        query.order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(limit).offset(offset)
    )
    return TicketPage(items=[_ticket_out(t, name) for t, name in rows], total=total or 0)


async def complete_ticket(
    session: AsyncSession, principal: Principal, ticket_id: UUID
) -> TicketOut:
    row = (await session.execute(_tickets(principal).where(Ticket.id == ticket_id))).first()
    if row is None:
        raise NotFound(TICKET_NOT_FOUND)
    ticket, customer_name = row
    if ticket.status != TicketStatus.DONE:
        ticket.status = TicketStatus.DONE
        ticket.closed_at = datetime.now(UTC)
        ticket.assignee_id = ticket.assignee_id or principal.staff_id
        await session.commit()
    return _ticket_out(ticket, customer_name)
