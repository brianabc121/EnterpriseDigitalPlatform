"""员工查看 Room 与消息历史。可见范围沿用客户的数据范围：能看到客户，才能看到它的 Room。"""

from uuid import UUID

from sqlalchemy import Select, and_, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.modules.conversation.models import Message, Room
from app.modules.conversation.schemas import MessageOut, MessagePage, RoomOut, RoomPage
from app.modules.customer.models import Customer
from app.modules.customer.service import visible_to
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

    query = select(Message).where(Message.room_id == room_id)
    if before is not None:
        anchor = await session.scalar(
            select(Message.sent_at).where(Message.room_id == room_id, Message.id == before)
        )
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
        items=[MessageOut.model_validate(m, from_attributes=True) for m in messages[:limit]],
        has_more=len(messages) > limit,
    )
