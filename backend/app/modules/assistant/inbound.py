"""收到 IM 消息后的处理（设计文档 §27.3.2、§27.3.4、§27.4），在实时消费进程里执行。

私聊：记下这个账号 → 处理"绑定 123456" → 未绑定的回复绑定说明 → 已绑定的交给引擎回答。
群聊：登记群、记录消息；默认不说话；设置为"被 @ 时回答"且提问人已绑定时才回答。
企业微信的成员按 wecom_userid 自动对应到员工。
"""

import contextlib
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.events.bus import Event
from app.integrations.imbots import InboundMessage, SendError
from app.modules.assistant import engine, sender, service
from app.modules.assistant import settings as assistant_settings
from app.modules.assistant.models import (
    AssistantBot,
    AssistantGroup,
    AssistantGroupMessage,
    AssistantIdentity,
    BotStatus,
    ReplyMode,
)
from app.modules.iam.models import Staff, StaffStatus

logger = logging.getLogger(__name__)

_BIND = re.compile(r"^\s*(?:绑定|bind)\s*[:：]?\s*(\d{6})\s*$", re.IGNORECASE)
BIND_HINT = (
    "你好，我是{name}。请先绑定员工账号：在控制台「AI 助理 → 我的绑定」获取 6 位绑定码，"
    "然后在这里回复「绑定 123456」。"
)
BOUND = (
    "绑定成功，{staff} 你好！可以直接问我：我的待办、某个订单的进度、某位客户的情况，"
    "或者让我记一件事。"
)
BAD_CODE = "绑定码不对或已过期，请在控制台「AI 助理 → 我的绑定」重新获取。"


def utcnow() -> datetime:
    return datetime.now(UTC)


def decode(data: dict[str, Any]) -> InboundMessage:
    return InboundMessage(
        external_message_id=str(data.get("external_message_id") or ""),
        chat_type="group" if data.get("chat_type") == "group" else "private",
        chat_id=str(data.get("chat_id") or ""),
        chat_name=str(data.get("chat_name") or ""),
        sender_id=str(data.get("sender_id") or ""),
        sender_name=str(data.get("sender_name") or ""),
        text=str(data.get("text") or ""),
        sent_at=datetime.fromisoformat(str(data.get("sent_at")))
        if data.get("sent_at")
        else utcnow(),
        mentioned=bool(data.get("mentioned")),
        reply_to={str(k): str(v) for k, v in (data.get("reply_to") or {}).items()},
    )


def encode(message: InboundMessage) -> dict[str, Any]:
    return {
        "external_message_id": message.external_message_id,
        "chat_type": message.chat_type,
        "chat_id": message.chat_id,
        "chat_name": message.chat_name,
        "sender_id": message.sender_id,
        "sender_name": message.sender_name,
        "text": message.text,
        "sent_at": message.sent_at.isoformat(),
        "mentioned": message.mentioned,
        "reply_to": dict(message.reply_to),
    }


async def _identity(
    session: AsyncSession, bot: AssistantBot, message: InboundMessage, now: datetime
) -> AssistantIdentity:
    identity = await session.scalar(
        select(AssistantIdentity).where(
            AssistantIdentity.bot_id == bot.id,
            AssistantIdentity.external_user_id == message.sender_id,
        )
    )
    if identity is None:
        identity = AssistantIdentity(
            tenant_id=bot.tenant_id,
            bot_id=bot.id,
            external_user_id=message.sender_id,
            display_name=message.sender_name[:128],
        )
        session.add(identity)
    if message.sender_name:
        identity.display_name = message.sender_name[:128]
    if message.chat_type == "private" and message.chat_id:
        identity.chat_id = message.chat_id[:128]
    identity.last_seen_at = now
    if identity.staff_id is None and bot.provider == "wecom":
        # 企业微信：按已绑定的成员自动对应。
        staff_id = await session.scalar(
            select(Staff.id).where(
                Staff.wecom_userid == message.sender_id, Staff.status == StaffStatus.ACTIVE
            )
        )
        if staff_id is not None:
            identity.staff_id, identity.bound_at = staff_id, now
    elif identity.staff_id is not None:
        active = await session.scalar(
            select(Staff.id).where(
                Staff.id == identity.staff_id, Staff.status == StaffStatus.ACTIVE
            )
        )
        if active is None:
            identity.staff_id, identity.bound_at = None, None
    await session.flush()
    return identity


async def _reply(
    ctx: AppContext,
    session: AsyncSession,
    bot: AssistantBot,
    message: InboundMessage,
    text: str,
) -> None:
    with contextlib.suppress(SendError):
        await sender.send(
            ctx,
            session,
            bot,
            kind="chat",
            target=message.chat_id,
            text=text,
            reply_to=message.reply_to or None,
        )


async def _private(
    ctx: AppContext, bot: AssistantBot, message: InboundMessage, now: datetime
) -> None:
    tenant_id = bot.tenant_id
    async with ctx.db.tenant_session(tenant_id) as session:
        settings = await assistant_settings.load(session, tenant_id)
        identity = await _identity(session, bot, message, now)
        match = _BIND.match(message.text)
        if match:
            staff_id = await service.redeem_binding_code(ctx, tenant_id, match.group(1))
            if staff_id is None:
                text = BAD_CODE
            else:
                await service.bind_identity(session, identity, staff_id, now=now)
                name = await session.scalar(select(Staff.display_name).where(Staff.id == staff_id))
                text = BOUND.format(staff=name or "")
            await session.commit()
            await _reply(ctx, session, bot, message, text)
            await session.commit()
            return
        if identity.staff_id is None:
            await session.commit()
            await _reply(ctx, session, bot, message, BIND_HINT.format(name=settings.name))
            await session.commit()
            return
        staff_id = identity.staff_id
        await session.commit()
    reply = await engine.answer(ctx, tenant_id, staff_id, message.text, bot_id=bot.id, now=now)
    async with ctx.db.tenant_session(tenant_id) as session:
        bot = await service.get_bot(session, bot.id)
        await _reply(ctx, session, bot, message, reply.text)
        await session.commit()


async def _group(
    ctx: AppContext, bot: AssistantBot, message: InboundMessage, now: datetime
) -> None:
    tenant_id = bot.tenant_id
    staff_id: uuid.UUID | None = None
    answer = False
    async with ctx.db.tenant_session(tenant_id) as session:
        settings = await assistant_settings.load(session, tenant_id)
        group = await session.scalar(
            select(AssistantGroup).where(
                AssistantGroup.bot_id == bot.id, AssistantGroup.external_chat_id == message.chat_id
            )
        )
        if group is None:
            group = AssistantGroup(
                tenant_id=tenant_id,
                bot_id=bot.id,
                external_chat_id=message.chat_id[:128],
                name=message.chat_name[:128],
            )
            session.add(group)
            await session.flush()
        elif message.chat_name and not group.name:
            group.name = message.chat_name[:128]
        identity = await _identity(session, bot, message, now)
        staff_id = identity.staff_id
        if group.recording:
            exists = await session.scalar(
                select(AssistantGroupMessage.id).where(
                    AssistantGroupMessage.group_id == group.id,
                    AssistantGroupMessage.external_message_id == message.external_message_id,
                )
            )
            if exists is None:
                session.add(
                    AssistantGroupMessage(
                        tenant_id=tenant_id,
                        group_id=group.id,
                        external_message_id=message.external_message_id[:128] or uuid.uuid4().hex,
                        sender_external_id=message.sender_id[:128],
                        sender_name=message.sender_name[:128],
                        staff_id=staff_id,
                        text=message.text[:10000],
                        sent_at=message.sent_at,
                    )
                )
                group.message_count += 1
                group.last_message_at = message.sent_at
        mode = group.reply_mode or settings.group_reply_mode
        answer = mode == ReplyMode.MENTIONED and message.mentioned and staff_id is not None
        await session.commit()
    if not answer or staff_id is None:
        return
    reply = await engine.answer(ctx, tenant_id, staff_id, message.text, bot_id=bot.id, now=now)
    async with ctx.db.tenant_session(tenant_id) as session:
        bot = await service.get_bot(session, bot.id)
        await _reply(ctx, session, bot, message, reply.text)
        await session.commit()


async def on_inbound(ctx: AppContext, event: Event) -> None:
    bot_id = uuid.UUID(str(event.data.get("bot_id")))
    message = decode(event.data.get("message") or {})
    now = utcnow()
    async with ctx.db.tenant_session(event.tenant_id) as session:
        bot = await session.get(AssistantBot, bot_id)
        if bot is None or bot.status != BotStatus.ACTIVE:
            return
        bot.last_received_at = now
        await session.commit()
        session.expunge(bot)
    if not message.sender_id or not message.text.strip():
        return
    if message.chat_type == "group":
        await _group(ctx, bot, message, now)
    else:
        await _private(ctx, bot, message, now)
