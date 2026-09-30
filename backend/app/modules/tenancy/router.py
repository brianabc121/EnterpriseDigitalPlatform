from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, status

from app.core.config import Settings
from app.core.dates import today
from app.core.deps import client_ip, get_app_settings, get_rate_limiter
from app.core.errors import ERROR_RESPONSES, ErrorResponse
from app.core.ratelimit import RateLimiter, login_attempt
from app.core.security import encode_platform_token
from app.modules.tenancy import mfa, service
from app.modules.tenancy.deps import CurrentPlatformUser, PlatformDb, PlatformUserForSetup
from app.modules.tenancy.schemas import (
    MfaCode,
    MfaDisable,
    MfaSetupOut,
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
        await mfa.verify_login(settings, request.app.state.redis, user, payload.otp)
    token = encode_platform_token(
        user_id=user.id,
        secret=settings.platform_jwt_secret.get_secret_value(),
        ttl_seconds=settings.platform_token_ttl_seconds,
    )
    return PlatformTokenResponse(
        access_token=token,
        expires_in=settings.platform_token_ttl_seconds,
        mfa_setup_required=settings.platform_mfa_enforced and user.mfa_enabled_at is None,
    )


@router.get("/me", response_model=PlatformMe)
async def platform_me(
    user: PlatformUserForSetup, settings: Annotated[Settings, Depends(get_app_settings)]
) -> PlatformMe:
    out = PlatformMe.model_validate(user)
    out.mfa_enabled = user.mfa_enabled_at is not None
    out.mfa_required = settings.platform_mfa_enforced
    return out


@router.post("/auth/mfa/setup", response_model=MfaSetupOut)
async def mfa_setup(
    session: PlatformDb,
    user: PlatformUserForSetup,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> MfaSetupOut:
    """生成二次验证密钥（尚未启用）：用验证器应用扫描 otpauth 二维码后，输入验证码启用。"""
    return await mfa.setup(session, settings, user)


@router.post("/auth/mfa/enable", status_code=status.HTTP_204_NO_CONTENT)
async def mfa_enable(
    payload: MfaCode,
    request: Request,
    session: PlatformDb,
    user: PlatformUserForSetup,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> None:
    await mfa.enable(
        session, settings, request.app.state.redis, user, payload.code, ip=client_ip(request)
    )


@router.post("/auth/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def mfa_disable(
    payload: MfaDisable,
    request: Request,
    session: PlatformDb,
    user: PlatformUserForSetup,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> None:
    """关闭二次验证（需要密码和当前验证码）。平台要求二次验证时，关闭后要重新设置才能继续使用。"""
    await mfa.disable(
        session,
        settings,
        request.app.state.redis,
        user,
        password=payload.password,
        code=payload.code,
        ip=client_ip(request),
    )


@router.get("/tenants", response_model=TenantList)
async def list_tenants(session: PlatformDb, _: CurrentPlatformUser) -> TenantList:
    tenants = await service.list_tenants(session)
    return TenantList(items=await service.tenant_outs(session, tenants))


@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def provision_tenant(
    payload: TenantCreate,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> TenantOut:
    """开通租户：创建租户、系统角色和首个租户管理员；指定套餐时开始试用或正式订阅。"""
    tenant = await service.provision_tenant(
        session,
        payload,
        actor_id=user.id,
        ip=client_ip(request),
        current_day=today(ZoneInfo(settings.usage_timezone)),
    )
    return (await service.tenant_outs(session, [tenant]))[0]


@router.get("/tenants/{tenant_id}", response_model=TenantOut)
async def get_tenant(tenant_id: UUID, session: PlatformDb, _: CurrentPlatformUser) -> TenantOut:
    tenant = await service.get_tenant(session, tenant_id)
    return (await service.tenant_outs(session, [tenant]))[0]


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
    return (await service.tenant_outs(session, [tenant]))[0]
