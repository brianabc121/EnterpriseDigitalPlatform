"""机器人、身份绑定与群的管理（设计文档 §27.3.1、§27.3.2、§27.4）。

密钥用租户数据密钥加密成一段 JSON 保存（secrets_enc），接口只返回保存了哪些键。
"""

import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.config import Settings
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.ids import new_id
from app.integrations.imbots import SPECS, BotContext, SendError, adapter_for
from app.modules.assistant.models import (
    AssistantBot,
    AssistantGroup,
    AssistantGroupMessage,
    AssistantIdentity,
    BotStatus,
)
from app.modules.assistant.schemas import (
    BotIn,
    BotList,
    BotOut,
    BotTestOut,
    FieldSpecOut,
    GroupList,
    GroupMessageOut,
    GroupMessagePage,
    GroupOut,
    GroupUpdate,
    IdentityList,
    IdentityOut,
    MyBindingOut,
    MyBindings,
    ProviderList,
    ProviderOut,
)
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal

BOT_NOT_FOUND = "机器人不存在"
BINDING_TTL = timedelta(minutes=10)
BINDING_KEY = "edp:assistant:bind:{tenant}:{code}"
MAX_BOTS = 10


def utcnow() -> datetime:
    return datetime.now(UTC)


def webhook_url(settings: Settings, bot: AssistantBot) -> str:
    return f"{settings.public_api_url.rstrip('/')}/hooks/assistant/{bot.id}/{bot.webhook_token}"


def providers_out(settings: Settings) -> ProviderList:
    return ProviderList(
        items=[
            ProviderOut(
                provider=spec.provider,
                name=spec.name,
                config_fields=[FieldSpecOut(**f.__dict__) for f in spec.config_fields],
                secret_fields=[FieldSpecOut(**f.__dict__) for f in spec.secret_fields],
                notes=list(spec.notes),
                notify=spec.notify,
                groups=spec.groups,
            )
            for spec in SPECS.values()
        ],
        webhook_base=f"{settings.public_api_url.rstrip('/')}/hooks/assistant/",
    )


# ---- 密钥 ----


async def secrets_of(ctx: AppContext, bot: AssistantBot) -> dict[str, str]:
    if not bot.secrets_enc:
        return {}
    try:
        data = json.loads(await ctx.keys.unseal(bot.tenant_id, bot.secrets_enc))
    except Exception:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


async def bot_context(ctx: AppContext, bot: AssistantBot) -> BotContext:
    return BotContext(
        config=dict(bot.config or {}),
        secrets=await secrets_of(ctx, bot),
        webhook_url=webhook_url(ctx.settings, bot),
        webhook_token=bot.webhook_token,
    )


# ---- 机器人 ----


async def _counts(
    session: AsyncSession, bot_ids: list[uuid.UUID]
) -> dict[str, dict[uuid.UUID, int]]:
    if not bot_ids:
        return {"identities": {}, "groups": {}}
    identities = dict(
        (
            await session.execute(
                select(AssistantIdentity.bot_id, func.count())
                .where(
                    AssistantIdentity.bot_id.in_(bot_ids), AssistantIdentity.staff_id.is_not(None)
                )
                .group_by(AssistantIdentity.bot_id)
            )
        ).all()
    )
    groups = dict(
        (
            await session.execute(
                select(AssistantGroup.bot_id, func.count())
                .where(AssistantGroup.bot_id.in_(bot_ids))
                .group_by(AssistantGroup.bot_id)
            )
        ).all()
    )
    return {"identities": identities, "groups": groups}


def _bot_out(
    settings: Settings,
    bot: AssistantBot,
    secret_keys: list[str],
    counts: dict[str, dict[uuid.UUID, int]],
) -> BotOut:
    return BotOut(
        id=bot.id,
        provider=bot.provider,
        name=bot.name,
        config=dict(bot.config or {}),
        secret_keys=secret_keys,
        webhook_url=webhook_url(settings, bot),
        status=bot.status,
        last_received_at=bot.last_received_at,
        last_sent_at=bot.last_sent_at,
        last_error=bot.last_error,
        failures=bot.failures,
        identities=counts["identities"].get(bot.id, 0),
        groups=counts["groups"].get(bot.id, 0),
        created_at=bot.created_at,
        updated_at=bot.updated_at,
    )


async def bot_out(ctx: AppContext, session: AsyncSession, bot: AssistantBot) -> BotOut:
    await session.refresh(bot)
    keys = sorted(await secrets_of(ctx, bot))
    return _bot_out(ctx.settings, bot, keys, await _counts(session, [bot.id]))


async def list_bots(ctx: AppContext, session: AsyncSession) -> BotList:
    bots = (
        await session.scalars(
            select(AssistantBot).order_by(AssistantBot.created_at, AssistantBot.id)
        )
    ).all()
    counts = await _counts(session, [b.id for b in bots])
    items = []
    for bot in bots:
        items.append(_bot_out(ctx.settings, bot, sorted(await secrets_of(ctx, bot)), counts))
    return BotList(items=items)


async def get_bot(session: AsyncSession, bot_id: uuid.UUID) -> AssistantBot:
    bot = await session.get(AssistantBot, bot_id)
    if bot is None:
        raise NotFound(BOT_NOT_FOUND)
    return bot


async def active_bots(session: AsyncSession) -> list[AssistantBot]:
    return list(
        (
            await session.scalars(
                select(AssistantBot)
                .where(AssistantBot.status == BotStatus.ACTIVE)
                .order_by(AssistantBot.created_at)
            )
        ).all()
    )


def _clean(values: dict[str, str]) -> dict[str, str]:
    return {k.strip(): v.strip() for k, v in values.items() if k.strip() and v.strip()}


async def _prepare(ctx: AppContext, bot: AssistantBot, secrets_: dict[str, str]) -> dict[str, Any]:
    """校验配置并在平台侧做准备（Telegram setWebhook 等），返回要并入配置的内容。"""
    context = BotContext(
        config=dict(bot.config or {}),
        secrets=secrets_,
        webhook_url=webhook_url(ctx.settings, bot),
        webhook_token=bot.webhook_token,
    )
    try:
        return await adapter_for(bot.provider).setup(ctx.bots, ctx.settings, context)
    except ValueError as exc:
        raise Unprocessable(str(exc)) from exc
    except SendError as exc:
        raise Unprocessable(f"连接平台失败：{exc}") from exc


async def create_bot(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: BotIn
) -> AssistantBot:
    if payload.provider not in SPECS:
        raise Unprocessable("不支持的平台")
    total = await session.scalar(select(func.count()).select_from(AssistantBot))
    if (total or 0) >= MAX_BOTS:
        raise Conflict(f"最多接入 {MAX_BOTS} 个机器人")
    secrets_ = _clean(payload.secrets)
    bot = AssistantBot(
        id=new_id(),
        tenant_id=principal.tenant_id,
        provider=payload.provider,
        name=payload.name.strip(),
        config=_clean(payload.config),
        secrets_enc=await ctx.keys.seal(principal.tenant_id, json.dumps(secrets_)),
        webhook_token=secrets.token_urlsafe(24),
        status=BotStatus.ACTIVE.value,
        created_by=principal.staff_id,
    )
    extra = await _prepare(ctx, bot, secrets_)
    bot.config = {**bot.config, **{k: v for k, v in extra.items() if v}}
    session.add(bot)
    await session.flush()
    return bot


async def update_bot(
    ctx: AppContext, session: AsyncSession, bot_id: uuid.UUID, payload: BotIn
) -> AssistantBot:
    bot = await get_bot(session, bot_id)
    if payload.provider != bot.provider:
        raise Unprocessable("不能修改机器人的平台，请删除后重新添加")
    secrets_ = {**await secrets_of(ctx, bot), **_clean(payload.secrets)}
    bot.name = payload.name.strip()
    bot.config = _clean(payload.config)
    bot.secrets_enc = await ctx.keys.seal(bot.tenant_id, json.dumps(secrets_))
    extra = await _prepare(ctx, bot, secrets_)
    bot.config = {**bot.config, **{k: v for k, v in extra.items() if v}}
    bot.last_error = None
    bot.failures = 0
    await session.flush()
    return bot


async def set_status(session: AsyncSession, bot_id: uuid.UUID, enabled: bool) -> AssistantBot:
    bot = await get_bot(session, bot_id)
    bot.status = (BotStatus.ACTIVE if enabled else BotStatus.DISABLED).value
    if enabled:
        bot.last_error = None
        bot.failures = 0
    await session.flush()
    return bot


async def rotate_token(ctx: AppContext, session: AsyncSession, bot_id: uuid.UUID) -> AssistantBot:
    bot = await get_bot(session, bot_id)
    bot.webhook_token = secrets.token_urlsafe(24)
    await _prepare(ctx, bot, await secrets_of(ctx, bot))
    await session.flush()
    return bot


async def delete_bot(session: AsyncSession, bot_id: uuid.UUID) -> None:
    bot = await get_bot(session, bot_id)
    await session.delete(bot)
    await session.flush()


async def test_bot(
    ctx: AppContext, session: AsyncSession, bot_id: uuid.UUID, text: str
) -> BotTestOut:
    """给这个机器人上已绑定的员工各发一条测试消息。"""
    from app.modules.assistant import sender

    bot = await get_bot(session, bot_id)
    identities = (
        await session.scalars(
            select(AssistantIdentity).where(
                AssistantIdentity.bot_id == bot.id, AssistantIdentity.staff_id.is_not(None)
            )
        )
    ).all()
    if not identities:
        return BotTestOut(ok=False, sent=0, error="还没有员工绑定这个机器人")
    sent = 0
    error: str | None = None
    for identity in identities:
        try:
            await sender.send_to_identity(ctx, session, bot, identity, text)
            sent += 1
        except SendError as exc:
            error = str(exc)
    await session.commit()
    return BotTestOut(ok=sent > 0, sent=sent, error=error)


# ---- 身份 ----


async def identity_out(session: AsyncSession, rows: list[AssistantIdentity]) -> list[IdentityOut]:
    bots = {
        b.id: b
        for b in await session.scalars(
            select(AssistantBot).where(AssistantBot.id.in_({r.bot_id for r in rows}))
        )
    }
    names = dict(
        (
            await session.execute(
                select(Staff.id, Staff.display_name).where(
                    Staff.id.in_({r.staff_id for r in rows if r.staff_id})
                )
            )
        ).all()
    )
    out: list[IdentityOut] = []
    for row in rows:
        bot = bots.get(row.bot_id)
        if bot is None:
            continue
        out.append(
            IdentityOut(
                id=row.id,
                bot_id=row.bot_id,
                bot_name=bot.name,
                provider=bot.provider,
                external_user_id=row.external_user_id,
                display_name=row.display_name,
                staff_id=row.staff_id,
                staff_name=names.get(row.staff_id) if row.staff_id else None,
                bound_at=row.bound_at,
                last_seen_at=row.last_seen_at,
            )
        )
    return out


async def list_identities(session: AsyncSession) -> IdentityList:
    rows = list(
        (
            await session.scalars(
                select(AssistantIdentity).order_by(
                    AssistantIdentity.bound_at.desc().nulls_first(),
                    AssistantIdentity.last_seen_at.desc().nulls_last(),
                    AssistantIdentity.id,
                )
            )
        ).all()
    )
    return IdentityList(items=await identity_out(session, rows))


async def get_identity(session: AsyncSession, identity_id: uuid.UUID) -> AssistantIdentity:
    row = await session.get(AssistantIdentity, identity_id)
    if row is None:
        raise NotFound("账号不存在")
    return row


async def bind_identity(
    session: AsyncSession, identity: AssistantIdentity, staff_id: uuid.UUID, *, now: datetime
) -> AssistantIdentity:
    staff = await session.get(Staff, staff_id)
    if staff is None or staff.status != StaffStatus.ACTIVE:
        raise Unprocessable("员工不存在或已停用")
    other = await session.scalar(
        select(AssistantIdentity).where(
            AssistantIdentity.bot_id == identity.bot_id,
            AssistantIdentity.staff_id == staff_id,
            AssistantIdentity.id != identity.id,
        )
    )
    if other is not None:
        # 同一个员工在这个机器人上换了账号：解除旧的。
        other.staff_id = None
        other.bound_at = None
    identity.staff_id = staff_id
    identity.bound_at = now
    await session.flush()
    return identity


async def unbind_identity(session: AsyncSession, identity: AssistantIdentity) -> None:
    identity.staff_id = None
    identity.bound_at = None
    await session.flush()


async def my_bindings(ctx: AppContext, session: AsyncSession, principal: Principal) -> MyBindings:
    rows = list(
        (
            await session.scalars(
                select(AssistantIdentity)
                .where(AssistantIdentity.staff_id == principal.staff_id)
                .order_by(AssistantIdentity.bound_at.desc())
            )
        ).all()
    )
    outs = await identity_out(session, rows)
    bots = await list_bots(ctx, session)
    return MyBindings(
        items=[
            MyBindingOut(
                id=o.id,
                bot_id=o.bot_id,
                bot_name=o.bot_name,
                provider=o.provider,
                display_name=o.display_name,
                bound_at=o.bound_at,
            )
            for o in outs
        ],
        bots=[b for b in bots.items if b.status == "active"],
    )


# ---- 绑定码 ----


async def issue_binding_code(
    ctx: AppContext, tenant_id: uuid.UUID, staff_id: uuid.UUID
) -> tuple[str, datetime]:
    """6 位数字的绑定码，10 分钟有效；同一个人再取时上一个作废。"""
    for _ in range(5):
        code = f"{secrets.randbelow(1_000_000):06d}"
        key = BINDING_KEY.format(tenant=tenant_id, code=code)
        if await ctx.redis.set(key, str(staff_id), ex=int(BINDING_TTL.total_seconds()), nx=True):
            await ctx.redis.set(
                f"edp:assistant:bind-of:{tenant_id}:{staff_id}",
                code,
                ex=int(BINDING_TTL.total_seconds()),
            )
            return code, utcnow() + BINDING_TTL
    raise Conflict("请稍后再试")


async def redeem_binding_code(ctx: AppContext, tenant_id: uuid.UUID, code: str) -> uuid.UUID | None:
    key = BINDING_KEY.format(tenant=tenant_id, code=code)
    value = await ctx.redis.getdel(key)
    if not value:
        return None
    try:
        return uuid.UUID(value.decode() if isinstance(value, bytes) else str(value))
    except ValueError:
        return None


# ---- 群 ----


async def _group_outs(session: AsyncSession, rows: list[AssistantGroup]) -> list[GroupOut]:
    bots = {
        b.id: b
        for b in await session.scalars(
            select(AssistantBot).where(AssistantBot.id.in_({r.bot_id for r in rows}))
        )
    }
    pending = dict(
        (
            await session.execute(
                select(AssistantGroupMessage.group_id, func.count())
                .where(
                    AssistantGroupMessage.group_id.in_({r.id for r in rows}),
                    AssistantGroupMessage.extracted_at.is_(None),
                )
                .group_by(AssistantGroupMessage.group_id)
            )
        ).all()
    )
    out = []
    for row in rows:
        bot = bots.get(row.bot_id)
        if bot is None:
            continue
        out.append(
            GroupOut(
                id=row.id,
                bot_id=row.bot_id,
                bot_name=bot.name,
                provider=bot.provider,
                external_chat_id=row.external_chat_id,
                name=row.name,
                recording=row.recording,
                reply_mode=row.reply_mode,
                extract=row.extract,
                message_count=row.message_count,
                unextracted=pending.get(row.id, 0),
                last_message_at=row.last_message_at,
                last_extracted_at=row.last_extracted_at,
                extracted_candidates=row.extracted_candidates,
                created_at=row.created_at,
            )
        )
    return out


async def list_groups(session: AsyncSession) -> GroupList:
    rows = list(
        (
            await session.scalars(
                select(AssistantGroup).order_by(
                    AssistantGroup.last_message_at.desc().nulls_last(), AssistantGroup.id
                )
            )
        ).all()
    )
    return GroupList(items=await _group_outs(session, rows))


async def get_group(session: AsyncSession, group_id: uuid.UUID) -> AssistantGroup:
    row = await session.get(AssistantGroup, group_id)
    if row is None:
        raise NotFound("群不存在")
    return row


async def group_out(session: AsyncSession, group: AssistantGroup) -> GroupOut:
    await session.refresh(group)
    [out] = await _group_outs(session, [group])
    return out


async def update_group(
    session: AsyncSession, group_id: uuid.UUID, payload: GroupUpdate
) -> AssistantGroup:
    group = await get_group(session, group_id)
    if payload.name is not None:
        group.name = payload.name.strip()
    if payload.recording is not None:
        group.recording = payload.recording
    if payload.clear_reply_mode:
        group.reply_mode = None
    elif payload.reply_mode is not None:
        group.reply_mode = payload.reply_mode
    if payload.extract is not None:
        group.extract = payload.extract
    await session.flush()
    return group


async def clear_group(session: AsyncSession, group_id: uuid.UUID) -> AssistantGroup:
    group = await get_group(session, group_id)
    for row in await session.scalars(
        select(AssistantGroupMessage).where(AssistantGroupMessage.group_id == group.id)
    ):
        await session.delete(row)
    group.message_count = 0
    group.last_message_at = None
    await session.flush()
    return group


async def group_messages(
    session: AsyncSession, group_id: uuid.UUID, *, limit: int, offset: int
) -> GroupMessagePage:
    group = await get_group(session, group_id)
    base = select(AssistantGroupMessage).where(AssistantGroupMessage.group_id == group.id)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.scalars(
            base.order_by(AssistantGroupMessage.sent_at.desc(), AssistantGroupMessage.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    names = dict(
        (
            await session.execute(
                select(Staff.id, Staff.display_name).where(
                    Staff.id.in_({r.staff_id for r in rows if r.staff_id})
                )
            )
        ).all()
    )
    return GroupMessagePage(
        items=[
            GroupMessageOut(
                id=r.id,
                sender_name=r.sender_name or r.sender_external_id,
                staff_id=r.staff_id,
                staff_name=names.get(r.staff_id) if r.staff_id else None,
                text=r.text,
                sent_at=r.sent_at,
                extracted=r.extracted_at is not None,
            )
            for r in rows
        ],
        total=total or 0,
    )
