"""收信（设计文档 §10.8）。

- 调度进程每 10 秒找到期的邮箱（poll_due），每个邮箱默认 60 秒收一次；"立即收取"直接调用 fetch。
  同一个邮箱同时只有一个收取者（Redis 锁）。
- IMAP 只读打开收件箱，按 UID 增量收取；添加邮箱时（或 UIDVALIDITY 变化时）只记下当前位置。
- 外部调用（IMAP、对象存储）在数据库事务之外；邮件入库和收信位置在同一个事务里提交。
- 每封邮件一条 email 消息，附件各一条图片或文件消息跟在后面；入库后镜像到服务群，发布
  message.received，由会话引擎归入会话（邮件会话直接排队，见 sessions/engine.py）。
- 失败：退避重试；登录失败连续 3 次暂停收信，给有设置权限的员工发站内信。
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.ids import new_id
from app.core.permissions import Permission
from app.integrations import mail as transport
from app.integrations.storage import StorageError
from app.modules.channels.models import ChannelType
from app.modules.conversation import imids, outbox
from app.modules.conversation.ingest import message_received
from app.modules.conversation.models import Direction, Message, MessageSource, Room, SenderType
from app.modules.customer import sensitive
from app.modules.customer.models import Customer, CustomerIdentity
from app.modules.files.service import IMAGE_TYPES, file_url, safe_filename
from app.modules.mail import parse
from app.modules.mail.models import MailAccount, MailStatus
from app.modules.notifications import service as notifications
from app.modules.tenancy.models import Tenant, TenantStatus

logger = logging.getLogger(__name__)

DUE_BATCH = 20
PARALLEL = 4
# 一次最多收这么多封，其余的稍后接着收。
PER_POLL = 30
AGAIN = timedelta(seconds=5)
PAUSE_AFTER = 3
BACKOFF = (60, 120, 300, 900, 1800)
LOCK_TTL = 600
QUOTED_KEEP = 5000

_IMAGE_MAGIC = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Snapshot:
    id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_code: str
    channel_account_id: uuid.UUID
    address: str
    server: transport.Server
    uidvalidity: int | None
    last_uid: int | None
    ignore: list[str]


@dataclass
class _Pulled:
    uidvalidity: int
    last_uid: int
    mails: list[transport.Fetched]
    more: bool


@dataclass
class FetchResult:
    imported: int = 0
    ignored: int = 0
    error: str | None = None
    # 登录失败（授权码错误等）：连续几次后暂停收信。
    auth: bool = False
    busy: bool = False


@dataclass
class _Stored:
    """一封邮件存到对象存储之后的内容（入库前）。"""

    uid: int
    parsed: parse.Parsed | None
    raw_size: int
    eml_url: str | None
    received_at: datetime
    attachments: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    truncated: bool = False


def _lock(account_id: uuid.UUID) -> str:
    return f"edp:mail:fetch:{account_id}"


async def poll_due(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度进程：收取到期的邮箱（正常收信、租户在用的），返回导入的邮件数。"""
    now = now or utcnow()
    async with ctx.db.platform_sessionmaker() as session:
        rows = (
            await session.execute(
                select(MailAccount.tenant_id, MailAccount.id)
                .join(Tenant, Tenant.id == MailAccount.tenant_id)
                .where(
                    MailAccount.status == MailStatus.ACTIVE,
                    MailAccount.next_poll_at <= now,
                    Tenant.status == TenantStatus.ACTIVE,
                )
                .order_by(MailAccount.next_poll_at)
                .limit(DUE_BATCH)
            )
        ).all()
    limit = asyncio.Semaphore(PARALLEL)

    async def one(tenant_id: uuid.UUID, account_id: uuid.UUID) -> int:
        async with limit:
            try:
                return (await fetch(ctx, tenant_id, account_id)).imported
            except Exception:
                logger.exception("mail fetch for %s failed", account_id)
                return 0

    return sum(await asyncio.gather(*(one(t, a) for t, a in rows)))


async def _snapshot(
    ctx: AppContext, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> _Snapshot | None:
    async with ctx.db.tenant_session(tenant_id) as session:
        account = await session.get(MailAccount, account_id)
        tenant = await session.get(Tenant, tenant_id)
        if account is None or tenant is None or account.status == MailStatus.DISABLED:
            return None
        secret = await ctx.keys.unseal(tenant_id, account.secret_enc)
        return _Snapshot(
            id=account.id,
            tenant_id=tenant_id,
            tenant_code=tenant.code,
            channel_account_id=account.channel_account_id,
            address=account.address,
            server=transport.Server(
                account.imap_host,
                account.imap_port,
                account.imap_security,
                account.username,
                secret,
            ),
            uidvalidity=account.uidvalidity,
            last_uid=account.last_uid,
            ignore=list(account.ignore_senders or []),
        )


def _pull(ctx: AppContext, snap: _Snapshot) -> _Pulled:
    """（在线程里执行）打开收件箱，收取上次位置之后的邮件。"""
    settings = ctx.settings
    with transport.ImapSession(
        snap.server,
        timeout=settings.mail_timeout_seconds,
        allow_private=settings.mail_allow_private_hosts,
    ) as imap:
        inbox = imap.open_inbox()
        if snap.last_uid is None or snap.uidvalidity != inbox.uidvalidity:
            # 第一次收信，或者收件箱被重建：从当前位置开始，以前的邮件不导入。
            return _Pulled(inbox.uidvalidity, imap.last_uid(inbox), [], False)
        uids = imap.uids_after(snap.last_uid)
        batch = uids[:PER_POLL]
        mails = imap.fetch(batch, max_bytes=settings.mail_max_message_bytes)
        last = max(batch, default=snap.last_uid)
        return _Pulled(inbox.uidvalidity, last, mails, len(uids) > len(batch))


async def fetch(
    ctx: AppContext, tenant_id: uuid.UUID, account_id: uuid.UUID, *, manual: bool = False
) -> FetchResult:
    """收取一个邮箱的新邮件。manual：管理员点了"立即收取"（暂停的邮箱也收，成功后恢复）。"""
    lock = _lock(account_id)
    if not await ctx.redis.set(lock, "1", nx=True, ex=LOCK_TTL):
        return FetchResult(busy=True, error="正在收取，请稍后再看")
    try:
        snap = await _snapshot(ctx, tenant_id, account_id)
        if snap is None:
            return FetchResult(error="邮箱已停用")
        try:
            pulled = await asyncio.to_thread(_pull, ctx, snap)
            stored = await _store_all(ctx, snap, pulled)
        except transport.MailError as exc:
            return await _failed(ctx, snap, exc.message, auth=exc.kind == "auth")
        except (transport.MailUnavailable, StorageError) as exc:
            return await _failed(ctx, snap, str(exc), auth=False)
        return await _ingest(ctx, snap, pulled, stored, manual=manual)
    finally:
        await ctx.redis.delete(lock)


async def _failed(ctx: AppContext, snap: _Snapshot, error: str, *, auth: bool) -> FetchResult:
    now = utcnow()
    async with ctx.db.tenant_session(snap.tenant_id) as session:
        account = await session.get(MailAccount, snap.id, with_for_update=True)
        if account is None:
            return FetchResult(error=error, auth=auth)
        account.failures += 1
        account.last_error = error[:1000]
        account.last_polled_at = now
        delay = BACKOFF[min(account.failures, len(BACKOFF)) - 1]
        account.next_poll_at = now + timedelta(seconds=delay)
        if auth and account.failures >= PAUSE_AFTER and account.status == MailStatus.ACTIVE:
            account.status = MailStatus.PAUSED
            await _notify_paused(session, account, error)
        await session.commit()
    logger.warning("mail fetch for %s failed: %s", snap.address, error)
    return FetchResult(error=error, auth=auth)


async def _notify_paused(session: AsyncSession, account: MailAccount, error: str) -> None:
    # 延迟导入：知识模块的人员范围查询依赖较多模块。
    from app.modules.kb.distribution import audience

    admins = await audience(session, Permission.SETTINGS_MANAGE)
    notifications.add(
        session,
        account.tenant_id,
        [s.id for s in admins],
        kind="mail_paused",
        title=f"邮箱 {account.address} 已暂停收信",
        body=f"连续 {account.failures} 次登录失败：{error}。请在「设置 → 邮箱」里检查授权码，"
        "修改后或点「立即收取」成功后恢复收信。",
        link="/settings?tab=mail",
    )


# ---- 存储 ----


def _sniff_image(mime: str, data: bytes) -> bool:
    return mime in IMAGE_TYPES and any(data.startswith(m) for m in _IMAGE_MAGIC.get(mime, ()))


async def _put(ctx: AppContext, snap: _Snapshot, name: str, data: bytes, mime: str) -> str:
    key = f"{snap.tenant_code}/email/{utcnow():%Y/%m}/{uuid.uuid4().hex}/{safe_filename(name)}"
    await ctx.storage.put(key, data, mime)
    return file_url(ctx.settings, key)


async def _store_one(ctx: AppContext, snap: _Snapshot, mail: transport.Fetched) -> _Stored:
    received = mail.received_at or utcnow()
    if mail.raw is None:
        return _Stored(mail.uid, None, mail.size, None, received, truncated=mail.truncated)
    parsed = await asyncio.to_thread(parse.parse, mail.raw)
    stored = _Stored(mail.uid, parsed, mail.size, None, received, truncated=mail.truncated)
    if mail.truncated or parse.skip_reason(parsed, snap.address, snap.ignore) is not None:
        # 太大的邮件只取了头部：不存原文和附件，会话里提示到邮箱查看。
        return stored
    stored.eml_url = await _put(ctx, snap, "原邮件.eml", mail.raw, "message/rfc822")
    limit = ctx.settings.mail_max_attachment_bytes
    for part in parsed.attachments:
        if len(part.data) > limit:
            stored.skipped.append(f"{part.name}（{len(part.data) / 1024 / 1024:.1f} MB）")
            continue
        image = _sniff_image(part.mime, part.data)
        name = part.name
        mime = part.mime if "/" in part.mime else "application/octet-stream"
        if not image and name.lower().endswith(_IMAGE_SUFFIXES):
            # 不是真正的图片却用图片的扩展名：下载时会按图片直接打开，改个扩展名。
            name = f"{name}.bin"
        url = await _put(ctx, snap, name, part.data, mime)
        body = {"url": url, "name": name[:200], "size": len(part.data), "mime": mime}
        if image:
            body |= {"width": None, "height": None}
        stored.attachments.append(("image" if image else "file", body))
    return stored


async def _store_all(ctx: AppContext, snap: _Snapshot, pulled: _Pulled) -> list[_Stored]:
    return [await _store_one(ctx, snap, mail) for mail in pulled.mails]


# ---- 入库 ----


async def _identity_room(
    ctx: AppContext, session: AsyncSession, snap: _Snapshot, sender: parse.Address
) -> tuple[CustomerIdentity, Room]:
    """按发件人地址找身份：这个邮箱里已有的身份 → 档案里邮箱相同的客户 → 新建客户。"""
    key = await sensitive.email_index(ctx.keys, snap.tenant_id, sender.address)
    identity = await session.scalar(
        select(CustomerIdentity).where(
            CustomerIdentity.channel_account_id == snap.channel_account_id,
            CustomerIdentity.external_id == key,
        )
    )
    if identity is None:
        customer = await session.scalar(
            select(Customer)
            .where(Customer.email_hash == key)
            .order_by(Customer.created_at)
            .limit(1)
        )
        if customer is None:
            name = sender.name or sender.address.partition("@")[0]
            customer = Customer(
                id=new_id(),
                tenant_id=snap.tenant_id,
                display_name=name[:128] or sender.address,
                source_channel=ChannelType.EMAIL,
            )
            await sensitive.set_email(ctx.keys, customer, sender.address)
            session.add(customer)
            await session.flush()
        identity_id = new_id()
        identity = CustomerIdentity(
            id=identity_id,
            tenant_id=snap.tenant_id,
            customer_id=customer.id,
            channel_account_id=snap.channel_account_id,
            external_id=key,
            im_user_id=imids.customer_user(snap.tenant_code, identity_id),
            profile={
                "name": sender.name,
                "address_enc": await ctx.keys.seal(snap.tenant_id, sender.address),
            },
        )
        session.add(identity)
        await session.flush()
    room = await session.scalar(select(Room).where(Room.identity_id == identity.id))
    if room is None:
        room_id = new_id()
        room = Room(
            id=room_id,
            tenant_id=snap.tenant_id,
            customer_id=identity.customer_id,
            identity_id=identity.id,
            channel_account_id=snap.channel_account_id,
            im_group_id=imids.room_group(snap.tenant_code, room_id),
        )
        session.add(room)
        await session.flush()
    return identity, room


def _email_content(snap: _Snapshot, item: _Stored, max_bytes: int) -> tuple[dict[str, Any], str]:
    parsed = item.parsed
    assert parsed is not None and parsed.sender is not None
    text = parsed.text
    if item.truncated:
        text = (
            f"（邮件有 {item.raw_size / 1024 / 1024:.1f} MB，超过 {max_bytes // 1024 // 1024} MB，"
            "没有导入正文和附件，请到邮箱中查看）"
        )
    if item.skipped:
        text += (
            "\n\n（附件超过大小上限，没有导入：" + "、".join(item.skipped) + "，请到邮箱中查看）"
        )
    content: dict[str, Any] = {
        "subject": parsed.subject,
        "from": parsed.sender.as_dict(),
        "to": [a.as_dict() for a in parsed.to],
        "cc": [a.as_dict() for a in parsed.cc],
        "reply_to": parsed.reply_to.as_dict() if parsed.reply_to else None,
        "text": text,
        "quoted": parsed.quoted[:QUOTED_KEEP],
        "html": parsed.html,
        "message_id": parsed.message_id,
        "in_reply_to": parsed.in_reply_to,
        "references": parsed.references,
        "attachments": len(item.attachments),
        "mailbox": snap.address,
        "url": item.eml_url,
        "name": "原邮件.eml",
        "size": item.raw_size,
        "mime": "message/rfc822",
    }
    plain = f"{parsed.subject}\n{text}".strip() if parsed.subject else text
    return content, plain[:4000]


async def _insert(
    session: AsyncSession,
    room: Room,
    identity: CustomerIdentity,
    *,
    ext_msg_id: str,
    content_type: str,
    content: dict[str, Any],
    text: str | None,
    sent_at: datetime,
) -> uuid.UUID | None:
    stmt = (
        insert(Message)
        .values(
            id=new_id(),
            tenant_id=room.tenant_id,
            room_id=room.id,
            channel_account_id=room.channel_account_id,
            direction=Direction.IN,
            sender_type=SenderType.CUSTOMER,
            sender_id=identity.id,
            content_type=content_type,
            content=content,
            text_plain=text,
            ext_msg_id=ext_msg_id[:128],
            source=MessageSource.CHANNEL,
            sent_at=sent_at,
        )
        .on_conflict_do_nothing(constraint="uq_messages_ext_msg")
        .returning(Message.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _ingest(
    ctx: AppContext, snap: _Snapshot, pulled: _Pulled, stored: list[_Stored], *, manual: bool
) -> FetchResult:
    now = utcnow()
    result = FetchResult()
    created: list[tuple[uuid.UUID, uuid.UUID]] = []  # (room_id, message_id)
    rooms: set[uuid.UUID] = set()
    async with ctx.db.tenant_session(snap.tenant_id) as session:
        account = await session.get(MailAccount, snap.id, with_for_update=True)
        if account is None or account.status == MailStatus.DISABLED:
            return FetchResult(error="邮箱已停用")
        if account.uidvalidity == pulled.uidvalidity and (account.last_uid or 0) > pulled.last_uid:
            # 另一个收取者已经往前收过了（锁过期等）：不重复入库。
            return FetchResult()
        ids = [_ext_id(snap, pulled, item) for item in stored]
        known = set(
            (
                await session.scalars(
                    select(Message.ext_msg_id).where(
                        Message.channel_account_id == snap.channel_account_id,
                        Message.ext_msg_id.in_(ids),
                    )
                )
            ).all()
        )
        for item in stored:
            if item.parsed is None:
                # 服务器没有返回内容。
                result.ignored += 1
                continue
            if parse.skip_reason(item.parsed, snap.address, snap.ignore) is not None:
                result.ignored += 1
                continue
            ext_id = _ext_id(snap, pulled, item)
            if ext_id in known:
                continue
            known.add(ext_id)
            sender = item.parsed.sender
            assert sender is not None
            identity, room = await _identity_room(ctx, session, snap, sender)
            sent_at = min(item.received_at, now)
            content, text = _email_content(snap, item, ctx.settings.mail_max_message_bytes)
            message_id = await _insert(
                session,
                room,
                identity,
                ext_msg_id=ext_id,
                content_type="email",
                content=content,
                text=text,
                sent_at=sent_at,
            )
            if message_id is None:
                continue
            created.append((room.id, message_id))
            outbox.enqueue_mirror(session, room.id, message_id)
            for index, (kind, body) in enumerate(item.attachments, start=1):
                attachment_id = await _insert(
                    session,
                    room,
                    identity,
                    ext_msg_id=f"{ext_id}#{index}",
                    content_type=kind,
                    content={**body, "email_id": str(message_id)},
                    text=None,
                    # 附件排在邮件后面（工作台按毫秒排序）。
                    sent_at=sent_at + timedelta(milliseconds=index),
                )
                if attachment_id is not None:
                    created.append((room.id, attachment_id))
                    outbox.enqueue_mirror(session, room.id, attachment_id)
            room.last_message_at = max(room.last_message_at or sent_at, sent_at)
            room.last_active_at = now
            identity.last_seen_at = sent_at
            rooms.add(room.id)
            result.imported += 1
        account.uidvalidity = pulled.uidvalidity
        account.last_uid = pulled.last_uid
        account.last_polled_at = now
        if result.imported:
            account.last_received_at = now
        account.ignored += result.ignored
        account.failures = 0
        account.last_error = None
        if manual and account.status == MailStatus.PAUSED:
            account.status = MailStatus.ACTIVE
        interval = timedelta(seconds=ctx.settings.mail_poll_seconds)
        account.next_poll_at = now + (AGAIN if pulled.more else interval)
        await session.commit()
    for room_id, message_id in created:
        try:
            await ctx.bus.publish(message_received(snap.tenant_id, room_id, message_id))
        except Exception:
            # 邮件已经入库；没有归入会话的消息会被调度进程重新发布。
            logger.exception("publishing message.received for %s failed", message_id)
    await outbox.flush_rooms(ctx, snap.tenant_id, rooms)
    return result


def _ext_id(snap: _Snapshot, pulled: _Pulled, item: _Stored) -> str:
    """去重用的标识：Message-ID；没有时用收件箱位置。"""
    if item.parsed is not None and item.parsed.message_id:
        return item.parsed.message_id
    return f"<uid.{pulled.uidvalidity}.{item.uid}@{snap.id.hex}>"
