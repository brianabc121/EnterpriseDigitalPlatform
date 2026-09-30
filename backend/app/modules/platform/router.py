from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES
from app.modules.audit.service import record_audit
from app.modules.platform import deadletters, llm, ops, prompts
from app.modules.platform import settings as platform_settings
from app.modules.platform.content import CONTENT_POLICY, content_policy
from app.modules.platform.schemas import (
    ChannelOverview,
    ContentPolicy,
    DeadLetterAction,
    DeadLetterList,
    HealthReport,
    ImOpAction,
    ImOpList,
    LlmProviderCreate,
    LlmProviderList,
    LlmProviderOut,
    LlmProviderUpdate,
    LlmRoutesOut,
    LlmRoutesUpdate,
    LlmTestResult,
    LlmUsage,
    OpsResult,
    PlatformAuditList,
    PromptActivate,
    PromptList,
    PromptOut,
    PromptVersionCreate,
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


# ---- 运维：死信与 IM 发件箱 ----


@router.get("/ops/dead-letters", response_model=DeadLetterList)
async def list_dead_letters(
    session: PlatformDb,
    _: CurrentPlatformUser,
    ctx: ContextDep,
    tenant_id: UUID | None = None,
    event_type: Annotated[str | None, Query(alias="type", max_length=64)] = None,
    before: Annotated[
        str | None, Query(pattern=r"^\d{1,20}-\d{1,20}$", description="上一页返回的 next_before")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DeadLetterList:
    """处理失败进入死信的事件（新的在前）。"""
    return await deadletters.list_dead_letters(
        ctx, session, tenant_id=tenant_id, type_=event_type, before=before, limit=limit
    )


@router.post("/ops/dead-letters/retry", response_model=OpsResult)
async def retry_dead_letters(
    payload: DeadLetterAction,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> OpsResult:
    """重新发布这些事件（问题修复后），成功后从死信中删除。"""
    result = await deadletters.retry_dead_letters(ctx, payload.ids)
    _record_ops(session, "platform.dead_letters.retry", user.id, payload.ids, result, request)
    await session.commit()
    return result


@router.post("/ops/dead-letters/discard", response_model=OpsResult)
async def discard_dead_letters(
    payload: DeadLetterAction,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> OpsResult:
    result = await deadletters.discard_dead_letters(ctx, payload.ids)
    _record_ops(session, "platform.dead_letters.discard", user.id, payload.ids, result, request)
    await session.commit()
    return result


@router.get("/ops/im-ops", response_model=ImOpList)
async def list_im_ops(
    session: PlatformDb,
    _: CurrentPlatformUser,
    status_: Annotated[Literal["failed", "stuck", "pending"], Query(alias="status")] = "failed",
    tenant_id: UUID | None = None,
    op: Annotated[str | None, Query(max_length=16)] = None,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ImOpList:
    """IM 发件箱：最终失败、积压（到期 5 分钟以上仍未执行）或全部待执行的操作。"""
    return await deadletters.list_im_ops(
        session, status=status_, tenant_id=tenant_id, op=op, before_id=before_id, limit=limit
    )


@router.post("/ops/im-ops/retry", response_model=OpsResult)
async def retry_im_ops(
    payload: ImOpAction,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> OpsResult:
    """失败的操作重新排队（重试次数清零），待执行的立即执行；在线信令不能重试。"""
    result, tenants = await deadletters.retry_im_ops(ctx, session, payload.ids)
    _record_ops(
        session, "platform.im_ops.retry", user.id, payload.ids, result, request, tenants=tenants
    )
    await session.commit()
    return result


@router.post("/ops/im-ops/discard", response_model=OpsResult)
async def discard_im_ops(
    payload: ImOpAction,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
) -> OpsResult:
    """放弃待执行的操作，同一个 Room 后面的操作可以继续执行。"""
    result, tenants = await deadletters.discard_im_ops(session, payload.ids)
    _record_ops(
        session, "platform.im_ops.discard", user.id, payload.ids, result, request, tenants=tenants
    )
    await session.commit()
    return result


def _record_ops(
    session: PlatformDb,
    action: str,
    actor_id: UUID,
    ids: list[str] | list[int],
    result: OpsResult,
    request: Request,
    *,
    tenants: list[UUID] | None = None,
) -> None:
    detail: dict[str, object] = {"ids": [str(i) for i in ids[:50]], "done": result.done}
    if tenants:
        detail["tenants"] = [str(t) for t in tenants]
    record_audit(
        session,
        action=action,
        actor_type="platform",
        actor_id=actor_id,
        resource_type="ops",
        detail=detail,
        ip=client_ip(request),
    )


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
    """给租户指定供应商（大客户专属模型等）和大模型并发上限；供应商为空表示用平台默认。
    租户自带密钥时以租户的为准。"""
    await tenancy.get_tenant(session, tenant_id)
    return await llm.assign_tenant_llm(
        ctx, session, tenant_id, payload, actor_id=user.id, ip=client_ip(request)
    )


@router.get("/llm-usage", response_model=LlmUsage)
async def llm_usage(
    session: PlatformDb,
    _: CurrentPlatformUser,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> LlmUsage:
    """近若干天的大模型调用量、tokens 与估算费用（按模型、租户、场景）。"""
    return await llm.usage(session, days=days)


# ---- 提示词版本 ----


@router.get("/prompts", response_model=PromptList)
async def list_prompts(session: PlatformDb, _: CurrentPlatformUser) -> PromptList:
    """各场景的提示词：内置模板、启用的版本和全部历史版本。"""
    return PromptList(items=await prompts.list_prompts(session))


@router.post(
    "/prompts/{key}/versions", response_model=PromptOut, status_code=status.HTTP_201_CREATED
)
async def create_prompt_version(
    key: str,
    payload: PromptVersionCreate,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> PromptOut:
    """保存新版本（可以立即启用）。模板只能使用这个场景提供的变量。"""
    return await prompts.create_version(
        ctx, session, key, payload, actor_id=user.id, ip=client_ip(request)
    )


@router.post("/prompts/{key}/activate", response_model=PromptOut)
async def activate_prompt(
    key: str,
    payload: PromptActivate,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> PromptOut:
    """启用某个版本（回滚）；version 为空时改回内置模板。"""
    return await prompts.activate(
        ctx, session, key, payload.version, actor_id=user.id, ip=client_ip(request)
    )
