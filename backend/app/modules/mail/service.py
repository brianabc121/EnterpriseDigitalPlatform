"""邮箱管理（设计文档 §10.8）：添加、修改（保存前测试连接）、停用和启用、立即收取、原邮件。"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.config import Settings
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.ids import new_id
from app.integrations import mail as transport
from app.integrations.storage import StorageError
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import check_limit
from app.modules.channels.models import ChannelAccount, ChannelStatus, ChannelType
from app.modules.channels.service import new_public_key
from app.modules.conversation.models import Message
from app.modules.customer import sensitive
from app.modules.files.service import key_of_url
from app.modules.iam.principal import Principal
from app.modules.mail import inbox, original, providers
from app.modules.mail.models import MailAccount, MailStatus
from app.modules.mail.schemas import (
    MailAccountIn,
    MailAccountList,
    MailAccountOut,
    MailFetchOut,
    MailOriginalOut,
    MailProviderList,
    MailProviderOut,
    MailServer,
    MailTestIn,
    MailTestOut,
)
from app.modules.sessions.service import visible_session

NOT_FOUND = "邮箱不存在"


def utcnow() -> datetime:
    return datetime.now(UTC)


def providers_out(settings: Settings) -> MailProviderList:
    return MailProviderList(
        items=[
            MailProviderOut(
                key=p.key,
                name=p.name,
                domains=list(p.domains),
                imap=MailServer(host=p.imap.host, port=p.imap.port, security=p.imap.security),
                smtp=MailServer(host=p.smtp.host, port=p.smtp.port, security=p.smtp.security),
                secret_label=p.secret_label,
                help=p.help,
            )
            for p in providers.PROVIDERS
        ],
        allow_insecure=settings.mail_allow_private_hosts,
    )


def _out(account: MailAccount, channel: ChannelAccount) -> MailAccountOut:
    return MailAccountOut(
        id=account.id,
        channel_account_id=account.channel_account_id,
        name=channel.name,
        address=account.address,
        display_name=account.display_name,
        provider=account.provider,
        username=account.username,
        imap=MailServer(
            host=account.imap_host,
            port=account.imap_port,
            security=account.imap_security,
        ),
        smtp=MailServer(
            host=account.smtp_host,
            port=account.smtp_port,
            security=account.smtp_security,
        ),
        signature=account.signature,
        ignore_senders=list(account.ignore_senders or []),
        status=account.status,
        last_polled_at=account.last_polled_at,
        last_received_at=account.last_received_at,
        next_poll_at=account.next_poll_at,
        failures=account.failures,
        last_error=account.last_error,
        ignored=account.ignored,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


async def list_accounts(session: AsyncSession) -> MailAccountList:
    rows = await session.execute(
        select(MailAccount, ChannelAccount)
        .join(ChannelAccount, ChannelAccount.id == MailAccount.channel_account_id)
        .order_by(MailAccount.created_at, MailAccount.id)
    )
    return MailAccountList(items=[_out(account, channel) for account, channel in rows])


async def _get(
    session: AsyncSession, account_id: uuid.UUID, *, lock: bool = False
) -> tuple[MailAccount, ChannelAccount]:
    account = await session.get(MailAccount, account_id, with_for_update=lock)
    if account is None:
        raise NotFound(NOT_FOUND)
    channel = await session.get(ChannelAccount, account.channel_account_id)
    if channel is None:
        raise NotFound(NOT_FOUND)
    return account, channel


async def _fresh(
    session: AsyncSession, account: MailAccount, channel: ChannelAccount
) -> MailAccountOut:
    """修改后重新读取（updated_at 由数据库更新）。"""
    await session.refresh(account)
    await session.refresh(channel)
    return _out(account, channel)


async def get_account(session: AsyncSession, account_id: uuid.UUID) -> MailAccountOut:
    return _out(*await _get(session, account_id))


# ---- 校验与测试 ----


@dataclass(frozen=True)
class _Settings:
    """邮箱的连接设置（校验过的）。"""

    address: str
    provider: str
    username: str
    imap: MailServer
    smtp: MailServer
    ignore: list[str]


def _validate(settings: Settings, payload: MailAccountIn) -> _Settings:
    address = sensitive.normalize_email(payload.address)
    if not sensitive.valid_email(address):
        raise Unprocessable("邮箱地址不正确")
    provider = providers.provider_of(payload.provider)
    if provider is None:
        raise Unprocessable("不支持的邮箱类型")
    imap = payload.imap or MailServer(
        host=provider.imap.host, port=provider.imap.port, security=provider.imap.security
    )
    smtp = payload.smtp or MailServer(
        host=provider.smtp.host, port=provider.smtp.port, security=provider.smtp.security
    )
    for label, server in (("收信", imap), ("发信", smtp)):
        if not server.host.strip():
            raise Unprocessable(f"请填写{label}服务器地址")
        if server.security == "none" and not settings.mail_allow_private_hosts:
            raise Unprocessable(f"{label}必须使用加密连接（SSL 或 STARTTLS）")
    ignore: list[str] = []
    for rule in payload.ignore_senders:
        rule = rule.strip().lower()
        if not rule:
            continue
        if "@" not in rule or len(rule) > 254:
            raise Unprocessable(f"忽略的发件人格式不正确：{rule}（填完整地址或 @域名）")
        if rule not in ignore:
            ignore.append(rule)
    return _Settings(
        address=address,
        provider=provider.key,
        username=(payload.username or "").strip() or address,
        imap=MailServer(host=imap.host.strip(), port=imap.port, security=imap.security),
        smtp=MailServer(host=smtp.host.strip(), port=smtp.port, security=smtp.security),
        ignore=ignore,
    )


@dataclass(frozen=True)
class _Probe:
    imap_error: str | None
    smtp_error: str | None
    inbox: int | None
    uidvalidity: int | None
    last_uid: int | None

    @property
    def ok(self) -> bool:
        return self.imap_error is None and self.smtp_error is None

    def message(self) -> str:
        parts = []
        if self.imap_error:
            parts.append(f"收信：{self.imap_error}")
        if self.smtp_error:
            parts.append(f"发信：{self.smtp_error}")
        return "；".join(parts)


def _server(server: MailServer, username: str, secret: str) -> transport.Server:
    return transport.Server(server.host, server.port, server.security, username, secret)


def _probe(ctx: AppContext, config: _Settings, secret: str) -> _Probe:
    """（在线程里执行）IMAP 登录并只读打开收件箱，SMTP 登录。"""
    timeout = ctx.settings.mail_timeout_seconds
    allow = ctx.settings.mail_allow_private_hosts
    imap_error = smtp_error = None
    count = validity = last = None
    try:
        with transport.ImapSession(
            _server(config.imap, config.username, secret), timeout=timeout, allow_private=allow
        ) as imap:
            box = imap.open_inbox()
            count, validity, last = box.exists, box.uidvalidity, imap.last_uid(box)
    except transport.MailError as exc:
        imap_error = exc.message
    except transport.MailUnavailable as exc:
        imap_error = str(exc)
    try:
        transport.check_smtp(
            _server(config.smtp, config.username, secret), timeout=timeout, allow_private=allow
        )
    except transport.MailError as exc:
        smtp_error = exc.message
    except transport.MailUnavailable as exc:
        smtp_error = str(exc)
    return _Probe(imap_error, smtp_error, count, validity, last)


async def test(ctx: AppContext, session: AsyncSession, payload: MailTestIn) -> MailTestOut:
    config = _validate(ctx.settings, payload)
    secret = (payload.secret or "").strip()
    if not secret and payload.account_id is not None:
        account, _ = await _get(session, payload.account_id)
        secret = await ctx.keys.unseal(account.tenant_id, account.secret_enc)
    if not secret:
        raise Unprocessable("请填写授权码")
    await session.commit()
    probe = await asyncio.to_thread(_probe, ctx, config, secret)
    return MailTestOut(
        ok=probe.ok, imap_error=probe.imap_error, smtp_error=probe.smtp_error, inbox=probe.inbox
    )


async def _unique(session: AsyncSession, address: str, exclude: uuid.UUID | None) -> None:
    query = select(MailAccount.id).where(func.lower(MailAccount.address) == address)
    if exclude is not None:
        query = query.where(MailAccount.id != exclude)
    if await session.scalar(query) is not None:
        raise Conflict("这个邮箱已经添加过了")


def _audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    account: MailAccount,
    detail: dict[str, object],
    ip: str | None,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="mail_account",
        resource_id=str(account.id),
        detail={"address": account.address, **detail},
        ip=ip,
    )


# ---- 添加、修改、停用 ----


async def create(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    payload: MailAccountIn,
    *,
    ip: str | None,
) -> MailAccountOut:
    config = _validate(ctx.settings, payload)
    secret = (payload.secret or "").strip()
    if not secret:
        raise Unprocessable("请填写授权码")
    await _unique(session, config.address, None)
    await check_limit(session, principal.tenant_id, "channels")
    await session.commit()
    probe = await asyncio.to_thread(_probe, ctx, config, secret)
    if not probe.ok:
        raise Unprocessable(f"连接邮箱失败：{probe.message()}")
    await _unique(session, config.address, None)
    now = utcnow()
    channel = ChannelAccount(
        id=new_id(),
        tenant_id=principal.tenant_id,
        type=ChannelType.EMAIL,
        name=payload.name.strip(),
        public_key=new_public_key(principal.tenant_code),
        config={"address": config.address},
    )
    session.add(channel)
    await session.flush()
    account = MailAccount(
        id=new_id(),
        tenant_id=principal.tenant_id,
        channel_account_id=channel.id,
        address=config.address,
        display_name=payload.display_name.strip(),
        provider=config.provider,
        imap_host=config.imap.host,
        imap_port=config.imap.port,
        imap_security=config.imap.security,
        smtp_host=config.smtp.host,
        smtp_port=config.smtp.port,
        smtp_security=config.smtp.security,
        username=config.username,
        secret_enc=await ctx.keys.seal(principal.tenant_id, secret),
        signature=(payload.signature or "").strip() or None,
        ignore_senders=config.ignore,
        status=MailStatus.ACTIVE,
        # 添加时记下收件箱的当前位置：之后收到的新邮件才导入。
        uidvalidity=probe.uidvalidity,
        last_uid=probe.last_uid,
        next_poll_at=now + timedelta(seconds=ctx.settings.mail_poll_seconds),
        last_polled_at=now,
        created_by=principal.staff_id,
    )
    session.add(account)
    _audit(session, principal, "mail.create", account, {"provider": config.provider}, ip)
    await session.commit()
    return _out(account, channel)


async def update(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    account_id: uuid.UUID,
    payload: MailAccountIn,
    *,
    ip: str | None,
) -> MailAccountOut:
    account, channel = await _get(session, account_id)
    config = _validate(ctx.settings, payload)
    secret = (payload.secret or "").strip()
    connection_changed = bool(secret) or (
        config.address,
        config.provider,
        config.username,
        config.imap.host,
        config.imap.port,
        config.imap.security,
        config.smtp.host,
        config.smtp.port,
        config.smtp.security,
    ) != (
        account.address,
        account.provider,
        account.username,
        account.imap_host,
        account.imap_port,
        account.imap_security,
        account.smtp_host,
        account.smtp_port,
        account.smtp_security,
    )
    if config.address != account.address:
        await _unique(session, config.address, account.id)
    probe: _Probe | None = None
    if connection_changed:
        test_secret = secret or await ctx.keys.unseal(account.tenant_id, account.secret_enc)
        await session.commit()
        probe = await asyncio.to_thread(_probe, ctx, config, test_secret)
        if not probe.ok:
            raise Unprocessable(f"连接邮箱失败：{probe.message()}")
    account, channel = await _get(session, account_id, lock=True)
    changed = [
        field
        for field, before, after in (
            ("address", account.address, config.address),
            ("provider", account.provider, config.provider),
            ("username", account.username, config.username),
            (
                "servers",
                (account.imap_host, account.smtp_host),
                (config.imap.host, config.smtp.host),
            ),
            ("display_name", account.display_name, payload.display_name.strip()),
            ("signature", account.signature, (payload.signature or "").strip() or None),
            ("ignore_senders", list(account.ignore_senders or []), config.ignore),
            ("name", channel.name, payload.name.strip()),
        )
        if before != after
    ]
    if secret:
        changed.append("secret")
        account.secret_enc = await ctx.keys.seal(account.tenant_id, secret)
    if probe is not None and (
        config.address != account.address or probe.uidvalidity != account.uidvalidity
    ):
        # 换了邮箱：从新邮箱的当前位置开始收。
        account.uidvalidity, account.last_uid = probe.uidvalidity, probe.last_uid
    account.address = config.address
    account.provider = config.provider
    account.username = config.username
    account.imap_host, account.imap_port = config.imap.host, config.imap.port
    account.imap_security = config.imap.security
    account.smtp_host, account.smtp_port = config.smtp.host, config.smtp.port
    account.smtp_security = config.smtp.security
    account.display_name = payload.display_name.strip()
    account.signature = (payload.signature or "").strip() or None
    account.ignore_senders = config.ignore
    channel.name = payload.name.strip()
    channel.config = {**(channel.config or {}), "address": config.address}
    if probe is not None and account.status == MailStatus.PAUSED:
        # 连接测试通过：恢复收信。
        account.status = MailStatus.ACTIVE
        account.failures = 0
        account.last_error = None
        account.next_poll_at = utcnow()
    _audit(session, principal, "mail.update", account, {"changed": changed}, ip)
    await session.commit()
    return await _fresh(session, account, channel)


async def set_enabled(
    session: AsyncSession,
    principal: Principal,
    account_id: uuid.UUID,
    *,
    enabled: bool,
    ip: str | None,
) -> MailAccountOut:
    account, channel = await _get(session, account_id, lock=True)
    if enabled:
        if channel.status != ChannelStatus.ACTIVE:
            await check_limit(session, principal.tenant_id, "channels")
        account.status = MailStatus.ACTIVE
        account.failures = 0
        account.last_error = None
        account.next_poll_at = utcnow()
        channel.status = ChannelStatus.ACTIVE
    else:
        account.status = MailStatus.DISABLED
        channel.status = ChannelStatus.DISABLED
    _audit(session, principal, "mail.enable" if enabled else "mail.disable", account, {}, ip)
    await session.commit()
    return await _fresh(session, account, channel)


async def sync_channel_status(session: AsyncSession, channel: ChannelAccount) -> None:
    """在"接入渠道"里停用或启用邮件渠道时，邮箱一起停用或启用。"""
    account = await session.scalar(
        select(MailAccount).where(MailAccount.channel_account_id == channel.id)
    )
    if account is None:
        return
    if channel.status == ChannelStatus.ACTIVE and account.status == MailStatus.DISABLED:
        account.status = MailStatus.ACTIVE
        account.failures = 0
        account.next_poll_at = utcnow()
    elif channel.status != ChannelStatus.ACTIVE:
        account.status = MailStatus.DISABLED


async def fetch_now(
    ctx: AppContext, session: AsyncSession, principal: Principal, account_id: uuid.UUID
) -> MailFetchOut:
    """立即收取一次（暂停收信的邮箱也收，成功后恢复）。"""
    account, _ = await _get(session, account_id)
    if account.status == MailStatus.DISABLED:
        raise Conflict("邮箱已停用，先启用再收取")
    await session.commit()
    result = await inbox.fetch(ctx, principal.tenant_id, account_id, manual=True)
    session.expire_all()
    return MailFetchOut(
        imported=result.imported,
        ignored=result.ignored,
        error=result.error,
        account=_out(*await _get(session, account_id)),
    )


async def original_mail(
    ctx: AppContext, session: AsyncSession, principal: Principal, message_id: uuid.UUID
) -> MailOriginalOut:
    """原邮件的 HTML（能看这个会话的员工）。"""
    message = await session.scalar(select(Message).where(Message.id == message_id).limit(1))
    if message is None or message.content_type != "email" or message.session_id is None:
        raise NotFound("邮件不存在")
    await visible_session(session, principal, message.session_id)
    content = message.content or {}
    subject = str(content.get("subject") or "")
    key = key_of_url(ctx.settings, str(content.get("url") or ""))
    text = str(content.get("text") or message.text_plain or "")
    if key is None:
        # 没有原文（坐席的回复、超过保留期删除的）：显示文字。
        return MailOriginalOut(subject=subject, html=original.wrap_text(text))
    if content.get("scan") == "infected":
        return MailOriginalOut(
            subject=subject, html=original.wrap_text("原邮件含有病毒，已经删除。\n\n" + text)
        )
    try:
        raw = await ctx.storage.get(key)
    except StorageError:
        return MailOriginalOut(subject=subject, html=original.wrap_text(text))
    return MailOriginalOut(subject=subject, html=await asyncio.to_thread(original.render, raw))
