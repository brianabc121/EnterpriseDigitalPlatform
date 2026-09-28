"""访客端查询：消息历史与当前会话状态（排队位置、接待坐席）。

访客端实时消息走 IM；OpenIM 在用户刚连上的一两秒内可能还没把他当作在线用户推送，
所以 Widget 同时用这里的历史接口补齐（实施计划 §10.1）。
"""

from uuid import UUID

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.modules.conversation.models import (
    ChatSession,
    Message,
    Room,
    SenderType,
    SessionStatus,
)
from app.modules.conversation.provisioning import BOT_NICKNAME
from app.modules.iam.models import Staff
from app.modules.visitor.deps import VisitorContext
from app.modules.visitor.schemas import (
    AttachmentOut,
    VisitorMessageOut,
    VisitorMessagePage,
    VisitorSessionState,
)

ROOM_NOT_FOUND = "会话不存在"


async def _room_of(session: AsyncSession, identity_id: UUID) -> Room:
    room = await session.scalar(select(Room).where(Room.identity_id == identity_id))
    if room is None:
        raise NotFound(ROOM_NOT_FOUND)
    return room


async def list_messages(
    visitor: VisitorContext, *, before: UUID | None, limit: int
) -> VisitorMessagePage:
    """只返回已经进入 IM 的消息（有 IM 消息 ID），与 Widget 从 IM 收到的消息按 ID 对齐。"""
    session = visitor.session
    room = await _room_of(session, visitor.claims.identity_id)
    query = select(Message).where(Message.room_id == room.id, Message.channel_msg_id.is_not(None))
    if before is not None:
        anchor = await session.scalar(
            select(Message.sent_at).where(Message.room_id == room.id, Message.id == before)
        )
        if anchor is None:
            raise NotFound("消息不存在")
        query = query.where(
            or_(Message.sent_at < anchor, and_(Message.sent_at == anchor, Message.id < before))
        )
    rows = list(
        (
            await session.scalars(
                query.order_by(tuple_(Message.sent_at, Message.id).desc()).limit(limit + 1)
            )
        ).all()
    )
    messages = rows[:limit]
    staff_ids = {m.sender_id for m in messages if m.sender_type == SenderType.AGENT and m.sender_id}
    names: dict[UUID, str] = {}
    if staff_ids:
        result = await session.execute(
            select(Staff.id, Staff.display_name).where(Staff.id.in_(staff_ids))
        )
        names = {staff_id: name for staff_id, name in result}
    return VisitorMessagePage(
        items=[
            VisitorMessageOut(
                id=m.id,
                server_msg_id=m.channel_msg_id or "",
                sender_type=m.sender_type,
                sender_name=(
                    names.get(m.sender_id)
                    if m.sender_type == SenderType.AGENT and m.sender_id
                    else BOT_NICKNAME
                    if m.sender_type == SenderType.BOT
                    else None
                ),
                content_type=m.content_type,
                text=m.text_plain,
                attachment=(
                    AttachmentOut.model_validate(m.content)
                    if m.content_type in ("image", "file") and m.content.get("url")
                    else None
                ),
                sent_at=m.sent_at,
            )
            for m in messages
        ],
        has_more=len(rows) > limit,
    )


async def session_state(visitor: VisitorContext) -> VisitorSessionState:
    session = visitor.session
    room = await _room_of(session, visitor.claims.identity_id)
    chat = await session.scalar(
        select(ChatSession)
        .where(ChatSession.room_id == room.id)
        .order_by(ChatSession.created_at.desc(), ChatSession.id.desc())
        .limit(1)
    )
    if chat is None:
        return VisitorSessionState(status="none")
    position: int | None = None
    if chat.status == SessionStatus.QUEUED and chat.queued_at is not None:
        # 排在前面的：优先级更高的，或同优先级更早排队的。
        ahead = await session.scalar(
            select(func.count())
            .select_from(ChatSession)
            .where(
                ChatSession.status == SessionStatus.QUEUED,
                ChatSession.id != chat.id,
                or_(
                    ChatSession.priority > chat.priority,
                    and_(
                        ChatSession.priority == chat.priority,
                        ChatSession.queued_at < chat.queued_at,
                    ),
                ),
            )
        )
        position = (ahead or 0) + 1
    assignee = await session.get(Staff, chat.assignee_id) if chat.assignee_id else None
    return VisitorSessionState(
        status=chat.status,
        session_id=chat.id,
        queue_position=position,
        assignee_name=assignee.display_name if assignee else None,
        closed_at=chat.closed_at,
        csat=chat.csat,
    )
