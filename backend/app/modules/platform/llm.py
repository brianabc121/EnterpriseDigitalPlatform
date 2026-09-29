"""大模型供应商配置（运营后台，设计文档 §7.5、§11.5）：供应商、按场景路由、给租户指定供应商。

接口密钥用 EDP_DATA_ENCRYPTION_KEY 加密保存，接口只返回末 4 位。保存后清空路由缓存，几秒内所有
进程生效。
"""

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.crypto import seal
from app.core.errors import NotFound, Unprocessable
from app.core.ids import new_id
from app.integrations.llm import EmbedEndpoint, LLMEndpoint, LLMError
from app.modules.ai import service as ai_service
from app.modules.ai.llm_router import ROUTES_KEY, SCENES, LlmRoutes
from app.modules.ai.models import AiSettings
from app.modules.audit.service import record_audit
from app.modules.kb.models import EMBED_DIM
from app.modules.platform import settings as platform_settings
from app.modules.platform.models import LlmProvider
from app.modules.platform.schemas import (
    LlmCheck,
    LlmPrices,
    LlmProviderCreate,
    LlmProviderOut,
    LlmProviderUpdate,
    LlmRoutesOut,
    LlmTestResult,
    TenantLlmOut,
)


def _hint(ctx: AppContext, provider: LlmProvider) -> str | None:
    from app.core.crypto import DecryptError, unseal

    if not provider.api_key_enc:
        return None
    try:
        key = unseal(ctx.settings, provider.api_key_enc)
    except DecryptError:
        return "????"
    return key[-4:] if len(key) >= 8 else "****"


def provider_out(ctx: AppContext, provider: LlmProvider, tenants: int = 0) -> LlmProviderOut:
    return LlmProviderOut(
        id=provider.id,
        name=provider.name,
        base_url=provider.base_url,
        api_key_set=bool(provider.api_key_enc),
        api_key_hint=_hint(ctx, provider),
        chat_model=provider.chat_model,
        fast_model=provider.fast_model,
        embed_model=provider.embed_model,
        embed_dim=provider.embed_dim,
        send_dimensions=provider.send_dimensions,
        prices=LlmPrices.model_validate(provider.prices or {}),
        is_default=provider.is_default,
        enabled=provider.enabled,
        tenants=tenants,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


async def list_providers(ctx: AppContext, session: AsyncSession) -> list[LlmProviderOut]:
    providers = (await session.scalars(select(LlmProvider).order_by(LlmProvider.created_at))).all()
    counts = dict(
        (
            await session.execute(
                select(AiSettings.llm_provider_id, func.count())
                .where(AiSettings.llm_provider_id.is_not(None))
                .group_by(AiSettings.llm_provider_id)
            )
        ).all()
    )
    return [provider_out(ctx, p, counts.get(p.id, 0)) for p in providers]


async def get_provider(session: AsyncSession, provider_id: uuid.UUID) -> LlmProvider:
    provider = await session.get(LlmProvider, provider_id)
    if provider is None:
        raise NotFound("供应商不存在")
    return provider


def _check_embedding(embed_model: str, embed_dim: int) -> None:
    if embed_model and embed_dim != EMBED_DIM:
        raise Unprocessable(f"向量维度必须是 {EMBED_DIM}（与知识库的向量字段一致）")


async def _make_default(session: AsyncSession, provider: LlmProvider) -> None:
    await session.execute(
        update(LlmProvider)
        .where(LlmProvider.is_default.is_(True), LlmProvider.id != provider.id)
        .values(is_default=False)
    )
    provider.is_default = True


def _audit(
    session: AsyncSession,
    action: str,
    provider: LlmProvider,
    actor_id: uuid.UUID,
    ip: str | None,
    detail: dict[str, Any],
) -> None:
    detail = {k: v for k, v in detail.items() if k != "api_key"}
    record_audit(
        session,
        action=action,
        actor_type="platform",
        actor_id=actor_id,
        resource_type="llm_provider",
        resource_id=str(provider.id),
        detail=detail,
        ip=ip,
    )


async def create_provider(
    ctx: AppContext,
    session: AsyncSession,
    payload: LlmProviderCreate,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> LlmProviderOut:
    _check_embedding(payload.embed_model, payload.embed_dim)
    provider = LlmProvider(
        id=new_id(),
        name=payload.name.strip(),
        base_url=payload.base_url.rstrip("/"),
        api_key_enc=seal(ctx.settings, payload.api_key) if payload.api_key else "",
        chat_model=payload.chat_model.strip(),
        fast_model=payload.fast_model.strip(),
        embed_model=payload.embed_model.strip(),
        embed_dim=payload.embed_dim,
        send_dimensions=payload.send_dimensions,
        prices=payload.prices.model_dump(),
        enabled=payload.enabled,
    )
    session.add(provider)
    await session.flush()
    if payload.is_default:
        await _make_default(session, provider)
    _audit(session, "llm_provider.create", provider, actor_id, ip, payload.model_dump(mode="json"))
    await session.commit()
    await session.refresh(provider)
    ctx.llms.invalidate()
    return provider_out(ctx, provider)


async def update_provider(
    ctx: AppContext,
    session: AsyncSession,
    provider_id: uuid.UUID,
    payload: LlmProviderUpdate,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> LlmProviderOut:
    provider = await get_provider(session, provider_id)
    changes = payload.model_dump(exclude_unset=True, mode="json")
    api_key = changes.pop("api_key", None)
    make_default = changes.pop("is_default", None)
    for field, value in changes.items():
        if value is None:
            continue
        if field == "base_url":
            value = value.rstrip("/")
        setattr(provider, field, value)
    if api_key is not None:
        provider.api_key_enc = seal(ctx.settings, api_key) if api_key else ""
    _check_embedding(provider.embed_model, provider.embed_dim)
    if make_default is True:
        await _make_default(session, provider)
    elif make_default is False:
        provider.is_default = False
    _audit(
        session,
        "llm_provider.update",
        provider,
        actor_id,
        ip,
        {
            **payload.model_dump(mode="json", exclude_unset=True),
            "api_key_changed": api_key is not None,
        },
    )
    await session.commit()
    await session.refresh(provider)
    ctx.llms.invalidate()
    return provider_out(ctx, provider)


async def delete_provider(
    ctx: AppContext,
    session: AsyncSession,
    provider_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> None:
    """删除供应商：指定了它的租户改用默认供应商，路由里引用它的场景一并去掉。"""
    provider = await get_provider(session, provider_id)
    routes = await platform_settings.read(session, ROUTES_KEY, LlmRoutes)
    kept = {scene: pid for scene, pid in routes.routes.items() if pid != provider.id}
    if kept != routes.routes:
        await platform_settings.write(
            session, ROUTES_KEY, LlmRoutes(routes=kept), actor_id=actor_id
        )
    _audit(session, "llm_provider.delete", provider, actor_id, ip, {"name": provider.name})
    await session.delete(provider)
    await session.commit()
    ctx.llms.invalidate()


def _endpoints(ctx: AppContext, provider: LlmProvider) -> tuple[LLMEndpoint, EmbedEndpoint | None]:
    from app.core.crypto import unseal

    key = unseal(ctx.settings, provider.api_key_enc) if provider.api_key_enc else ""
    chat = LLMEndpoint(
        base_url=provider.base_url,
        api_key=key,
        chat_model=provider.chat_model,
        fast_model=provider.fast_model,
        name=provider.name,
    )
    embed = None
    if provider.embed_model:
        embed = EmbedEndpoint(
            base_url=provider.base_url,
            api_key=key,
            model=provider.embed_model,
            dim=provider.embed_dim,
            send_dimensions=provider.send_dimensions,
        )
    return chat, embed


async def check_endpoint(
    ctx: AppContext, chat: LLMEndpoint, embed: EmbedEndpoint | None = None
) -> LlmTestResult:
    """发一次很短的对话（和一次向量）请求，检查地址、密钥和模型名是否可用。"""
    client = ctx.llms.new_client(chat, embed=embed)
    try:
        try:
            result = await client.chat(
                [{"role": "user", "content": "你好，请回复“好”。"}], max_tokens=8
            )
            chat_check = LlmCheck(ok=True, latency_ms=result.latency_ms, model=result.model)
        except LLMError as exc:
            chat_check = LlmCheck(ok=False, error=str(exc)[:300])
        embed_check = None
        if embed is not None:
            try:
                vectors = await client.embed(["连通性检查"])
                embed_check = LlmCheck(ok=True, latency_ms=vectors.latency_ms, model=vectors.model)
            except LLMError as exc:
                embed_check = LlmCheck(ok=False, error=str(exc)[:300])
    finally:
        await client.aclose()
    return LlmTestResult(chat=chat_check, embed=embed_check)


async def test_provider(
    ctx: AppContext, session: AsyncSession, provider_id: uuid.UUID
) -> LlmTestResult:
    chat, embed = _endpoints(ctx, await get_provider(session, provider_id))
    return await check_endpoint(ctx, chat, embed)


async def routes_out(session: AsyncSession) -> LlmRoutesOut:
    routes = await platform_settings.read(session, ROUTES_KEY, LlmRoutes)
    return LlmRoutesOut(routes=routes.routes, scenes=SCENES)


async def set_routes(
    ctx: AppContext,
    session: AsyncSession,
    routes: dict[str, uuid.UUID],
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> LlmRoutesOut:
    unknown = set(routes) - set(SCENES)
    if unknown:
        raise Unprocessable(f"未知的场景：{'、'.join(sorted(unknown))}")
    for provider_id in set(routes.values()):
        await get_provider(session, provider_id)
    await platform_settings.write(session, ROUTES_KEY, LlmRoutes(routes=routes), actor_id=actor_id)
    record_audit(
        session,
        action="llm_routes.update",
        actor_type="platform",
        actor_id=actor_id,
        resource_type="platform_setting",
        resource_id=ROUTES_KEY,
        detail={scene: str(pid) for scene, pid in routes.items()},
        ip=ip,
    )
    await session.commit()
    ctx.llms.invalidate()
    return await routes_out(session)


async def tenant_llm(ctx: AppContext, session: AsyncSession, tenant_id: uuid.UUID) -> TenantLlmOut:
    provider_id = await session.scalar(
        select(AiSettings.llm_provider_id).where(AiSettings.tenant_id == tenant_id)
    )
    source, name = await ctx.llms.describe(tenant_id)
    return TenantLlmOut(provider_id=provider_id, source=source, provider_name=name)


async def assign_tenant_llm(
    ctx: AppContext,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    provider_id: uuid.UUID | None,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> TenantLlmOut:
    if provider_id is not None:
        await get_provider(session, provider_id)
    row = await session.get(AiSettings, tenant_id)
    if row is None:
        row = AiSettings(tenant_id=tenant_id, **ai_service.DEFAULTS)
        session.add(row)
    row.llm_provider_id = provider_id
    record_audit(
        session,
        action="tenant.llm_provider",
        actor_type="platform",
        actor_id=actor_id,
        tenant_id=tenant_id,
        resource_type="tenant",
        resource_id=str(tenant_id),
        detail={"provider_id": str(provider_id) if provider_id else None},
        ip=ip,
    )
    await session.commit()
    ctx.llms.invalidate()
    return await tenant_llm(ctx, session, tenant_id)
