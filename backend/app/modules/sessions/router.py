from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.context import AppContext
from app.core.deps import get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.conversation.models import SessionStatus, TicketStatus
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.sessions import service
from app.modules.sessions.schemas import (
    SessionDetail,
    SessionOut,
    SessionPage,
    TicketOut,
    TicketPage,
)

router = APIRouter(prefix="/api/v1", tags=["sessions"], responses=ERROR_RESPONSES)

CanServe = Annotated[Principal, Depends(require_permission(Permission.WORKBENCH_USE))]
Context = Annotated[AppContext, Depends(get_context)]


@router.get("/sessions", response_model=SessionPage)
async def list_sessions(
    session: TenantDb,
    principal: CanServe,
    status: Annotated[
        SessionStatus | Literal["open"] | None,
        Query(description="open 表示所有未结束的会话"),
    ] = None,
    mine: Annotated[bool, Query(description="只看分配给自己的会话")] = False,
    customer_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SessionPage:
    """可见范围内的会话。排队中的按优先级和排队时间排序，其余按最近活动时间倒序。"""
    return await service.list_sessions(
        session,
        principal,
        status=status,
        mine=mine,
        customer_id=customer_id,
        limit=limit,
        offset=offset,
    )


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(session_id: UUID, session: TenantDb, principal: CanServe) -> SessionDetail:
    return await service.get_session(session, principal, session_id)


@router.post("/sessions/{session_id}/close", response_model=SessionOut)
async def close_session(
    session_id: UUID, ctx: Context, session: TenantDb, principal: CanServe
) -> SessionOut:
    """结束会话（已结束时直接返回）。坐席被移出服务群，客户收到结束提示。"""
    return await service.close_session(ctx, session, principal, session_id)


@router.get("/tickets", response_model=TicketPage)
async def list_tickets(
    session: TenantDb,
    principal: CanServe,
    status: TicketStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TicketPage:
    """留言：指派给自己的、所在技能组的、自己能看到其客户的。"""
    return await service.list_tickets(session, principal, status=status, limit=limit, offset=offset)


@router.post("/tickets/{ticket_id}/done", response_model=TicketOut)
async def complete_ticket(ticket_id: UUID, session: TenantDb, principal: CanServe) -> TicketOut:
    return await service.complete_ticket(session, principal, ticket_id)
