from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, Depends, Request, Response, status

from app.context import AppContext
from app.core.config import Settings
from app.core.dates import today
from app.core.deps import client_ip, get_app_settings, get_context, get_rate_limiter
from app.core.errors import ERROR_RESPONSES, ErrorResponse, Unauthorized
from app.core.ratelimit import RateLimiter, login_attempt
from app.core.security import (
    TokenError,
    decode_platform_refresh_token,
    encode_platform_refresh_token,
    encode_platform_token,
)
from app.modules.iam.schemas import PasswordResetResult
from app.modules.tenancy import admins, mfa, service
from app.modules.tenancy.deps import CurrentPlatformUser, PlatformDb, PlatformUserForSetup
from app.modules.tenancy.models import PlatformUser, PlatformUserStatus
from app.modules.tenancy.schemas import (
    AdminPasswordReset,
    MfaCode,
    MfaDisable,
    MfaSetupOut,
    PlatformLoginRequest,
    PlatformMe,
    PlatformTokenResponse,
    TenantAdminList,
    TenantCreate,
    TenantList,
    TenantOut,
    TenantUpdate,
)

router = APIRouter(prefix="/platform/v1", tags=["platform"], responses=ERROR_RESPONSES)

# 运营后台的刷新令牌放在 httpOnly Cookie 里，只随 /platform/v1/auth 下的请求发送。
REFRESH_COOKIE = "edp_platform_refresh"
REFRESH_COOKIE_PATH = "/platform/v1/auth"
RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE, include_in_schema=False)]
SESSION_EXPIRED = "登录已失效，请重新登录"


def _set_refresh_cookie(response: Response, settings: Settings, token: str, max_age: int) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )


def _token_response(settings: Settings, user: PlatformUser) -> PlatformTokenResponse:
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


@router.post(
    "/auth/login",
    response_model=PlatformTokenResponse,
    responses={429: {"model": ErrorResponse}},
)
async def platform_login(
    payload: PlatformLoginRequest,
    request: Request,
    response: Response,
    session: PlatformDb,
    settings: Annotated[Settings, Depends(get_app_settings)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> PlatformTokenResponse:
    """运营人员登录。Access Token 在响应体中返回，刷新令牌写入 httpOnly Cookie。"""
    account = f"platform:{payload.username}"
    async with login_attempt(limiter, ip=client_ip(request), account=account):
        user = await service.authenticate_platform_user(
            session, username=payload.username, password=payload.password
        )
        await mfa.verify_login(settings, request.app.state.redis, user, payload.otp)
    refresh = encode_platform_refresh_token(
        user_id=user.id,
        secret=settings.platform_jwt_secret.get_secret_value(),
        ttl_seconds=settings.platform_refresh_ttl_seconds,
    )
    _set_refresh_cookie(response, settings, refresh, settings.platform_refresh_ttl_seconds)
    return _token_response(settings, user)


@router.post("/auth/refresh", response_model=PlatformTokenResponse)
async def platform_refresh(
    response: Response,
    session: PlatformDb,
    settings: Annotated[Settings, Depends(get_app_settings)],
    refresh_token: RefreshCookie = None,
) -> PlatformTokenResponse:
    """用 Cookie 里的刷新令牌换取新的 Access Token：页面刷新后不用重新登录。

    刷新不延长登录：登录后最多 platform_refresh_ttl_seconds（默认 12 小时）要重新登录。
    """
    if not refresh_token:
        raise Unauthorized(SESSION_EXPIRED)
    try:
        claims = decode_platform_refresh_token(
            refresh_token, secret=settings.platform_jwt_secret.get_secret_value()
        )
    except TokenError as exc:
        raise Unauthorized(SESSION_EXPIRED) from exc
    user = await session.get(PlatformUser, claims.user_id)
    if user is None or user.status != PlatformUserStatus.ACTIVE:
        raise Unauthorized(SESSION_EXPIRED)
    remaining = int((claims.expires_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        raise Unauthorized(SESSION_EXPIRED)
    refresh = encode_platform_refresh_token(
        user_id=user.id,
        secret=settings.platform_jwt_secret.get_secret_value(),
        ttl_seconds=remaining,
    )
    _set_refresh_cookie(response, settings, refresh, remaining)
    return _token_response(settings, user)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def platform_logout(settings: Annotated[Settings, Depends(get_app_settings)]) -> Response:
    """退出登录：清除刷新令牌 Cookie（Access Token 由前端丢弃）。"""
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


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


@router.get("/tenants/{tenant_id}/admins", response_model=TenantAdminList)
async def list_tenant_admins(
    tenant_id: UUID, session: PlatformDb, _: CurrentPlatformUser
) -> TenantAdminList:
    """企业的管理员账号（有租户管理员角色的员工），企业拥有者（开通企业时创建的账号）在前（§38.2）。"""
    tenant = await service.get_tenant(session, tenant_id)
    return TenantAdminList(items=await admins.list_admins(session, tenant))


@router.post("/tenants/{tenant_id}/admins/{staff_id}/password", response_model=PasswordResetResult)
async def reset_tenant_admin_password(
    tenant_id: UUID,
    staff_id: UUID,
    payload: AdminPasswordReset,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: Annotated[AppContext, Depends(get_context)],
) -> PasswordResetResult:
    """重置企业管理员的密码（§38.3），必须填写原因。

    新密码不填时自动生成（只返回这一次）。新密码是临时密码：管理员登录后要先设置新密码；现有的登录
    全部失效。记入企业的操作日志，企业的其他管理员收到提醒。
    """
    tenant = await service.get_tenant(session, tenant_id)
    return await admins.reset_password(
        ctx, session, tenant, staff_id, payload, actor=user, ip=client_ip(request)
    )
