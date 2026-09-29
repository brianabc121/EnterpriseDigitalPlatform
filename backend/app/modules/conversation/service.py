"""员工查看 Room 与消息历史。可见范围沿用客户的数据范围：能看到客户，才能看到它的 Room。"""

from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.modules.conversation.models import Message, Room, SenderType
from app.modules.conversation.schemas import MessageOut, MessagePage, RoomOut, RoomPage
from app.modules.customer.models import Customer
from app.modules.customer.service import visible_to
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal

ROOM_NOT_FOUND = "会话不存在"


def _visible_rooms(principal: Principal) -> Select[Room, str]:
    return (
        select(Room, Customer.display_name)
        .join(Customer, and_(Customer.tenant_id == Room.tenant_id, Customer.id == Room.customer_id))
        .where(visible_to(principal))
    )


def _room_out(room: Room, customer_name: str) -> RoomOut:
    return RoomOut(
        id=room.id,
        customer_id=room.customer_id,
        customer_display_name=customer_name,
        channel_account_id=room.channel_account_id,
        im_group_id=room.im_group_id,
        last_message_at=room.last_message_at,
        last_active_at=room.last_active_at,
        created_at=room.created_at,
    )


async def list_rooms(
    session: AsyncSession,
    principal: Principal,
    *,
    customer_id: UUID | None,
    limit: int,
    offset: int,
) -> RoomPage:
    query = _visible_rooms(principal)
    if customer_id is not None:
        query = query.where(Room.customer_id == customer_id)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.execute(
        query.order_by(
            Room.last_message_at.desc().nulls_last(), Room.created_at.desc(), Room.id.desc()
        )
        .limit(limit)
        .offset(offset)
    )
    return RoomPage(items=[_room_out(room, name) for room, name in rows], total=total or 0)


async def list_messages(
    session: AsyncSession,
    principal: Principal,
    room_id: UUID,
    *,
    before: UUID | None,
    limit: int,
) -> MessagePage:
    room = (await session.execute(_visible_rooms(principal).where(Room.id == room_id))).first()
    # 看不到与不存在返回同样的 404。
    if room is None:
        raise NotFound(ROOM_NOT_FOUND)

    return await message_page(session, Message.room_id == room_id, before=before, limit=limit)


async def message_page(
    session: AsyncSession, scope: ColumnElement[bool], *, before: UUID | None, limit: int
) -> MessagePage:
    """按发送时间倒序分页：scope 限定范围（某个 Room 或某个会话），before 为上一页最后一条的 id。"""
    query = select(Message).where(scope)
    if before is not None:
        anchor = await session.scalar(select(Message.sent_at).where(scope, Message.id == before))
        if anchor is None:
            raise NotFound("消息不存在")
        query = query.where(
            or_(
                Message.sent_at < anchor,
                and_(Message.sent_at == anchor, Message.id < before),
            )
        )
    rows = await session.scalars(
        query.order_by(tuple_(Message.sent_at, Message.id).desc()).limit(limit + 1)
    )
    messages = list(rows.all())
    return MessagePage(
        items=await messages_out(session, messages[:limit]), has_more=len(messages) > limit
    )


async def messages_out(session: AsyncSession, messages: list[Message]) -> list[MessageOut]:
    """转换为接口格式，并补上坐席姓名。"""
    staff_ids = {m.sender_id for m in messages if m.sender_type == SenderType.AGENT and m.sender_id}
    names: dict[UUID, str] = {}
    if staff_ids:
        rows = await session.execute(
            select(Staff.id, Staff.display_name).where(Staff.id.in_(staff_ids))
        )
        names = {staff_id: name for staff_id, name in rows}
    return [
        MessageOut.model_validate(m, from_attributes=True).model_copy(
            update={"sender_name": names.get(m.sender_id) if m.sender_id else None}
        )
        for m in messages
    ]
