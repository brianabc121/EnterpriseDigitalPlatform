from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, Depends, Request, Response, status

from app.core.config import Settings
from app.core.dates import today
from app.core.deps import client_ip, get_app_settings, get_database, get_rate_limiter
from app.core.errors import ERROR_RESPONSES, ErrorResponse, Unauthorized
from app.core.permissions import ALL_PERMISSIONS, Permission
from app.core.ratelimit import RateLimiter, login_attempt
from app.core.security import RefreshClaims, TokenError, decode_refresh_token
from app.db.session import Database
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import entitlements
from app.modules.billing.service import billing_notice
from app.modules.iam import service
from app.modules.iam.deps import CurrentPrincipal, TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.iam.schemas import (
    LoginRequest,
    MePlan,
    MeResponse,
    RoleList,
    RoleOut,
    StaffCreate,
    StaffList,
    StaffOut,
    TenantBrief,
    TokenResponse,
)

REFRESH_COOKIE = "edp_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"

auth_router = APIRouter(prefix="/api/v1/auth", tags=["auth"], responses=ERROR_RESPONSES)
router = APIRouter(prefix="/api/v1", tags=["iam"], responses=ERROR_RESPONSES)

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE, include_in_schema=False)]


def set_refresh_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )


def _decode_refresh(settings: Settings, token: str | None) -> RefreshClaims | None:
    if not token:
        return None
    try:
        return decode_refresh_token(token, secret=settings.jwt_secret.get_secret_value())
    except TokenError:
        return None


@auth_router.post("/login", response_model=TokenResponse, responses={429: {"model": ErrorResponse}})
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DatabaseDep,
    settings: SettingsDep,
    limiter: LimiterDep,
) -> TokenResponse:
    """员工登录。Access Token 在响应体中返回，Refresh Token 写入 httpOnly Cookie。"""
    account = f"{payload.tenant_code.lower()}:{payload.username}"
    async with (
        db.app_sessionmaker() as session,
        login_attempt(limiter, ip=client_ip(request), account=account),
    ):
        staff = await service.authenticate(
            session,
            tenant_code=payload.tenant_code,
            username=payload.username,
            password=payload.password,
        )
        tokens, _ = service.issue_tokens(session, settings, staff)
        record_audit(
            session,
            action="auth.login",
            actor_type="staff",
            actor_id=staff.id,
            tenant_id=staff.tenant_id,
            ip=client_ip(request),
        )
        await session.commit()
    set_refresh_cookie(response, settings, tokens.refresh_token)
    return TokenResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)


@auth_router.post("/refresh", response_model=TokenResponse)
async def refresh(
    response: Response,
    db: DatabaseDep,
    settings: SettingsDep,
    refresh_token: RefreshCookie = None,
) -> TokenResponse:
    """用 Cookie 中的 Refresh Token 换取新的令牌对（旧的随即失效）。"""
    claims = _decode_refresh(settings, refresh_token)
    if claims is None:
        raise Unauthorized(service.SESSION_EXPIRED)
    async with db.tenant_session(claims.tenant_id) as session:
        tokens = await service.rotate_refresh_token(session, settings, claims)
    set_refresh_cookie(response, settings, tokens.refresh_token)
    return TokenResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    db: DatabaseDep, settings: SettingsDep, refresh_token: RefreshCookie = None
) -> Response:
    claims = _decode_refresh(settings, refresh_token)
    if claims is not None:
        async with db.tenant_session(claims.tenant_id) as session:
            await service.revoke_session(session, claims)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/me", response_model=MeResponse)
async def me(principal: CurrentPrincipal, session: TenantDb, settings: SettingsDep) -> MeResponse:
    entitled = await entitlements(session, principal.tenant_id)
    sub, plan = entitled.subscription, entitled.plan
    days_left, notice = billing_notice(sub, [], today(ZoneInfo(settings.usage_timezone)))
    return MeResponse(
        id=principal.staff_id,
        username=principal.username,
        display_name=principal.display_name,
        tenant=TenantBrief(
            id=principal.tenant_id, code=principal.tenant_code, name=principal.tenant_name
        ),
        roles=list(principal.role_codes),
        # 自定义角色里可能残留已下线的权限点，只返回当前版本认识的。
        permissions=sorted(Permission(p) for p in principal.permissions if p in ALL_PERMISSIONS),
        features=dict(entitled.features),
        plan=MePlan(
            code=plan.code,
            name=plan.name,
            status=sub.status,
            period_end=sub.period_end,
            days_left=days_left or 0,
        )
        if sub is not None and plan is not None
        else None,
        billing_notice=notice if principal.has(Permission.SETTINGS_MANAGE) else None,
    )


@router.get("/roles", response_model=RoleList)
async def list_roles(
    session: TenantDb,
    _: Annotated[Principal, Depends(require_permission(Permission.STAFF_READ))],
) -> RoleList:
    roles = await service.list_roles(session)
    return RoleList(
        items=[
            RoleOut(
                id=role.id,
                code=role.code,
                name=role.name,
                permissions=sorted(service.role_permissions(role)),
                is_system=role.is_system,
            )
            for role in roles
        ]
    )


@router.get("/staff", response_model=StaffList)
async def list_staff(
    session: TenantDb,
    _: Annotated[Principal, Depends(require_permission(Permission.STAFF_READ))],
) -> StaffList:
    return StaffList(items=await service.list_staff(session))


@router.post("/staff", response_model=StaffOut, status_code=status.HTTP_201_CREATED)
async def create_staff(
    payload: StaffCreate,
    request: Request,
    session: TenantDb,
    principal: Annotated[Principal, Depends(require_permission(Permission.STAFF_MANAGE))],
) -> StaffOut:
    return await service.create_staff(session, principal, payload, ip=client_ip(request))
