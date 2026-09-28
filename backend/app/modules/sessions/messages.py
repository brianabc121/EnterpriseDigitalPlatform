"""坐席经平台发送消息（设计文档 §9.2）。

先写库（pending，消息 ID 即 pmid），再以坐席身份经 OpenIM REST 发到服务群，消息的 ex 带 pmid；
回调或对账拿到这条消息时按 pmid 关联到同一行（见 conversation/ingest.py），不会重复入库。
同一个 client_msg_id 重复提交时返回已有的消息；之前发送失败的会重新发送。
"""

import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, Forbidden, ServiceUnavailable
from app.core.ids import new_id
from app.integrations.openim import ContentType, OpenIMError
from app.modules.conversation import imids, outbox
from app.modules.conversation.models import (
    Direction,
    Message,
    MessageSource,
    Room,
    SenderType,
    SendStatus,
    SessionStatus,
)
from app.modules.conversation.schemas import MessageOut
from app.modules.conversation.service import messages_out
from app.modules.iam.principal import Principal
from app.modules.sessions.engine import touch_session
from app.modules.sessions.schemas import SendMessageRequest
from app.modules.sessions.service import visible_session

SENDABLE = (SessionStatus.HUMAN_SERVING, SessionStatus.TRANSFERRING)
SEND_FAILED = "消息发送失败，请稍后重试"


async def send_message(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    session_id: UUID,
    payload: SendMessageRequest,
) -> MessageOut:
    chat, *_ = await visible_session(session, principal, session_id)
    if chat.assignee_id != principal.staff_id:
        raise Forbidden("只有接待这个会话的坐席可以回复")
    if chat.status not in SENDABLE:
        raise Conflict(
            "会话已结束" if chat.status == SessionStatus.CLOSED else "会话不在人工接待中"
        )

    message = await session.scalar(
        select(Message).where(
            Message.room_id == chat.room_id,
            Message.sender_id == principal.staff_id,
            Message.source == MessageSource.API,
            Message.client_msg_id == payload.client_msg_id,
        )
    )
    if message is None:
        now = datetime.now(UTC)
        message = Message(
            id=new_id(),
            tenant_id=chat.tenant_id,
            room_id=chat.room_id,
            channel_account_id=chat.channel_account_id,
            session_id=chat.id,
            direction=Direction.OUT,
            sender_type=SenderType.AGENT,
            sender_id=principal.staff_id,
            content_type="text",
            content={"text": payload.text},
            text_plain=payload.text,
            client_msg_id=payload.client_msg_id,
            source=MessageSource.API,
            send_status=SendStatus.PENDING,
            sent_at=now,
        )
        session.add(message)
        touch_session(chat, message)
        await session.commit()
    elif message.send_status == SendStatus.SENT:
        return (await messages_out(session, [message]))[0]

    room = await session.get(Room, chat.room_id)
    assert room is not None
    # 分配时的拉人操作可能还没执行（OpenIM 短暂不可用）：先把这个 Room 的待执行操作执行完。
    await outbox.flush_rooms(ctx, chat.tenant_id, [room.id])
    try:
        sent = await ctx.im.send_group_message(
            send_id=imids.staff_user(principal.tenant_code, principal.staff_id),
            group_id=room.im_group_id,
            content_type=ContentType.TEXT,
            content={"content": message.text_plain or ""},
            sender_nickname=principal.display_name,
            ex=json.dumps({"pmid": str(message.id)}),
        )
    except OpenIMError as exc:
        await session.execute(
            update(Message)
            .where(Message.id == message.id, Message.channel_msg_id.is_(None))
            .values(send_status=SendStatus.FAILED, send_error=str(exc)[:500])
        )
        await session.commit()
        raise ServiceUnavailable(SEND_FAILED) from exc

    # 回调可能已经先到并关联了这一行（channel_msg_id 相同），这里的更新同样成立。
    await session.execute(
        update(Message)
        .where(
            Message.id == message.id,
            or_(Message.channel_msg_id.is_(None), Message.channel_msg_id == sent.server_msg_id),
        )
        .values(
            channel_msg_id=sent.server_msg_id,
            send_status=SendStatus.SENT,
            send_error=None,
            sent_at=datetime.fromtimestamp(sent.send_time / 1000, UTC),
        )
    )
    await session.commit()
    await session.refresh(message)
    return (await messages_out(session, [message]))[0]
