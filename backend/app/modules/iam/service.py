from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Conflict, Forbidden, Unauthorized, Unprocessable
from app.core.ids import new_id
from app.core.permissions import DEFAULT_ROLES, TENANT_ADMIN_ROLE
from app.core.security import (
    AccessClaims,
    RefreshClaims,
    encode_access_token,
    encode_refresh_token,
    hash_password,
    password_stamp,
    verify_password,
)
from app.db.errors import violated_unique_constraint
from app.db.session import bind_tenant
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import check_limit
from app.modules.iam import access, diagram, owner
from app.modules.iam.models import RefreshToken, Role, Staff, StaffRole, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.iam.schemas import StaffCreate, StaffOut
from app.modules.tenancy.models import Tenant, TenantStatus
from app.modules.warehouse import settings as warehouse_settings

_SYSTEM_ROLES = {spec.code: spec for spec in DEFAULT_ROLES}


def role_permissions(role: Role) -> frozenset[str]:
    """角色的有效权限：系统角色以代码中的定义为准（新增权限点后现有租户自动生效），
    自定义角色以数据库中保存的为准。"""
    spec = _SYSTEM_ROLES.get(role.code) if role.is_system else None
    return frozenset(spec.permissions) if spec else frozenset(role.permissions)


def granted_by(roles: Iterable[Role]) -> frozenset[str]:
    """角色给的权限（并集）。"""
    return frozenset(p for role in roles for p in role_permissions(role))


def effective_permissions(staff: Staff, roles: Iterable[Role]) -> frozenset[str]:
    """员工被授予的权限（设计文档 §31.4）：角色权限的并集，加上多给的、减去去掉的；租户管理员只看
    角色。不含仓管另外获得的确认权限（见 principal_for）。"""
    roles = list(roles)
    granted = granted_by(roles)
    return access.grant(granted, staff) if access.adjustable(roles) else granted


async def roles_of(session: AsyncSession, staff_id: UUID) -> list[Role]:
    return list(
        (
            await session.scalars(
                select(Role)
                .join(StaffRole, StaffRole.role_id == Role.id)
                .where(StaffRole.staff_id == staff_id)
                .order_by(Role.code)
            )
        ).all()
    )


async def staff_permissions(session: AsyncSession, staff: Staff) -> frozenset[str]:
    """员工被授予的权限（不含仓管另外获得的）。"""
    return effective_permissions(staff, await roles_of(session, staff.id))


async def _roles_by_staff(session: AsyncSession) -> dict[UUID, list[Role]]:
    roles = {role.id: role for role in await session.scalars(select(Role))}
    held: dict[UUID, list[Role]] = defaultdict(list)
    for staff_id, role_id in await session.execute(select(StaffRole.staff_id, StaffRole.role_id)):
        if role_id in roles:
            held[staff_id].append(roles[role_id])
    return held


async def active_staff_permissions(session: AsyncSession) -> list[tuple[Staff, frozenset[str]]]:
    """启用状态的员工和各自被授予的权限（不含仓管另外获得的），按创建的先后。用于"有某项权限的
    员工"（通知对象、必读知识的读者等）。"""
    held = await _roles_by_staff(session)
    staff = await session.scalars(
        select(Staff).where(Staff.status == StaffStatus.ACTIVE).order_by(Staff.created_at)
    )
    return [(s, effective_permissions(s, held[s.id])) for s in staff.all()]


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
            password_stamp=password_stamp(staff.password_changed_at),
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
    return await principal_for(session, claims.tenant_id, claims.staff_id, token=claims)


async def principal_for(
    session: AsyncSession, tenant_id: UUID, staff_id: UUID, *, token: AccessClaims | None = None
) -> Principal | None:
    """员工当前的身份与权限（后台任务以发起人的身份执行时也用它）。停用或租户不可用时为空；
    给了访问令牌时，令牌签发之后密码修改或重置过也为空（§38.6）。"""
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.status != TenantStatus.ACTIVE:
        return None
    staff = await session.get(Staff, staff_id)
    if staff is None or staff.status != StaffStatus.ACTIVE:
        return None
    if token is not None and token.password_stamp != password_stamp(staff.password_changed_at):
        return None
    roles = await roles_of(session, staff.id)
    permissions = effective_permissions(staff, roles)
    if not permissions >= warehouse_settings.KEEPER_PERMISSIONS:
        # 仓管（设置里指定的员工，或者最早创建的工人）另外可以确认单据、调整库存（§25.13）。
        permissions |= await warehouse_settings.keeper_permissions(session, tenant.id, staff.id)
    return Principal(
        staff_id=staff.id,
        tenant_id=tenant.id,
        tenant_code=tenant.code,
        tenant_name=tenant.name,
        username=staff.username,
        display_name=staff.display_name,
        role_codes=tuple(role.code for role in roles),
        permissions=permissions,
        must_change_password=staff.must_change_password,
    )


async def list_roles(session: AsyncSession) -> list[Role]:
    return list((await session.scalars(select(Role).order_by(Role.created_at, Role.code))).all())


def staff_out(staff: Staff, roles: Iterable[Role], *, owner: bool = False) -> StaffOut:
    roles = list(roles)
    return StaffOut(
        id=staff.id,
        is_owner=owner,
        diagram_parent_id=staff.diagram_parent_id,
        diagram_direction=staff.diagram_direction,
        username=staff.username,
        display_name=staff.display_name,
        status=staff.status,
        roles=sorted(role.code for role in roles),
        created_at=staff.created_at,
        access=access.out(staff, roles),
        permissions=access.known(effective_permissions(staff, roles)),
        must_change_password=staff.must_change_password,
        password_changed_at=staff.password_changed_at,
    )


async def list_staff(session: AsyncSession) -> list[StaffOut]:
    staff_rows = (await session.scalars(select(Staff).order_by(Staff.created_at, Staff.id))).all()
    held = await _roles_by_staff(session)
    # 企业所有者：有这个角色的员工里最早创建的（§39.5，和 owner.owner_id 一致）。
    owner_id = next(
        (s.id for s in staff_rows if any(r.code == TENANT_ADMIN_ROLE for r in held[s.id])), None
    )
    return [staff_out(s, held[s.id], owner=s.id == owner_id) for s in staff_rows]


async def create_staff(
    session: AsyncSession, principal: Principal, payload: StaffCreate, *, ip: str | None
) -> StaffOut:
    layout_node = (
        await diagram.pending_node(session, principal.tenant_id, payload.diagram_node_id)
        if payload.diagram_node_id is not None
        else None
    )
    if payload.diagram_parent_id is not None:
        parent = await session.scalar(
            select(Staff.id).where(
                Staff.id == payload.diagram_parent_id,
                Staff.tenant_id == principal.tenant_id,
            )
        )
        if parent is None:
            raise Unprocessable("来源员工不存在或不属于当前企业")
    requested = set(payload.role_codes)
    owner.check_assignable(requested)
    roles = (await session.scalars(select(Role).where(Role.code.in_(requested)))).all()
    missing = requested - {role.code for role in roles}
    if missing:
        raise Unprocessable(f"角色不存在：{'、'.join(sorted(missing))}")
    # 不能把超出自己权限的角色分配给别人（防止越权提权）。
    if any(not role_permissions(role) <= principal.permissions for role in roles):
        raise Forbidden("不能分配超出自身权限的角色")
    staff = Staff(
        id=new_id(),
        tenant_id=principal.tenant_id,
        diagram_parent_id=payload.diagram_parent_id,
        diagram_direction=payload.diagram_direction,
        username=payload.username,
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
    )
    # 按员工设置的页面和权限（§31）：多给的也不能超出自己的权限。
    access.apply(
        staff,
        payload.access,
        roles=roles,
        granted=granted_by(roles),
        operator=principal.permissions,
    )
    if await session.scalar(select(Staff.id).where(Staff.username == payload.username)):
        raise Conflict("用户名已存在")
    await check_limit(session, principal.tenant_id, "seats")
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
    if layout_node is not None:
        layout_node.staff_id = staff.id
    detail: dict[str, object] = {"username": staff.username, "roles": sorted(requested)}
    if layout_node is not None:
        detail["diagram_node_id"] = str(layout_node.id)
    if payload.diagram_direction is not None:
        detail["diagram"] = {
            "parent_id": str(payload.diagram_parent_id) if payload.diagram_parent_id else None,
            "direction": payload.diagram_direction,
        }
    if (saved := access.snapshot(staff)) is not None:
        detail["access"] = saved
    record_audit(
        session,
        action="staff.create",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="staff",
        resource_id=str(staff.id),
        detail=detail,
        ip=ip,
    )
    await session.commit()
    await session.refresh(staff)
    return staff_out(staff, roles)
