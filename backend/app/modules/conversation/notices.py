"""按客户所在渠道的规则通知客户（设计文档 §24.7，待办和订单共用）。

- 官网访客：发一条系统消息；
- 微信客服：回复窗口内直接发送，窗口已关闭时不能发送；
- 企业微信客户联系：不能由平台直接发送，由员工在侧边栏发送（manual）。

通知发到客户最近的对话（有会话时用会话所在的对话）。由调用方提交并刷新发件箱。
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.conversation import outbox
from app.modules.conversation.models import ChatSession, Room


@dataclass(frozen=True)
class CustomerNotice:
    status: str  # sent（已发送）、manual（待员工发送）、unreachable（未能通知）
    channel: str | None
    reason: str | None
    room_id: uuid.UUID | None


async def room_for(
    session: AsyncSession, *, session_id: uuid.UUID | None, customer_id: uuid.UUID | None
) -> Room | None:
    if session_id is not None:
        room_id = await session.scalar(
            select(ChatSession.room_id).where(ChatSession.id == session_id)
        )
        if room_id is not None:
            return await session.get(Room, room_id)
    if customer_id is None:
        return None
    return await session.scalar(
        select(Room)
        .where(Room.customer_id == customer_id)
        .order_by(Room.updated_at.desc())
        .limit(1)
    )


async def send(
    session: AsyncSession,
    *,
    session_id: uuid.UUID | None,
    customer_id: uuid.UUID | None,
    text: str,
    now: datetime,
) -> CustomerNotice:
    from app.modules.wecom.kf import reply_window

    room = await room_for(session, session_id=session_id, customer_id=customer_id)
    channel = await session.get(ChannelAccount, room.channel_account_id) if room else None
    if room is None or channel is None:
        return CustomerNotice("unreachable", None, "客户没有可以联系的对话", None)
    if channel.type == ChannelType.WEB:
        outbox.enqueue_notice(session, room.id, text)
        return CustomerNotice("sent", channel.type, None, room.id)
    if channel.type == ChannelType.WECOM_KF:
        window = await reply_window(session, room.id, now)
        if window.open:
            outbox.enqueue_notice(session, room.id, text)
            return CustomerNotice("sent", channel.type, None, room.id)
        return CustomerNotice("unreachable", channel.type, window.reason, room.id)
    return CustomerNotice(
        "manual", channel.type, "企业微信客户联系不能直接发送，请在侧边栏发送", room.id
    )
