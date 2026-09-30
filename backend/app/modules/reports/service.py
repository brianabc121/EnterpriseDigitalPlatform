"""运营报表与首页实时数据（设计文档 §17.3）。

- 服务指标按会话创建时间归到日期（时区可选，默认 EDP_USAGE_TIMEZONE）。
- 排队、首次响应、处理时长以会话事件中的首次排队、首次分配为起点：转接或退回队列会刷新会话上的
  assigned_at、queued_at，不能直接用。
- 数据范围与会话列表一致（session_visible_to）：管理员看到全部，主管看到所带团队。
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Row, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Subquery

from app.core.dates import day_bounds, today
from app.core.permissions import Permission
from app.modules.conversation.models import (
    ChatSession,
    CloseReason,
    Message,
    SenderType,
    SessionEvent,
    SessionStatus,
    SessionTransfer,
    TransferStatus,
)
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.reports.schemas import (
    AgentReport,
    AgentStats,
    DailyStats,
    Overview,
    OverviewTotals,
    Realtime,
)
from app.modules.routing.models import AgentState, AgentStatus
from app.modules.routing.scope import team_members
from app.modules.sessions.service import session_visible_to
from app.modules.todos.models import Todo, TodoType
from app.modules.todos.presets import LEAVE_MESSAGE
from app.modules.todos.service import visible_to as todo_visible_to

SERVING = (SessionStatus.HUMAN_SERVING, SessionStatus.TRANSFERRING)
DONE_TRANSFERS = (TransferStatus.ACCEPTED, TransferStatus.COMPLETED)
SATISFIED = 4


def _with_customer(query: Any, owner: Any) -> Any:
    return query.join(
        Customer, and_(Customer.tenant_id == owner.tenant_id, Customer.id == owner.customer_id)
    )


def _scoped_sessions(principal: Principal, start: datetime, end: datetime) -> Subquery:
    """可见范围内、在 [start, end) 创建的会话，附带首次排队和首次分配时间。"""
    base = (
        _with_customer(select(ChatSession), ChatSession)
        .where(
            ChatSession.tenant_id == principal.tenant_id,
            ChatSession.created_at >= start,
            ChatSession.created_at < end,
            session_visible_to(principal),
        )
        .with_only_columns(
            ChatSession.id,
            ChatSession.created_at,
            ChatSession.assignee_id,
            ChatSession.status,
            ChatSession.close_reason,
            ChatSession.first_response_at,
            ChatSession.closed_at,
            ChatSession.csat,
        )
        .cte("scoped")
    )
    firsts = (
        select(
            SessionEvent.session_id,
            func.min(SessionEvent.created_at)
            .filter(SessionEvent.type == "queued")
            .label("first_queued_at"),
            func.min(SessionEvent.created_at)
            .filter(SessionEvent.type == "assigned")
            .label("first_assigned_at"),
            func.min(SessionEvent.created_at)
            .filter(SessionEvent.type == "ai_serving")
            .label("ai_started_at"),
            func.min(SessionEvent.created_at)
            .filter(SessionEvent.type == "handoff")
            .label("handed_off_at"),
        )
        .where(
            SessionEvent.tenant_id == principal.tenant_id,
            SessionEvent.session_id.in_(select(base.c.id)),
            SessionEvent.type.in_(("queued", "assigned", "ai_serving", "handoff")),
        )
        .group_by(SessionEvent.session_id)
        .subquery()
    )
    return (
        select(
            base,
            firsts.c.first_queued_at,
            firsts.c.first_assigned_at,
            firsts.c.ai_started_at,
            firsts.c.handed_off_at,
        )
        .outerjoin(firsts, firsts.c.session_id == base.c.id)
        .subquery("sessions_in_range")
    )


def _stats_columns(r: Subquery) -> list[Any]:
    def seconds(later: Any, earlier: Any) -> Any:
        return func.avg(func.extract("epoch", later - earlier)).filter(later >= earlier)

    return [
        func.count().label("sessions"),
        func.count(r.c.first_assigned_at).label("human_sessions"),
        func.count().filter(r.c.status == SessionStatus.CLOSED).label("closed_sessions"),
        func.count().filter(r.c.close_reason == CloseReason.LEAVE_MESSAGE).label("missed"),
        seconds(r.c.first_assigned_at, r.c.first_queued_at).label("wait"),
        seconds(r.c.first_response_at, r.c.first_assigned_at).label("first_response"),
        seconds(r.c.closed_at, r.c.first_assigned_at).label("handle"),
        func.avg(r.c.csat).label("csat_avg"),
        func.count(r.c.csat).label("csat_count"),
        func.count().filter(r.c.csat >= SATISFIED).label("satisfied"),
        func.count(r.c.ai_started_at).label("ai_sessions"),
        func.count().filter(r.c.close_reason == CloseReason.AI_RESOLVED).label("ai_resolved"),
        func.count(r.c.handed_off_at).filter(r.c.ai_started_at.is_not(None)).label("ai_handoffs"),
    ]


def _round(value: Decimal | float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


_EMPTY_STATS: dict[str, Any] = {
    "sessions": 0,
    "human_sessions": 0,
    "closed_sessions": 0,
    "missed_sessions": 0,
    "avg_wait_seconds": None,
    "avg_first_response_seconds": None,
    "avg_handle_seconds": None,
    "csat_avg": None,
    "csat_count": 0,
    "satisfied_rate": None,
    "ai_sessions": 0,
    "ai_resolved": 0,
    "ai_handoffs": 0,
    "ai_resolution_rate": None,
}


def _stats(row: Row[Any] | None) -> dict[str, Any]:
    if row is None:
        return dict(_EMPTY_STATS)
    m = row._mapping
    return {
        "sessions": m["sessions"],
        "human_sessions": m["human_sessions"],
        "closed_sessions": m["closed_sessions"],
        "missed_sessions": m["missed"],
        "avg_wait_seconds": _round(m["wait"]),
        "avg_first_response_seconds": _round(m["first_response"]),
        "avg_handle_seconds": _round(m["handle"]),
        "csat_avg": _round(m["csat_avg"], 2),
        "csat_count": m["csat_count"],
        "satisfied_rate": (round(m["satisfied"] / m["csat_count"], 4) if m["csat_count"] else None),
        "ai_sessions": m["ai_sessions"],
        "ai_resolved": m["ai_resolved"],
        "ai_handoffs": m["ai_handoffs"],
        "ai_resolution_rate": (
            round(m["ai_resolved"] / m["ai_sessions"], 4) if m["ai_sessions"] else None
        ),
    }


def _range(start: date, end: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return day_bounds(start, tz)[0], day_bounds(end, tz)[1]


async def overview(
    session: AsyncSession, principal: Principal, start: date, end: date, tz: ZoneInfo
) -> Overview:
    since, until = _range(start, end, tz)
    rows = _scoped_sessions(principal, since, until)
    totals = (await session.execute(select(*_stats_columns(rows)))).first()
    local_day = func.date(func.timezone(tz.key, rows.c.created_at)).label("day")
    by_day = {
        row.day: row
        for row in await session.execute(
            select(local_day, *_stats_columns(rows)).group_by(local_day)
        )
    }

    messages = (
        await session.execute(
            _with_customer(
                select(
                    func.count().filter(Message.sender_type == SenderType.CUSTOMER),
                    func.count().filter(Message.sender_type == SenderType.AGENT),
                )
                .select_from(Message)
                .join(
                    ChatSession,
                    and_(
                        ChatSession.tenant_id == Message.tenant_id,
                        ChatSession.id == Message.session_id,
                    ),
                ),
                ChatSession,
            ).where(
                Message.tenant_id == principal.tenant_id,
                Message.sent_at >= since,
                Message.sent_at < until,
                session_visible_to(principal),
            )
        )
    ).one()
    # 新增留言："留言"类待办（非工作时间、排队超时和访客自己提交的）。
    tickets = await session.scalar(
        select(func.count())
        .select_from(Todo)
        .join(TodoType, TodoType.id == Todo.type_id)
        .where(
            Todo.tenant_id == principal.tenant_id,
            TodoType.code == LEAVE_MESSAGE,
            Todo.created_at >= since,
            Todo.created_at < until,
            todo_visible_to(principal),
        )
    )
    transfers = await session.scalar(
        _with_customer(
            select(func.count())
            .select_from(SessionTransfer)
            .join(
                ChatSession,
                and_(
                    ChatSession.tenant_id == SessionTransfer.tenant_id,
                    ChatSession.id == SessionTransfer.session_id,
                ),
            ),
            ChatSession,
        ).where(
            SessionTransfer.tenant_id == principal.tenant_id,
            SessionTransfer.status.in_(DONE_TRANSFERS),
            SessionTransfer.decided_at >= since,
            SessionTransfer.decided_at < until,
            session_visible_to(principal),
        )
    )
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return Overview(
        start=start,
        end=end,
        timezone=tz.key,
        totals=OverviewTotals(
            **_stats(totals),
            messages_in=messages[0],
            agent_messages=messages[1],
            tickets=tickets or 0,
            transfers=transfers or 0,
        ),
        days=[DailyStats(day=d, **_stats(by_day.get(d))) for d in days],
    )


def _sees_all(principal: Principal) -> bool:
    return principal.has(Permission.SESSION_READ_ALL) or principal.has(Permission.ROUTING_MANAGE)


async def _agents_in_scope(session: AsyncSession, principal: Principal) -> set[UUID] | None:
    """可以查看哪些坐席的数据：None 表示全部；主管为所带团队；其他人只有自己。"""
    if _sees_all(principal):
        return None
    if principal.has(Permission.SESSION_READ_TEAM):
        team = set((await session.scalars(team_members(principal.staff_id))).all())
        return team | {principal.staff_id}
    return {principal.staff_id}


async def agents(
    session: AsyncSession, principal: Principal, start: date, end: date, tz: ZoneInfo
) -> AgentReport:
    since, until = _range(start, end, tz)
    scope = await _agents_in_scope(session, principal)
    rows = _scoped_sessions(principal, since, until)
    per_agent = {
        row.assignee_id: row
        for row in await session.execute(
            select(rows.c.assignee_id, *_stats_columns(rows))
            .where(rows.c.assignee_id.is_not(None))
            .group_by(rows.c.assignee_id)
        )
    }
    sent: dict[UUID | None, int] = {
        sender_id: count
        for sender_id, count in await session.execute(
            select(Message.sender_id, func.count())
            .where(
                Message.tenant_id == principal.tenant_id,
                Message.sender_type == SenderType.AGENT,
                Message.sent_at >= since,
                Message.sent_at < until,
                Message.sender_id.is_not(None),
            )
            .group_by(Message.sender_id)
        )
    }
    transfers_out: dict[UUID | None, int] = {
        staff_id: count
        for staff_id, count in await session.execute(
            select(SessionTransfer.from_staff_id, func.count())
            .where(
                SessionTransfer.tenant_id == principal.tenant_id,
                SessionTransfer.status.in_(DONE_TRANSFERS),
                SessionTransfer.decided_at >= since,
                SessionTransfer.decided_at < until,
                SessionTransfer.from_staff_id.is_not(None),
            )
            .group_by(SessionTransfer.from_staff_id)
        )
    }
    states = {
        staff_id: status
        for staff_id, status in await session.execute(
            select(AgentState.staff_id, AgentState.status).where(
                AgentState.tenant_id == principal.tenant_id
            )
        )
    }
    ids = set(states) | set(per_agent) | set(sent)
    if scope is not None:
        ids &= scope
    names = {
        staff_id: name
        for staff_id, name in await session.execute(
            select(Staff.id, Staff.display_name).where(
                Staff.tenant_id == principal.tenant_id, Staff.id.in_(ids)
            )
        )
    }
    items = [
        AgentStats(
            staff_id=staff_id,
            display_name=names[staff_id],
            status=states.get(staff_id, AgentStatus.OFFLINE),
            messages=sent.get(staff_id, 0),
            transfers_out=transfers_out.get(staff_id, 0),
            **_stats(per_agent.get(staff_id)),
        )
        for staff_id in ids
        if staff_id in names
    ]
    items.sort(key=lambda a: (-a.sessions, a.display_name))
    return AgentReport(start=start, end=end, timezone=tz.key, items=items)


async def realtime(
    session: AsyncSession, principal: Principal, tz: ZoneInfo, now: datetime | None = None
) -> Realtime:
    now = now or datetime.now(UTC)
    tenant = principal.tenant_id
    queued, oldest, ai_serving = (
        await session.execute(
            select(
                func.count().filter(ChatSession.status == SessionStatus.QUEUED),
                func.min(ChatSession.queued_at).filter(ChatSession.status == SessionStatus.QUEUED),
                func.count().filter(ChatSession.status == SessionStatus.AI_SERVING),
            ).where(
                ChatSession.tenant_id == tenant,
                ChatSession.status.in_((SessionStatus.QUEUED, SessionStatus.AI_SERVING)),
            )
        )
    ).one()
    serving = await session.scalar(
        _with_customer(select(func.count()).select_from(ChatSession), ChatSession).where(
            ChatSession.tenant_id == tenant,
            ChatSession.status.in_(SERVING),
            session_visible_to(principal),
        )
    )

    scope = await _agents_in_scope(session, principal)
    by_status: dict[str, int] = {}
    if scope is None or principal.has(Permission.SESSION_READ_TEAM):
        query = select(AgentState.status, func.count()).where(AgentState.tenant_id == tenant)
        if scope is not None:
            query = query.where(AgentState.staff_id.in_(scope))
        grouped = await session.execute(query.group_by(AgentState.status))
        by_status = {status: count for status, count in grouped}

    start, end = day_bounds(today(tz, now), tz)
    rows = _scoped_sessions(principal, start, end)
    today_row = (await session.execute(select(*_stats_columns(rows)))).first()
    stats = _stats(today_row)
    mine = (
        await session.execute(
            select(
                func.count().filter(ChatSession.status.in_(SERVING)),
                func.count().filter(ChatSession.created_at >= start, ChatSession.created_at < end),
            ).where(ChatSession.tenant_id == tenant, ChatSession.assignee_id == principal.staff_id)
        )
    ).one()
    return Realtime(
        queued=queued,
        ai_serving=ai_serving,
        longest_wait_seconds=int((now - oldest).total_seconds()) if oldest else None,
        serving=serving or 0,
        agents_online=by_status.get(AgentStatus.ONLINE, 0),
        agents_busy=by_status.get(AgentStatus.BUSY, 0),
        agents_away=by_status.get(AgentStatus.AWAY, 0),
        today_sessions=stats["sessions"],
        today_closed=stats["closed_sessions"],
        today_csat_avg=stats["csat_avg"],
        today_csat_count=stats["csat_count"],
        my_serving=mine[0],
        my_today_sessions=mine[1],
    )
