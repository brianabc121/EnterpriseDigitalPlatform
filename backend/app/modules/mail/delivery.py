"""回复邮件（设计文档 §10.8）：坐席在工作台回复，经发件箱投递（conversation/outbox.py）。

- 默认回复会话里最近一封客户邮件，也可以指定回复哪一封（reply_to）；主题"Re: 原主题"，
  In-Reply-To、References 指向原邮件，发到原邮件的 Reply-To（没有时发件人）。
- 文字是一封邮件；图片、文件是一封带这个附件的邮件。
- 网络问题抛出 MailUnavailable，由发件箱稍后重试；邮箱拒绝（授权码错误、收件人被拒）时把
  消息标记为失败，坐席工作台显示原因，可以重试。
"""

import asyncio
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, Unprocessable
from app.integrations import mail as transport
from app.modules.conversation.models import (
    ChatSession,
    Direction,
    Message,
    Room,
    SenderType,
    SendStatus,
)
from app.modules.customer.models import CustomerIdentity
from app.modules.files.service import key_of_url
from app.modules.mail import compose, parse
from app.modules.mail.models import MailAccount, MailStatus

ACCOUNT_DISABLED = "邮箱已停用，不能回复"
NO_ADDRESS = "不知道客户的邮箱地址"
TEXT_LIMIT = 20_000


async def latest_email(
    session: AsyncSession, room_id: uuid.UUID, message_id: uuid.UUID | None = None
) -> Message | None:
    """要回复的客户邮件：指定的那封，或者 Room 里最近的一封。"""
    query = select(Message).where(
        Message.room_id == room_id,
        Message.content_type == "email",
        Message.direction == Direction.IN,
    )
    if message_id is not None:
        query = query.where(Message.id == message_id)
    return await session.scalar(query.order_by(Message.sent_at.desc()).limit(1))


async def outgoing(
    session: AsyncSession,
    chat: ChatSession,
    *,
    content_type: str,
    content: dict[str, Any],
    text: str | None,
    subject: str | None,
    reply_to: uuid.UUID | None,
) -> tuple[str, dict[str, Any], str | None]:
    """坐席要发的消息在邮件会话里保存的类型和内容：文字是 email，图片、文件保持原类型。"""
    account = await session.scalar(
        select(MailAccount).where(MailAccount.channel_account_id == chat.channel_account_id)
    )
    if account is None or account.status == MailStatus.DISABLED:
        raise Conflict(ACCOUNT_DISABLED)
    original = await latest_email(session, chat.room_id, reply_to)
    if reply_to is not None and original is None:
        raise Unprocessable("要回复的邮件不存在")
    title = (subject or "").strip() or parse.reply_subject(
        str((original.content or {}).get("subject") or "") if original else ""
    )
    extra: dict[str, Any] = {"subject": title[:300]}
    if original is not None:
        extra["reply_message_id"] = str(original.id)
    if content_type == "text":
        if text is not None and len(text) > TEXT_LIMIT:
            raise Unprocessable(f"邮件正文最长 {TEXT_LIMIT} 个字")
        return "email", {**extra, "text": text or ""}, text
    return content_type, {**content, **extra}, text


def _fail(message: Message, reason: str) -> None:
    message.send_status = SendStatus.FAILED
    message.send_error = reason[:500]


def _original(email: Message | None) -> compose.Original | None:
    if email is None:
        return None
    content = email.content or {}
    sender = content.get("from") or {}
    return compose.Original(
        sender_name=str(sender.get("name") or ""),
        sender_address=str(sender.get("address") or ""),
        sent_at=email.sent_at,
        text=str(content.get("text") or ""),
    )


async def deliver(
    ctx: AppContext, session: AsyncSession, room: Room, message: Message, now: datetime
) -> bool:
    """把一条出站消息发成邮件（发件箱调用，已持有 Room 的锁）。成功返回 True。"""
    account = await session.scalar(
        select(MailAccount).where(MailAccount.channel_account_id == room.channel_account_id)
    )
    if account is None or account.status == MailStatus.DISABLED:
        _fail(message, ACCOUNT_DISABLED)
        return False
    if message.sender_type != SenderType.AGENT:
        # 系统提示和 AI 回复不发邮件（发件箱已经跳过，这里兜底）。
        _fail(message, "邮件渠道只发送坐席的回复")
        return False
    content = dict(message.content or {})
    target_id = content.get("reply_message_id")
    email = await latest_email(session, room.id, uuid.UUID(target_id) if target_id else None)
    target = email.content if email is not None else {}
    to_name, to_address = "", ""
    for candidate in (target.get("reply_to"), target.get("from")):
        if isinstance(candidate, dict) and candidate.get("address"):
            to_name = str(candidate.get("name") or "")
            to_address = str(candidate["address"])
            break
    if not to_address:
        identity = await session.get(CustomerIdentity, room.identity_id)
        sealed = (identity.profile or {}).get("address_enc") if identity else None
        if sealed:
            to_address = await ctx.keys.unseal(room.tenant_id, str(sealed))
            to_name = str((identity.profile or {}).get("name") or "") if identity else ""
    if not to_address:
        _fail(message, NO_ADDRESS)
        return False

    attachments: list[compose.Attachment] = []
    text = str(content.get("text") or message.text_plain or "")
    if message.content_type in ("image", "file") and content.get("url"):
        key = key_of_url(ctx.settings, str(content["url"]))
        if key is None:
            _fail(message, "附件不是平台签发的文件链接")
            return False
        data = await ctx.storage.get(key)
        name = str(content.get("name") or key.rsplit("/", 1)[-1])
        attachments.append(
            compose.Attachment(name, str(content.get("mime") or "application/octet-stream"), data)
        )
        text = text or f"请查收附件：{name}"

    in_reply_to = target.get("message_id") if isinstance(target.get("message_id"), str) else None
    references = [r for r in target.get("references") or [] if isinstance(r, str)]
    msg_id = compose.message_id(message.id.hex, account.address)
    subject = str(content.get("subject") or parse.reply_subject(str(target.get("subject") or "")))
    outgoing_mail = compose.compose(
        sender_name=account.display_name,
        sender_address=account.address,
        to_name=to_name,
        to_address=to_address,
        subject=subject,
        text=text,
        signature=account.signature,
        original=_original(email),
        msg_id=msg_id,
        in_reply_to=in_reply_to,
        references=references,
        attachments=attachments,
        now=now,
    )
    secret = await ctx.keys.unseal(room.tenant_id, account.secret_enc)
    server = transport.Server(
        account.smtp_host, account.smtp_port, account.smtp_security, account.username, secret
    )
    try:
        await asyncio.to_thread(
            transport.send,
            server,
            outgoing_mail,
            sender=account.address,
            recipients=[to_address],
            timeout=ctx.settings.mail_timeout_seconds,
            allow_private=ctx.settings.mail_allow_private_hosts,
        )
    except transport.MailError as exc:
        _fail(message, f"发信失败：{exc.message}")
        return False
    message.content = {
        **content,
        "subject": subject,
        "from": {"name": account.display_name, "address": account.address},
        "to": [{"name": to_name, "address": to_address}],
        "message_id": msg_id,
        "in_reply_to": in_reply_to,
    }
    message.ext_msg_id = msg_id
    message.send_status = SendStatus.SENT
    message.send_error = None
    message.sent_at = now
    return True
