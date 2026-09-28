from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Conflict, Forbidden, Unauthorized, Unprocessable
from app.core.ids import new_id
from app.core.security import (
    AccessClaims,
    RefreshClaims,
    encode_access_token,
    encode_refresh_token,
    hash_password,
    verify_password,
)
from app.db.errors import violated_unique_constraint
from app.db.session import bind_tenant
from app.modules.audit.service import record_audit
from app.modules.iam.models import RefreshToken, Role, Staff, StaffRole, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.iam.schemas import StaffCreate, StaffOut
from app.modules.tenancy.models import Tenant, TenantStatus

INVALID_CREDENTIALS = "企业代码、用户名或密码错误"
SESSION_EXPIRED = "登录已过期，请重新登录"
# 同一个刷新令牌在轮换后的短时间内再次出现，按"多个标签页同时刷新"处理，而不是按盗用处理。
REFRESH_REUSE_GRACE = timedelta(seconds=10)


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int


def _now() -> datetime:
    return datetime.now(UTC)


async def authenticate(
    session: AsyncSession, *, tenant_code: str, username: str, password: str
) -> Staff:
    tenant = await session.scalar(select(Tenant).where(Tenant.code == tenant_code.lower()))
    staff = None
    if tenant is not None:
        await bind_tenant(session, tenant.id)
        staff = await session.scalar(select(Staff).where(Staff.username == username))
    password_ok = verify_password(staff.password_hash if staff else None, password)
    if tenant is None or staff is None or not password_ok:
        raise Unauthorized(INVALID_CREDENTIALS)
    # 口令正确之后才提示停用状态，避免向未认证的请求暴露账号状态。
    if tenant.status != TenantStatus.ACTIVE:
        raise Forbidden("该企业账号已停用，请联系平台管理员")
    if staff.status != StaffStatus.ACTIVE:
        raise Forbidden("该账号已停用，请联系企业管理员")
    return staff


def issue_tokens(
    session: AsyncSession, settings: Settings, staff: Staff, *, family_id: UUID | None = None
) -> tuple[IssuedTokens, RefreshToken]:
    """签发一对令牌，并把刷新令牌记录加入当前事务（由调用方提交）。"""
    record = RefreshToken(
        id=new_id(),
        tenant_id=staff.tenant_id,
        staff_id=staff.id,
        family_id=family_id or new_id(),
        expires_at=_now() + timedelta(seconds=settings.refresh_token_ttl_seconds),
    )
    session.add(record)
    tokens = IssuedTokens(
        access_token=encode_access_token(
            staff_id=staff.id,
            tenant_id=staff.tenant_id,
            secret=settings.jwt_secret.get_secret_value(),
            ttl_seconds=settings.access_token_ttl_seconds,
        ),
        refresh_token=encode_refresh_token(
            staff_id=staff.id,
            tenant_id=staff.tenant_id,
            token_id=record.id,
            family_id=record.family_id,
            secret=settings.jwt_secret.get_secret_value(),
            ttl_seconds=settings.refresh_token_ttl_seconds,
        ),
        expires_in=settings.access_token_ttl_seconds,
    )
    return tokens, record


async def _revoke_family(session: AsyncSession, family_id: UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


async def rotate_refresh_token(
    session: AsyncSession, settings: Settings, claims: RefreshClaims
) -> IssuedTokens:
    """刷新令牌轮换。会话必须已绑定到 claims.tenant_id。"""
    current = await session.get(RefreshToken, claims.token_id, with_for_update=True)
    if current is None or current.staff_id != claims.staff_id:
        raise Unauthorized(SESSION_EXPIRED)

    now = _now()
    if current.revoked_at is not None:
        within_grace = (
            current.replaced_by is not None and now - current.revoked_at <= REFRESH_REUSE_GRACE
        )
        if not within_grace:
            # 已轮换的令牌被再次使用：可能已被盗用，吊销这次登录产生的全部令牌。
            await _revoke_family(session, current.family_id)
            await session.commit()
            raise Unauthorized(SESSION_EXPIRED)
    if current.expires_at <= now:
        raise Unauthorized(SESSION_EXPIRED)

    staff = await session.get(Staff, current.staff_id)
    tenant = await session.get(Tenant, claims.tenant_id)
    if (
        staff is None
        or staff.status != StaffStatus.ACTIVE
        or tenant is None
        or tenant.status != TenantStatus.ACTIVE
    ):
        await _revoke_family(session, current.family_id)
        await session.commit()
        raise Unauthorized(SESSION_EXPIRED)

    tokens, replacement = issue_tokens(session, settings, staff, family_id=current.family_id)
    if current.revoked_at is None:
        current.revoked_at = now
        current.replaced_by = replacement.id
    await session.commit()
    return tokens


async def revoke_session(session: AsyncSession, claims: RefreshClaims) -> None:
    await _revoke_family(session, claims.family_id)
    await session.commit()


async def load_principal(session: AsyncSession, claims: AccessClaims) -> Principal | None:
    tenant = await session.get(Tenant, claims.tenant_id)
    if tenant is None or tenant.status != TenantStatus.ACTIVE:
        return None
    staff = await session.get(Staff, claims.staff_id)
    if staff is None or staff.status != StaffStatus.ACTIVE:
        return None
    roles = (
        await session.scalars(
            select(Role)
            .join(StaffRole, StaffRole.role_id == Role.id)
            .where(StaffRole.staff_id == staff.id)
            .order_by(Role.code)
        )
    ).all()
    return Principal(
        staff_id=staff.id,
        tenant_id=tenant.id,
        tenant_code=tenant.code,
        tenant_name=tenant.name,
        username=staff.username,
        display_name=staff.display_name,
        role_codes=tuple(role.code for role in roles),
        permissions=frozenset(p for role in roles for p in role.permissions),
    )


async def list_roles(session: AsyncSession) -> list[Role]:
    return list((await session.scalars(select(Role).order_by(Role.created_at, Role.code))).all())


async def list_staff(session: AsyncSession) -> list[StaffOut]:
    staff_rows = (await session.scalars(select(Staff).order_by(Staff.created_at))).all()
    role_rows = await session.execute(
        select(StaffRole.staff_id, Role.code).join(Role, Role.id == StaffRole.role_id)
    )
    roles_by_staff: dict[UUID, list[str]] = defaultdict(list)
    for staff_id, code in role_rows:
        roles_by_staff[staff_id].append(code)
    return [
        StaffOut(
            id=s.id,
            username=s.username,
            display_name=s.display_name,
            status=s.status,
            roles=sorted(roles_by_staff[s.id]),
            created_at=s.created_at,
        )
        for s in staff_rows
    ]


async def create_staff(
    session: AsyncSession, principal: Principal, payload: StaffCreate, *, ip: str | None
) -> StaffOut:
    requested = set(payload.role_codes)
    roles = (await session.scalars(select(Role).where(Role.code.in_(requested)))).all()
    missing = requested - {role.code for role in roles}
    if missing:
        raise Unprocessable(f"角色不存在：{'、'.join(sorted(missing))}")
    # 不能把超出自己权限的角色分配给别人（防止越权提权）。
    if any(not set(role.permissions) <= principal.permissions for role in roles):
        raise Forbidden("不能分配超出自身权限的角色")
    if await session.scalar(select(Staff.id).where(Staff.username == payload.username)):
        raise Conflict("用户名已存在")

    staff = Staff(
        id=new_id(),
        tenant_id=principal.tenant_id,
        username=payload.username,
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
    )
    session.add(staff)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if violated_unique_constraint(exc) == "uq_staff_tenant_id_username":
            raise Conflict("用户名已存在") from exc
        raise
    session.add_all(
        StaffRole(tenant_id=principal.tenant_id, staff_id=staff.id, role_id=role.id)
        for role in roles
    )
    record_audit(
        session,
        action="staff.create",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="staff",
        resource_id=str(staff.id),
        detail={"username": staff.username, "roles": sorted(requested)},
        ip=ip,
    )
    await session.commit()
    await session.refresh(staff)
    return StaffOut(
        id=staff.id,
        username=staff.username,
        display_name=staff.display_name,
        status=staff.status,
        roles=sorted(requested),
        created_at=staff.created_at,
    )
