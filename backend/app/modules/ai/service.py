"""AI 接待设置与可用性：是否配置了大模型、租户是否启用、本月额度是否用完。"""

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.modules.ai.models import AiSettings
from app.modules.ai.schemas import AiSettingsOut, AiSettingsUpdate
from app.modules.billing.entitlements import ai_replies_this_month, entitlements

DEFAULTS = {
    "enabled": False,
    "bot_name": "智能客服",
    "persona": None,
    "handoff_threshold": 0.6,
    "max_turns": 8,
    "relevance_threshold": 0.55,
    "handoff_keywords": [],
    "sensitive_keywords": [],
    "extraction_enabled": True,
    "auto_merge_similar": False,
}

UNAVAILABLE = {
    "not_configured": "平台还没有配置大模型",
    "disabled": "租户没有启用 AI 接待",
    "plan": "当前套餐不包含 AI 接待",
    "quota": "本月 AI 回复额度已用完",
}


async def load(session: AsyncSession, tenant_id: uuid.UUID) -> AiSettings:
    """租户的 AI 设置；没有保存过时返回默认值（不写库）。"""
    row = await session.get(AiSettings, tenant_id)
    return row or AiSettings(tenant_id=tenant_id, **DEFAULTS)


async def update(
    session: AsyncSession, tenant_id: uuid.UUID, payload: AiSettingsUpdate
) -> AiSettings:
    row = await session.get(AiSettings, tenant_id)
    if row is None:
        row = AiSettings(tenant_id=tenant_id, **DEFAULTS)
        session.add(row)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is None and field != "persona":
            continue
        setattr(row, field, value)
    await session.commit()
    await session.refresh(row)
    return row


async def quota_status(
    session: AsyncSession, tenant_id: uuid.UUID, tz: ZoneInfo, now: datetime
) -> tuple[int | None, int]:
    """（本月额度，本月已用的 AI 回复条数）。额度来自套餐（或平台单独设置），为空表示不限。"""
    entitled = await entitlements(session, tenant_id)
    used = await ai_replies_this_month(session, tenant_id, tz, now)
    return entitled.limit("ai_replies_monthly"), used


async def unavailable_reason(
    ctx: AppContext, session: AsyncSession, settings: AiSettings, now: datetime
) -> str | None:
    """AI 不能接待的原因（见 UNAVAILABLE）；可以接待时为空。

    超出额度后：套餐的超额策略为 degrade 时停止接待（转人工）；为 warn 时继续回复，按条计费。
    """
    if not await ctx.llms.chat_enabled(settings.tenant_id):
        return "not_configured"
    if not settings.enabled:
        return "disabled"
    entitled = await entitlements(session, settings.tenant_id)
    if not entitled.has("ai"):
        return "plan"
    quota = entitled.limit("ai_replies_monthly")
    if quota is not None and not entitled.overage_allowed:
        tz = ZoneInfo(ctx.settings.usage_timezone)
        if await ai_replies_this_month(session, settings.tenant_id, tz, now) >= quota:
            return "quota"
    return None


async def settings_out(
    ctx: AppContext, session: AsyncSession, settings: AiSettings, now: datetime
) -> AiSettingsOut:
    quota, used = await quota_status(
        session, settings.tenant_id, ZoneInfo(ctx.settings.usage_timezone), now
    )
    return AiSettingsOut(
        enabled=settings.enabled,
        bot_name=settings.bot_name,
        persona=settings.persona,
        handoff_threshold=settings.handoff_threshold,
        max_turns=settings.max_turns,
        relevance_threshold=settings.relevance_threshold,
        handoff_keywords=list(settings.handoff_keywords),
        sensitive_keywords=list(settings.sensitive_keywords),
        extraction_enabled=settings.extraction_enabled,
        auto_merge_similar=settings.auto_merge_similar,
        llm_configured=await ctx.llms.chat_enabled(settings.tenant_id),
        embeddings_configured=await ctx.llms.embed_enabled(),
        monthly_quota=quota,
        used_this_month=used,
    )
