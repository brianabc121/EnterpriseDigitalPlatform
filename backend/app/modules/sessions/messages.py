"""坐席经平台发送消息（设计文档 §9.2）。

先写库（pending，消息 ID 即 pmid），再以坐席身份经 OpenIM REST 发到服务群，消息的 ex 带 pmid；
回调或对账拿到这条消息时按 pmid 关联到同一行（见 conversation/ingest.py），不会重复入库。
同一个 client_msg_id 重复提交时返回已有的消息；之前发送失败的会重新发送。
微信客服等外部渠道以平台消息库为准：先投递给客户，成功后再镜像到服务群（_send_via_channel）。
"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, Forbidden, ServiceUnavailable, Unprocessable
from app.core.ids import new_id
from app.integrations.openim import OpenIMError
from app.modules.ai import copilot
from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.conversation import imids, outbox
from app.modules.conversation.content import im_payload
from app.modules.conversation.models import (
    ChatSession,
    Direction,
    Message,
    MessageSource,
    Room,
    SenderType,
    SendStatus,
    SessionStatus,
    SessionWatcher,
    WatcherRole,
)
from app.modules.conversation.schemas import MessageOut
from app.modules.conversation.service import messages_out
from app.modules.files.service import IMAGE_TYPES
from app.modules.iam.principal import Principal
from app.modules.mail import delivery as mail_delivery
from app.modules.platform.content import check_agent_text
from app.modules.sessions.engine import touch_session
from app.modules.sessions.schemas import SendMessageRequest
from app.modules.sessions.service import visible_session
from app.modules.wecom.kf import reply_window

SENDABLE = (SessionStatus.HUMAN_SERVING, SessionStatus.TRANSFERRING)
CHAT_TEXT_LIMIT = 4000
SEND_FAILED = "消息发送失败，请稍后重试"
# 同一条消息（client_msg_id）的并发重试串行执行：消息表按发送时间分区，唯一约束带着发送时间，
# 不能单靠它去重（咨询锁命名空间，见 outbox、engine）。
_CLIENT_MSG_LOCK = 1003


async def _assisting(session: AsyncSession, session_id: UUID, staff_id: UUID) -> bool:
    """被邀请协助（可以发言）的员工。旁听的主管只能看。"""
    return (
        await session.scalar(
            select(SessionWatcher.role).where(
                SessionWatcher.session_id == session_id,
                SessionWatcher.staff_id == staff_id,
                SessionWatcher.left_at.is_(None),
            )
        )
    ) == WatcherRole.ASSIST


async def send_message(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    session_id: UUID,
    payload: SendMessageRequest,
) -> MessageOut:
    chat, *_ = await visible_session(session, principal, session_id)
    if chat.assignee_id != principal.staff_id and not await _assisting(
        session, chat.id, principal.staff_id
    ):
        raise Forbidden("只有接待或协助这个会话的坐席可以回复")
    if chat.status not in SENDABLE:
        raise Conflict(
            "会话已结束" if chat.status == SessionStatus.CLOSED else "会话不在人工接待中"
        )

    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                _CLIENT_MSG_LOCK,
                func.hashtext(f"{chat.room_id}:{principal.staff_id}:{payload.client_msg_id}"),
            )
        )
    )
    message = await session.scalar(
        select(Message).where(
            Message.room_id == chat.room_id,
            Message.sender_id == principal.staff_id,
            Message.source == MessageSource.API,
            Message.client_msg_id == payload.client_msg_id,
        )
    )
    channel = await session.get(ChannelAccount, chat.channel_account_id)
    email = channel is not None and channel.type == ChannelType.EMAIL
    if payload.text is not None and not email and len(payload.text) > CHAT_TEXT_LIMIT:
        raise Unprocessable(f"消息最长 {CHAT_TEXT_LIMIT} 个字")
    if message is None:
        await check_agent_text(session, payload.text)
    # 回复了就是看过了：之前客户的消息不再算未读。
    if chat.assignee_id == principal.staff_id:
        chat.read_at = datetime.now(UTC)
    if channel is not None and channel.type in (ChannelType.WECOM_KF, ChannelType.EMAIL):
        return await _send_via_channel(ctx, session, principal, chat, message, payload)
    if message is None:
        now = datetime.now(UTC)
        content_type, content, text = _content(ctx, payload)
        message = Message(
            id=new_id(),
            tenant_id=chat.tenant_id,
            room_id=chat.room_id,
            channel_account_id=chat.channel_account_id,
            session_id=chat.id,
            direction=Direction.OUT,
            sender_type=SenderType.AGENT,
            sender_id=principal.staff_id,
            content_type=content_type,
            content=content,
            text_plain=text,
            client_msg_id=payload.client_msg_id,
            source=MessageSource.API,
            send_status=SendStatus.PENDING,
            sent_at=now,
        )
        session.add(message)
        touch_session(chat, message)
        copilot.inspect_agent(session, chat, message)
        await session.commit()
    elif message.send_status == SendStatus.SENT:
        return (await messages_out(session, [message]))[0]

    room = await session.get(Room, chat.room_id)
    assert room is not None
    # 分配时的拉人操作可能还没执行（OpenIM 短暂不可用）：先把这个 Room 的待执行操作执行完。
    await outbox.flush_rooms(ctx, chat.tenant_id, [room.id])
    im_type, im_content = im_payload(message)
    try:
        sent = await ctx.im.send_group_message(
            send_id=imids.staff_user(principal.tenant_code, principal.staff_id),
            group_id=room.im_group_id,
            content_type=im_type,
            content=im_content,
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


async def _send_via_channel(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    chat: ChatSession,
    message: Message | None,
    payload: SendMessageRequest,
) -> MessageOut:
    """外部渠道（微信客服、邮件）：写库后经发件箱投递给客户，成功后再镜像到服务群。

    微信客服先检查回复窗口；邮件按"Re: 原主题"回复客户最近的一封邮件（或指定的那封）。
    投递立即尝试；渠道暂时不可用时消息保持"发送中"，由发件箱稍后重试。
    """
    if message is not None and message.send_status == SendStatus.SENT:
        return (await messages_out(session, [message]))[0]
    channel = await session.get(ChannelAccount, chat.channel_account_id)
    email = channel is not None and channel.type == ChannelType.EMAIL
    if message is None or message.send_status == SendStatus.FAILED:
        if not email:
            window = await reply_window(session, chat.room_id, datetime.now(UTC))
            if not window.open:
                raise Conflict(window.reason or SEND_FAILED)
        if message is None:
            content_type, content, text = _content(ctx, payload)
            if email:
                content_type, content, text = await mail_delivery.outgoing(
                    session,
                    chat,
                    content_type=content_type,
                    content=content,
                    text=text,
                    subject=payload.subject,
                    reply_to=payload.reply_to,
                )
            message = Message(
                id=new_id(),
                tenant_id=chat.tenant_id,
                room_id=chat.room_id,
                channel_account_id=chat.channel_account_id,
                session_id=chat.id,
                direction=Direction.OUT,
                sender_type=SenderType.AGENT,
                sender_id=principal.staff_id,
                content_type=content_type,
                content=content,
                text_plain=text,
                client_msg_id=payload.client_msg_id,
                source=MessageSource.API,
                send_status=SendStatus.PENDING,
                sent_at=datetime.now(UTC),
            )
            session.add(message)
            touch_session(chat, message)
            copilot.inspect_agent(session, chat, message)
        else:
            message.send_status = SendStatus.PENDING
            message.send_error = None
        await session.flush()
        outbox.enqueue_channel_send(session, chat.room_id, message.id)
        await session.commit()
    await outbox.flush_rooms(ctx, chat.tenant_id, [chat.room_id])
    await session.refresh(message)
    if message.send_status == SendStatus.FAILED:
        raise Conflict(message.send_error or SEND_FAILED)
    return (await messages_out(session, [message]))[0]


def _content(
    ctx: AppContext, payload: SendMessageRequest
) -> tuple[str, dict[str, Any], str | None]:
    """消息在平台里保存的类型、内容和纯文本（与回调入库的格式一致，见 conversation/ingest.py）。"""
    if payload.type == "text":
        assert payload.text is not None
        content: dict[str, Any] = {"text": payload.text}
        if payload.origin != "manual":
            content["origin"] = payload.origin
        return "text", content, payload.text
    attachment = payload.attachment
    assert attachment is not None
    prefix = f"{ctx.settings.public_api_url.rstrip('/')}/api/v1/files/"
    if not attachment.url.startswith(prefix):
        raise Unprocessable("附件必须先通过上传接口上传")
    if payload.type == "image" and attachment.content_type not in IMAGE_TYPES:
        raise Unprocessable("不支持的图片类型")
    return (
        payload.type,
        {
            "url": attachment.url,
            "name": attachment.name,
            "size": attachment.size,
            "width": attachment.width,
            "height": attachment.height,
            "mime": attachment.content_type,
        },
        None,
    )
