from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, status

from app.context import AppContext
from app.core.dates import today
from app.core.deps import client_ip, get_context, get_rate_limiter
from app.core.errors import ERROR_RESPONSES, ErrorResponse, NotFound
from app.core.permissions import Permission
from app.core.ratelimit import SIGNUP_PER_IP, RateLimiter
from app.modules.audit.service import record_audit
from app.modules.billing.service import TENANT_POLICY, tenant_policy
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.lifecycle import closure, export, signup, support
from app.modules.lifecycle.schemas import (
    ClosureRequest,
    ClosureStatus,
    ExportDownload,
    PlatformClosureRequest,
    PurgeResult,
    SignupOptions,
    SignupRequest,
    SignupResult,
    SupportGrantCreate,
    SupportGrantList,
    SupportGrantOut,
    SupportMessageList,
    SupportSessionList,
    SupportStatus,
    TenantDeletionList,
    TenantDeletionOut,
    TenantExportList,
    TenantExportOut,
)
from app.modules.platform import settings as platform_settings
from app.modules.platform.schemas import TenantPolicy
from app.modules.tenancy import service as tenancy
from app.modules.tenancy.deps import CurrentPlatformUser, PlatformDb
from app.modules.tenancy.models import Tenant

public_router = APIRouter(prefix="/api/v1", tags=["signup"], responses=ERROR_RESPONSES)
router = APIRouter(prefix="/api/v1/tenant", tags=["tenant"], responses=ERROR_RESPONSES)
platform_router = APIRouter(prefix="/platform/v1", tags=["platform"], responses=ERROR_RESPONSES)

ContextDep = Annotated[AppContext, Depends(get_context)]
CanManageTenant = Annotated[Principal, Depends(require_permission(Permission.TENANT_MANAGE))]


# ---- 自助注册 ----


@public_router.get("/signup", response_model=SignupOptions)
async def signup_options(session: PlatformDb) -> SignupOptions:
    """是否开放自助注册，以及注册后试用的套餐。"""
    return await signup.options(session)


@public_router.post(
    "/signup",
    response_model=SignupResult,
    status_code=status.HTTP_201_CREATED,
    responses={429: {"model": ErrorResponse}},
)
async def self_signup(
    payload: SignupRequest,
    request: Request,
    session: PlatformDb,
    ctx: ContextDep,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> SignupResult:
    """企业自助注册：开通租户和管理员账号并开始试用，之后用企业代码和账号登录控制台。"""
    ip = client_ip(request)
    if ip:
        await limiter.check(SIGNUP_PER_IP, ip)
    return await signup.signup(
        session, payload, ip=ip, current_day=today(ZoneInfo(ctx.settings.usage_timezone))
    )


# ---- 租户：注销与导出 ----


@router.get("/closure", response_model=ClosureStatus)
async def closure_status(session: TenantDb, principal: CanManageTenant) -> ClosureStatus:
    tenant = await session.get(Tenant, principal.tenant_id)
    if tenant is None:
        raise NotFound("租户不存在")
    return await closure.closure_status(session, tenant)


@router.post("/closure", response_model=ClosureStatus)
async def request_closure(
    payload: ClosureRequest, request: Request, principal: CanManageTenant, ctx: ContextDep
) -> ClosureStatus:
    """申请注销：自动导出一次数据，保留期结束后删除全部业务数据（期间可以撤销）。"""
    return await closure.request_closure(ctx, principal, payload, ip=client_ip(request))


@router.delete("/closure", response_model=ClosureStatus)
async def cancel_closure(
    request: Request, principal: CanManageTenant, ctx: ContextDep
) -> ClosureStatus:
    async with ctx.db.platform_sessionmaker() as session:
        tenant = await tenancy.get_tenant(session, principal.tenant_id)
        return await closure.cancel_closure(
            session,
            tenant,
            actor_type="staff",
            actor_id=principal.staff_id,
            ip=client_ip(request),
        )


@router.get("/exports", response_model=TenantExportList)
async def list_exports(session: TenantDb, principal: CanManageTenant) -> TenantExportList:
    items = await export.list_exports(session, principal.tenant_id)
    return TenantExportList(items=[TenantExportOut.model_validate(e) for e in items])


@router.post("/exports", response_model=TenantExportOut, status_code=status.HTTP_201_CREATED)
async def request_export(
    request: Request, session: TenantDb, principal: CanManageTenant
) -> TenantExportOut:
    """导出本企业的全部业务数据（后台生成，几分钟后在列表里下载）。"""
    item = await export.request_export(session, principal.tenant_id, principal.staff_id)
    record_audit(
        session,
        action="tenant.export",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="tenant_export",
        resource_id=str(item.id),
        ip=client_ip(request),
    )
    await session.commit()
    await session.refresh(item)
    return TenantExportOut.model_validate(item)


@router.get("/exports/{export_id}/download", response_model=ExportDownload)
async def download_export(
    export_id: UUID,
    request: Request,
    session: TenantDb,
    principal: CanManageTenant,
    ctx: ContextDep,
) -> ExportDownload:
    """短时有效的下载地址。"""
    result = await export.download(ctx, session, principal.tenant_id, export_id)
    record_audit(
        session,
        action="tenant.export_download",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="tenant_export",
        resource_id=str(export_id),
        ip=client_ip(request),
    )
    await session.commit()
    return result


# ---- 租户：授权平台运维访问 ----


@router.get("/support-grants", response_model=SupportGrantList)
async def list_support_grants(session: TenantDb, principal: CanManageTenant) -> SupportGrantList:
    """授权记录与平台人员的查看记录。"""
    return await support.list_grants(session, principal.tenant_id)


@router.post("/support-grants", response_model=SupportGrantOut, status_code=status.HTTP_201_CREATED)
async def create_support_grant(
    payload: SupportGrantCreate, request: Request, session: TenantDb, principal: CanManageTenant
) -> SupportGrantOut:
    """授权平台运维在有效期内只读查看会话和消息，用于排查问题。"""
    grant = await support.create_grant(session, principal, payload, ip=client_ip(request))
    return support.grant_out(grant, support.utcnow())


@router.delete("/support-grants/{grant_id}", response_model=SupportGrantOut)
async def revoke_support_grant(
    grant_id: UUID, request: Request, session: TenantDb, principal: CanManageTenant
) -> SupportGrantOut:
    grant = await support.revoke_grant(session, principal, grant_id, ip=client_ip(request))
    return support.grant_out(grant, support.utcnow())


# ---- 平台 ----


@platform_router.get("/settings/tenant-policy", response_model=TenantPolicy)
async def get_tenant_policy(session: PlatformDb, _: CurrentPlatformUser) -> TenantPolicy:
    return await tenant_policy(session)


@platform_router.put("/settings/tenant-policy", response_model=TenantPolicy)
async def put_tenant_policy(
    payload: TenantPolicy, request: Request, session: PlatformDb, user: CurrentPlatformUser
) -> TenantPolicy:
    """自助注册、订阅到期宽限期、注销保留期、导出文件保留天数。"""
    await platform_settings.write(session, TENANT_POLICY, payload, actor_id=user.id)
    record_audit(
        session,
        action="platform.settings",
        actor_type="platform",
        actor_id=user.id,
        resource_type="platform_setting",
        resource_id=TENANT_POLICY,
        detail=payload.model_dump(mode="json"),
        ip=client_ip(request),
    )
    await session.commit()
    return payload


@platform_router.get("/tenants/{tenant_id}/closure", response_model=ClosureStatus)
async def platform_closure_status(
    tenant_id: UUID, session: PlatformDb, _: CurrentPlatformUser
) -> ClosureStatus:
    return await closure.closure_status(session, await tenancy.get_tenant(session, tenant_id))


@platform_router.post("/tenants/{tenant_id}/closure", response_model=ClosureStatus)
async def platform_request_closure(
    tenant_id: UUID,
    payload: PlatformClosureRequest,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
) -> ClosureStatus:
    """代租户申请注销（例如合同终止）：自动导出一次数据，保留期结束后删除。"""
    tenant = await tenancy.get_tenant(session, tenant_id)
    return await closure.start_closure(
        session,
        tenant,
        actor_type="platform",
        actor_id=user.id,
        reason=payload.reason,
        ip=client_ip(request),
    )


@platform_router.delete("/tenants/{tenant_id}/closure", response_model=ClosureStatus)
async def platform_cancel_closure(
    tenant_id: UUID, request: Request, session: PlatformDb, user: CurrentPlatformUser
) -> ClosureStatus:
    tenant = await tenancy.get_tenant(session, tenant_id)
    return await closure.cancel_closure(
        session, tenant, actor_type="platform", actor_id=user.id, ip=client_ip(request)
    )


@platform_router.post("/tenants/{tenant_id}/purge", response_model=PurgeResult)
async def purge_tenant(
    tenant_id: UUID, request: Request, user: CurrentPlatformUser, ctx: ContextDep
) -> PurgeResult:
    """立即删除已申请注销的租户的全部业务数据（不等保留期结束），返回删除记录。"""
    deletion = await closure.purge_tenant(
        ctx, tenant_id, actor_type="platform", actor_id=user.id, ip=client_ip(request)
    )
    return PurgeResult(status="purged", deletion=TenantDeletionOut.model_validate(deletion))


@platform_router.get("/deletions", response_model=TenantDeletionList)
async def list_deletions(session: PlatformDb, _: CurrentPlatformUser) -> TenantDeletionList:
    """租户数据删除记录（删除证明）。"""
    items = await closure.list_deletions(session)
    return TenantDeletionList(items=[TenantDeletionOut.model_validate(d) for d in items])


@platform_router.get("/tenants/{tenant_id}/support", response_model=SupportStatus)
async def support_status(
    tenant_id: UUID, session: PlatformDb, _: CurrentPlatformUser
) -> SupportStatus:
    """租户是否授权平台查看业务数据。"""
    await tenancy.get_tenant(session, tenant_id)
    grant = await support.active_grant(session, tenant_id)
    now = support.utcnow()
    return SupportStatus(
        active=grant is not None, grant=support.grant_out(grant, now) if grant else None
    )


@platform_router.get("/tenants/{tenant_id}/support/sessions", response_model=SupportSessionList)
async def support_sessions(
    tenant_id: UUID,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SupportSessionList:
    """最近的会话（需要租户授权；每次查看记入审计）。"""
    items = await support.recent_sessions(
        session, tenant_id, actor_id=user.id, ip=client_ip(request), limit=limit
    )
    return SupportSessionList(items=items)


@platform_router.get(
    "/tenants/{tenant_id}/support/sessions/{session_id}/messages",
    response_model=SupportMessageList,
)
async def support_messages(
    tenant_id: UUID,
    session_id: UUID,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
) -> SupportMessageList:
    """一个会话的消息（需要租户授权；每次查看记入审计）。"""
    items = await support.session_messages(
        session, tenant_id, session_id, actor_id=user.id, ip=client_ip(request)
    )
    return SupportMessageList(items=items)
