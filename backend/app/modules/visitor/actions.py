"""访客端操作：满意度评价、留言、请求人工、上传文件。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.context import AppContext
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.ratelimit import Limit, RateLimiter
from app.modules.conversation.models import (
    ChatSession,
    Room,
    SessionStatus,
    Ticket,
    TicketSource,
)
from app.modules.customer.models import Customer
from app.modules.files import service as files
from app.modules.routing.assign import PolicyResolver
from app.modules.sessions import collab, engine
from app.modules.tenancy.models import Tenant
from app.modules.visitor.deps import VisitorContext
from app.modules.visitor.schemas import CsatRequest, LeaveMessageRequest

CSAT_WINDOW = timedelta(days=7)
LEAVE_MESSAGE_LIMIT = Limit("visitor-ticket", 5, 3600)
UPLOAD_LIMIT = Limit("visitor-upload", 30, 3600)


async def _room(visitor: VisitorContext) -> Room:
    room = await visitor.session.scalar(
        select(Room).where(Room.identity_id == visitor.claims.identity_id)
    )
    if room is None:
        raise NotFound("会话不存在")
    return room


async def rate(visitor: VisitorContext, payload: CsatRequest) -> None:
    """对已结束的会话评价一次（结束后 7 天内）。"""
    room = await _room(visitor)
    chat = await visitor.session.scalar(
        select(ChatSession)
        .where(ChatSession.id == payload.session_id, ChatSession.room_id == room.id)
        .with_for_update()
    )
    if chat is None:
        raise NotFound("会话不存在")
    if chat.status != SessionStatus.CLOSED or chat.closed_at is None:
        raise Conflict("会话结束后才能评价")
    if chat.csat is not None:
        raise Conflict("已经评价过了")
    if chat.closed_at < datetime.now(UTC) - CSAT_WINDOW:
        raise Conflict("评价已过期")
    chat.csat = payload.score
    chat.csat_comment = payload.comment or None
    engine.record_event(
        visitor.session,
        chat,
        "csat",
        actor_type=engine.ActorType.VISITOR,
        payload={"score": payload.score},
    )
    await visitor.session.commit()


async def leave_message(
    visitor: VisitorContext, limiter: RateLimiter, payload: LeaveMessageRequest
) -> None:
    """访客留言：指派给归属坐席或默认技能组，由坐席在"留言"里跟进。"""
    await limiter.check(LEAVE_MESSAGE_LIMIT, str(visitor.claims.identity_id))
    session = visitor.session
    room = await _room(visitor)
    policy = await PolicyResolver(session).for_channel(room.channel_account_id)
    latest = await session.scalar(
        select(ChatSession.id)
        .where(ChatSession.room_id == room.id)
        .order_by(ChatSession.created_at.desc())
        .limit(1)
    )
    owner = await session.scalar(select(Customer.owner_id).where(Customer.id == room.customer_id))
    session.add(
        Ticket(
            tenant_id=room.tenant_id,
            customer_id=room.customer_id,
            session_id=latest,
            source=TicketSource.VISITOR,
            content=payload.content,
            contact=payload.contact or None,
            assignee_id=owner,
            skill_group_id=policy.default_skill_group_id,
        )
    )
    await session.commit()


async def request_human(ctx: AppContext, visitor: VisitorContext) -> None:
    room = await _room(visitor)
    await visitor.session.commit()
    await engine.request_handoff(
        ctx,
        visitor.claims.tenant_id,
        room.id,
        reason="visitor_request",
        actor_type=engine.ActorType.VISITOR,
    )


async def cancel_queue(ctx: AppContext, visitor: VisitorContext) -> None:
    room = await _room(visitor)
    await visitor.session.commit()
    await collab.cancel_queue(ctx, visitor.claims.tenant_id, room.id)


async def tenant_code(visitor: VisitorContext, tenant_id: UUID) -> str:
    code = await visitor.session.scalar(select(Tenant.code).where(Tenant.id == tenant_id))
    if code is None:
        raise NotFound("租户不存在")
    return code


async def new_upload(
    ctx: AppContext,
    visitor: VisitorContext,
    limiter: RateLimiter,
    *,
    filename: str,
    content_type: str,
    size: int,
) -> files.UploadTicket:
    await limiter.check(UPLOAD_LIMIT, str(visitor.claims.identity_id))
    if size <= 0:
        raise Unprocessable("文件为空")
    return files.new_upload(
        ctx.settings,
        tenant_code=await tenant_code(visitor, visitor.claims.tenant_id),
        filename=filename,
        content_type=content_type,
        size=size,
    )
