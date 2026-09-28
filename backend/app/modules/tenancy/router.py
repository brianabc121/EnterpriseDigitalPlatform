from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from app.core.config import Settings
from app.core.deps import client_ip, get_app_settings, get_rate_limiter
from app.core.errors import ERROR_RESPONSES, ErrorResponse
from app.core.ratelimit import RateLimiter, login_attempt
from app.core.security import encode_platform_token
from app.modules.tenancy import service
from app.modules.tenancy.deps import CurrentPlatformUser, PlatformDb
from app.modules.tenancy.schemas import (
    PlatformLoginRequest,
    PlatformMe,
    PlatformTokenResponse,
    TenantCreate,
    TenantList,
    TenantOut,
    TenantUpdate,
)

router = APIRouter(prefix="/platform/v1", tags=["platform"], responses=ERROR_RESPONSES)


@router.post(
    "/auth/login",
    response_model=PlatformTokenResponse,
    responses={429: {"model": ErrorResponse}},
)
async def platform_login(
    payload: PlatformLoginRequest,
    request: Request,
    session: PlatformDb,
    settings: Annotated[Settings, Depends(get_app_settings)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> PlatformTokenResponse:
    account = f"platform:{payload.username}"
    async with login_attempt(limiter, ip=client_ip(request), account=account):
        user = await service.authenticate_platform_user(
            session, username=payload.username, password=payload.password
        )
    token = encode_platform_token(
        user_id=user.id,
        secret=settings.platform_jwt_secret.get_secret_value(),
        ttl_seconds=settings.platform_token_ttl_seconds,
    )
    return PlatformTokenResponse(access_token=token, expires_in=settings.platform_token_ttl_seconds)


@router.get("/me", response_model=PlatformMe)
async def platform_me(user: CurrentPlatformUser) -> PlatformMe:
    return PlatformMe.model_validate(user)


@router.get("/tenants", response_model=TenantList)
async def list_tenants(session: PlatformDb, _: CurrentPlatformUser) -> TenantList:
    tenants = await service.list_tenants(session)
    return TenantList(items=[TenantOut.model_validate(t) for t in tenants])


@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def provision_tenant(
    payload: TenantCreate, request: Request, session: PlatformDb, user: CurrentPlatformUser
) -> TenantOut:
    """开通租户：创建租户、系统角色和首个租户管理员。"""
    tenant = await service.provision_tenant(
        session, payload, actor_id=user.id, ip=client_ip(request)
    )
    return TenantOut.model_validate(tenant)


@router.get("/tenants/{tenant_id}", response_model=TenantOut)
async def get_tenant(tenant_id: UUID, session: PlatformDb, _: CurrentPlatformUser) -> TenantOut:
    return TenantOut.model_validate(await service.get_tenant(session, tenant_id))


@router.patch("/tenants/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: UUID,
    payload: TenantUpdate,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
) -> TenantOut:
    """修改名称或状态。停用后，该租户员工的现有令牌在下一次请求时失效。"""
    tenant = await service.update_tenant(
        session, tenant_id, payload, actor_id=user.id, ip=client_ip(request)
    )
    return TenantOut.model_validate(tenant)
