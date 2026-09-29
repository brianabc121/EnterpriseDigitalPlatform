from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES
from app.modules.audit.service import record_audit
from app.modules.platform import llm, ops
from app.modules.platform import settings as platform_settings
from app.modules.platform.content import CONTENT_POLICY, content_policy
from app.modules.platform.schemas import (
    ChannelOverview,
    ContentPolicy,
    HealthReport,
    LlmProviderCreate,
    LlmProviderList,
    LlmProviderOut,
    LlmProviderUpdate,
    LlmRoutesOut,
    LlmRoutesUpdate,
    LlmTestResult,
    PlatformAuditList,
    TenantLlmAssign,
    TenantLlmOut,
)
from app.modules.tenancy import service as tenancy
from app.modules.tenancy.deps import CurrentPlatformUser, PlatformDb

router = APIRouter(prefix="/platform/v1", tags=["platform"], responses=ERROR_RESPONSES)

ContextDep = Annotated[AppContext, Depends(get_context)]


@router.get("/health", response_model=HealthReport)
async def platform_health(_: CurrentPlatformUser, ctx: ContextDep) -> HealthReport:
    """系统健康：数据库、Redis、OpenIM、对象存储、大模型、企业微信、发件箱、实时消费与调度进程。"""
    return await ops.health(ctx)


@router.get("/audit-logs", response_model=PlatformAuditList)
async def platform_audit_logs(
    session: PlatformDb,
    _: CurrentPlatformUser,
    tenant_id: UUID | None = None,
    action: Annotated[str | None, Query(max_length=64, description="动作或前缀，如 tenant")] = None,
    actor_type: Annotated[str | None, Query(max_length=16)] = None,
    start: datetime | None = None,
    before: Annotated[datetime | None, Query(description="翻页：上一页返回的 next_before")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PlatformAuditList:
    """平台与各租户的审计日志（平台运营的操作、支持访问、租户的关键操作）。"""
    return await ops.audit_logs(
        session,
        tenant_id=tenant_id,
        action=action,
        actor_type=actor_type,
        start=start,
        before=before,
        limit=limit,
    )


@router.get("/channels", response_model=ChannelOverview)
async def channel_overview(
    session: PlatformDb, _: CurrentPlatformUser, ctx: ContextDep
) -> ChannelOverview:
    """各租户的渠道与企业微信授权状态。"""
    return await ops.channel_overview(ctx, session)


# ---- 内容安全 ----


@router.get("/settings/content-policy", response_model=ContentPolicy)
async def get_content_policy(session: PlatformDb, _: CurrentPlatformUser) -> ContentPolicy:
    return await content_policy(session)


@router.put("/settings/content-policy", response_model=ContentPolicy)
async def put_content_policy(
    payload: ContentPolicy, request: Request, session: PlatformDb, user: CurrentPlatformUser
) -> ContentPolicy:
    """全局敏感词：AI 接待转人工与回复拦截、坐席消息拦截。"""
    words = list(dict.fromkeys(w.strip() for w in payload.words if w.strip()))
    policy = payload.model_copy(update={"words": words})
    await platform_settings.write(session, CONTENT_POLICY, policy, actor_id=user.id)
    record_audit(
        session,
        action="platform.settings",
        actor_type="platform",
        actor_id=user.id,
        resource_type="platform_setting",
        resource_id=CONTENT_POLICY,
        detail={
            "words": len(words),
            "apply_to_ai": policy.apply_to_ai,
            "block_agent_messages": policy.block_agent_messages,
        },
        ip=client_ip(request),
    )
    await session.commit()
    return policy


# ---- 大模型供应商 ----


@router.get("/llm-providers", response_model=LlmProviderList)
async def list_providers(
    session: PlatformDb, _: CurrentPlatformUser, ctx: ContextDep
) -> LlmProviderList:
    return LlmProviderList(items=await llm.list_providers(ctx, session))


@router.post("/llm-providers", response_model=LlmProviderOut, status_code=status.HTTP_201_CREATED)
async def create_provider(
    payload: LlmProviderCreate,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> LlmProviderOut:
    """添加供应商（OpenAI 兼容接口）。设为默认后，没有单独指定供应商的租户都用它。"""
    return await llm.create_provider(ctx, session, payload, actor_id=user.id, ip=client_ip(request))


@router.patch("/llm-providers/{provider_id}", response_model=LlmProviderOut)
async def update_provider(
    provider_id: UUID,
    payload: LlmProviderUpdate,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> LlmProviderOut:
    return await llm.update_provider(
        ctx, session, provider_id, payload, actor_id=user.id, ip=client_ip(request)
    )


@router.delete("/llm-providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: UUID,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> None:
    await llm.delete_provider(ctx, session, provider_id, actor_id=user.id, ip=client_ip(request))


@router.post("/llm-providers/{provider_id}/test", response_model=LlmTestResult)
async def test_provider(
    provider_id: UUID, session: PlatformDb, _: CurrentPlatformUser, ctx: ContextDep
) -> LlmTestResult:
    """发一次很短的请求，检查地址、密钥和模型名。"""
    return await llm.test_provider(ctx, session, provider_id)


@router.get("/settings/llm-routes", response_model=LlmRoutesOut)
async def get_routes(session: PlatformDb, _: CurrentPlatformUser) -> LlmRoutesOut:
    return await llm.routes_out(session)


@router.put("/settings/llm-routes", response_model=LlmRoutesOut)
async def put_routes(
    payload: LlmRoutesUpdate,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> LlmRoutesOut:
    """按场景指定供应商（例如知识提炼用便宜的模型）；没有指定的场景用默认供应商。"""
    return await llm.set_routes(
        ctx, session, payload.routes, actor_id=user.id, ip=client_ip(request)
    )


@router.get("/tenants/{tenant_id}/llm", response_model=TenantLlmOut)
async def tenant_llm(
    tenant_id: UUID, session: PlatformDb, _: CurrentPlatformUser, ctx: ContextDep
) -> TenantLlmOut:
    await tenancy.get_tenant(session, tenant_id)
    return await llm.tenant_llm(ctx, session, tenant_id)


@router.put("/tenants/{tenant_id}/llm", response_model=TenantLlmOut)
async def assign_tenant_llm(
    tenant_id: UUID,
    payload: TenantLlmAssign,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> TenantLlmOut:
    """给租户指定供应商（大客户专属模型等）；为空表示用平台默认。租户自带密钥时以租户的为准。"""
    await tenancy.get_tenant(session, tenant_id)
    return await llm.assign_tenant_llm(
        ctx, session, tenant_id, payload.provider_id, actor_id=user.id, ip=client_ip(request)
    )
