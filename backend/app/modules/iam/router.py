from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, Depends, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import select

from app.context import AppContext
from app.core.config import Settings
from app.core.consoles import DEFAULT_MENUS, PROFILE_LABELS, PROFILE_PERMISSIONS, ConsoleProfile
from app.core.dates import today
from app.core.deps import (
    client_ip,
    get_app_settings,
    get_context,
    get_database,
    get_rate_limiter,
)
from app.core.errors import ERROR_RESPONSES, ErrorResponse, Unauthorized, Unprocessable
from app.core.permissions import ALL_PERMISSIONS, Permission
from app.core.ratelimit import PASSWORD_CHECK, RateLimiter, login_attempt
from app.core.security import RefreshClaims, TokenError, decode_refresh_token
from app.db.session import Database
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import entitlements
from app.modules.billing.service import billing_notice
from app.modules.iam import access, manage, passwords, service
from app.modules.iam import console as consoles
from app.modules.iam.deps import PrincipalForPasswordChange, TenantDb, require_permission
from app.modules.iam.models import Role, Staff
from app.modules.iam.principal import Principal
from app.modules.iam.schemas import (
    ConsoleOut,
    ConsoleProfileMenus,
    ConsoleSettingsIn,
    ConsoleSettingsOut,
    LoginRequest,
    MePlan,
    MeResponse,
    PasswordChange,
    PasswordReset,
    PasswordResetResult,
    PermissionList,
    ProfilePermissionList,
    ProfilePermissions,
    RoleCreate,
    RoleList,
    RoleOut,
    RoleUpdate,
    StaffAccessDefaults,
    StaffCreate,
    StaffList,
    StaffOut,
    StaffUpdate,
    TenantBrief,
    TokenResponse,
)
from app.modules.todos import sla

REFRESH_COOKIE = "edp_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"

auth_router = APIRouter(prefix="/api/v1/auth", tags=["auth"], responses=ERROR_RESPONSES)
router = APIRouter(prefix="/api/v1", tags=["iam"], responses=ERROR_RESPONSES)

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DatabaseDep = Annotated[Database, Depends(get_database)]
LimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]
RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE, include_in_schema=False)]
ContextDep = Annotated[AppContext, Depends(get_context)]
CanReadStaff = Annotated[Principal, Depends(require_permission(Permission.STAFF_READ))]
CanManageStaff = Annotated[Principal, Depends(require_permission(Permission.STAFF_MANAGE))]
CanManageSettings = Annotated[Principal, Depends(require_permission(Permission.SETTINGS_MANAGE))]


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
async def me(
    principal: PrincipalForPasswordChange, session: TenantDb, settings: SettingsDep
) -> MeResponse:
    """当前员工的信息、权限和菜单。密码被重置后还没有设置新密码时也可以调用（§38.5）。"""
    entitled = await entitlements(session, principal.tenant_id)
    sub, plan = entitled.subscription, entitled.plan
    roles = await service.roles_of(session, principal.staff_id)
    staff = await session.get(Staff, principal.staff_id)
    assert staff is not None
    profiles = consoles.profiles_for(roles, principal.permissions)
    console_settings = await consoles.load_settings(session, principal.tenant_id)
    # 按员工设置的页面和登录后打开的页面（§31）。
    menus = consoles.menus_for(
        profiles,
        console_settings,
        principal.permissions,
        entitled.features,
        access.own_menus(staff, roles),
    )
    home = access.home_menu(staff, roles)
    days_left, notice = billing_notice(sub, [], today(ZoneInfo(settings.usage_timezone)))
    return MeResponse(
        id=principal.staff_id,
        username=principal.username,
        display_name=principal.display_name,
        tenant=TenantBrief(
            id=principal.tenant_id,
            code=principal.tenant_code,
            name=principal.tenant_name,
            timezone=sla.tz_of(await sla.business_hours(session)).key,
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
        console=ConsoleOut(profiles=profiles, menus=menus, home=home if home in menus else None),
        must_change_password=staff.must_change_password,
        password_reset=await passwords.last_reset(session, staff)
        if staff.must_change_password
        else None,
    )


@router.post(
    "/me/password",
    response_model=TokenResponse,
    responses={429: {"model": ErrorResponse}},
)
async def change_password(
    payload: PasswordChange,
    request: Request,
    response: Response,
    principal: PrincipalForPasswordChange,
    session: TenantDb,
    settings: SettingsDep,
    limiter: LimiterDep,
) -> TokenResponse:
    """修改自己的密码。其他设备上的登录随即失效，当前页面换发新的令牌。

    管理员或平台运维人员重置了密码时，用重置的密码作为当前密码，设置后才能使用控制台（§38.5）。
    """
    await limiter.check(PASSWORD_CHECK, str(principal.staff_id))
    tokens = await manage.change_own_password(
        session, settings, principal, payload, ip=client_ip(request)
    )
    set_refresh_cookie(response, settings, tokens.refresh_token)
    return TokenResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)


@router.get("/permissions", response_model=PermissionList)
async def list_permissions(_: CanReadStaff) -> PermissionList:
    """全部权限点及其名称、分组（编辑自定义角色时使用）。"""
    return PermissionList(items=manage.permission_catalog())


@router.get("/roles", response_model=RoleList)
async def list_roles(session: TenantDb, _: CanReadStaff) -> RoleList:
    roles = await service.list_roles(session)
    members = await manage.role_members(session)
    return RoleList(items=[manage.role_out(role, members[role.id]) for role in roles])


@router.get("/roles/profile-permissions", response_model=ProfilePermissionList)
async def profile_permissions(_: CanReadStaff) -> ProfilePermissionList:
    """每个岗位的默认权限：新建自定义角色时选了岗位可以一键填入（设计文档 §28.5）。"""
    return ProfilePermissionList(
        items=[
            ProfilePermissions(
                profile=profile,
                label=PROFILE_LABELS[profile],
                permissions=sorted(PROFILE_PERMISSIONS[profile]),
            )
            for profile in ConsoleProfile
        ]
    )


@router.post("/roles", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
async def create_role(
    payload: RoleCreate, request: Request, session: TenantDb, principal: CanManageStaff
) -> RoleOut:
    """新建自定义角色（权限不能超出自己拥有的权限）。"""
    return await manage.create_role(session, principal, payload, ip=client_ip(request))


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update_role(
    role_id: UUID,
    payload: RoleUpdate,
    request: Request,
    session: TenantDb,
    principal: CanManageStaff,
) -> RoleOut:
    """修改自定义角色的名称和权限（系统角色不能修改）。"""
    return await manage.update_role(session, principal, role_id, payload, ip=client_ip(request))


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(
    role_id: UUID, request: Request, session: TenantDb, principal: CanManageStaff
) -> Response:
    """删除没有员工使用的自定义角色。"""
    await manage.delete_role(session, principal, role_id, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _console_out(value: consoles.ConsoleSettings) -> ConsoleSettingsOut:
    items = []
    for profile in ConsoleProfile:
        defaults = list(DEFAULT_MENUS[profile])
        menus = value.menus.get(profile, defaults)
        items.append(
            ConsoleProfileMenus(
                profile=profile,
                label=PROFILE_LABELS[profile],
                menus=menus,
                defaults=defaults,
                customized=profile in value.menus,
                editable=profile != ConsoleProfile.ADMIN,
            )
        )
    return ConsoleSettingsOut(items=items)


@router.get("/tenant/console", response_model=ConsoleSettingsOut)
async def get_console(session: TenantDb, principal: CanManageSettings) -> ConsoleSettingsOut:
    """每个岗位显示的菜单（§25.15）：管理员固定看全部，其他岗位可以调整。"""
    return _console_out(await consoles.load_settings(session, principal.tenant_id))


@router.put("/tenant/console", response_model=ConsoleSettingsOut)
async def put_console(
    payload: ConsoleSettingsIn, request: Request, session: TenantDb, principal: CanManageSettings
) -> ConsoleSettingsOut:
    """调整岗位显示的菜单（没有列出的岗位恢复默认）。菜单还要有相应的权限才会显示。"""
    try:
        value = consoles.ConsoleSettings(menus=payload.menus)
    except ValidationError as exc:
        raise Unprocessable(str(exc.errors()[0]["msg"]).removeprefix("Value error, ")) from exc
    await consoles.save_settings(session, principal.tenant_id, value, principal.staff_id)
    record_audit(
        session,
        action="console.update",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="tenant",
        resource_id=str(principal.tenant_id),
        detail=value.model_dump(mode="json"),
        ip=client_ip(request),
    )
    await session.commit()
    return _console_out(value)


@router.get("/staff", response_model=StaffList)
async def list_staff(session: TenantDb, _: CanReadStaff) -> StaffList:
    return StaffList(items=await service.list_staff(session))


@router.get("/staff/access-defaults", response_model=StaffAccessDefaults)
async def staff_access_defaults(
    session: TenantDb,
    principal: CanManageStaff,
    role_codes: Annotated[list[str], Query(min_length=1)],
) -> StaffAccessDefaults:
    """这些角色给的页面和权限：新建、编辑员工时选"自定义"的起点（§31）。"""
    requested = set(role_codes)
    roles = list((await session.scalars(select(Role).where(Role.code.in_(requested)))).all())
    missing = requested - {role.code for role in roles}
    if missing:
        raise Unprocessable(f"角色不存在：{'、'.join(sorted(missing))}")
    granted = service.granted_by(roles)
    profiles = consoles.profiles_for(roles, granted)
    entitled = await entitlements(session, principal.tenant_id)
    return StaffAccessDefaults(
        profiles=profiles,
        menus=consoles.menus_for(
            profiles,
            await consoles.load_settings(session, principal.tenant_id),
            granted,
            entitled.features,
        ),
        permissions=access.known(granted),
        adjustable=access.adjustable(roles),
    )


@router.post("/staff", response_model=StaffOut, status_code=status.HTTP_201_CREATED)
async def create_staff(
    payload: StaffCreate, request: Request, session: TenantDb, principal: CanManageStaff
) -> StaffOut:
    return await service.create_staff(session, principal, payload, ip=client_ip(request))


@router.patch("/staff/{staff_id}", response_model=StaffOut)
async def update_staff(
    staff_id: UUID,
    payload: StaffUpdate,
    request: Request,
    ctx: ContextDep,
    session: TenantDb,
    principal: CanManageStaff,
) -> StaffOut:
    """修改员工的名称、角色或状态。停用后立即退出登录并下线，接待中的会话退回队列。"""
    return await manage.update_staff(
        ctx, session, principal, staff_id, payload, ip=client_ip(request)
    )


@router.post("/staff/{staff_id}/password", response_model=PasswordResetResult)
async def reset_staff_password(
    staff_id: UUID,
    payload: PasswordReset,
    request: Request,
    ctx: ContextDep,
    session: TenantDb,
    principal: CanManageStaff,
) -> PasswordResetResult:
    """重置员工的密码（§38.4）：适用于全部角色，不能重置权限高于自己的员工，也不能重置自己的。

    新密码不填时自动生成（只返回这一次）；默认要求员工下次登录时先设置新密码。员工现有的登录全部
    失效。
    """
    return await manage.reset_password(
        ctx, session, principal, staff_id, payload, ip=client_ip(request)
    )
