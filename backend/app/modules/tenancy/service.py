"""平台运营：平台账号与租户开通。这些函数使用 edp_platform 连接（可以访问所有租户的行）。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, NotFound, Unauthorized, Unprocessable
from app.core.ids import new_id
from app.core.permissions import DEFAULT_ROLES, TENANT_ADMIN_ROLE
from app.core.security import hash_password, verify_password
from app.db.errors import violated_unique_constraint
from app.modules.audit.service import record_audit
from app.modules.channels.service import default_web_channel
from app.modules.iam.models import Role, Staff, StaffRole
from app.modules.routing.models import RoutingPolicy
from app.modules.tenancy.models import PlatformUser, PlatformUserStatus, Tenant
from app.modules.tenancy.schemas import TenantCreate, TenantUpdate

MIN_PASSWORD_LENGTH = 8


async def authenticate_platform_user(
    session: AsyncSession, *, username: str, password: str
) -> PlatformUser:
    user = await session.scalar(select(PlatformUser).where(PlatformUser.username == username))
    password_ok = verify_password(user.password_hash if user else None, password)
    if user is None or not password_ok:
        raise Unauthorized("用户名或密码错误")
    if user.status != PlatformUserStatus.ACTIVE:
        raise Forbidden("该账号已停用")
    return user


async def create_platform_user(
    session: AsyncSession, *, username: str, display_name: str, password: str
) -> PlatformUser:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise Unprocessable(f"密码至少需要 {MIN_PASSWORD_LENGTH} 位")
    if await session.scalar(select(PlatformUser.id).where(PlatformUser.username == username)):
        raise Conflict("用户名已存在")
    user = PlatformUser(
        username=username, display_name=display_name, password_hash=hash_password(password)
    )
    session.add(user)
    await session.commit()
    return user


async def provision_tenant(
    session: AsyncSession, payload: TenantCreate, *, actor_id: UUID | None, ip: str | None
) -> Tenant:
    """在一个事务里创建租户、系统角色、默认 Web 渠道、默认路由策略和首个租户管理员。

    模型之间没有声明 relationship，ORM 不会按外键排序 INSERT，所以按依赖顺序逐步 flush。
    """
    if await session.scalar(select(Tenant.id).where(Tenant.code == payload.code)):
        raise Conflict("企业代码已被使用")

    tenant = Tenant(id=new_id(), code=payload.code, name=payload.name)
    session.add(tenant)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if violated_unique_constraint(exc) == "uq_tenants_code":
            raise Conflict("企业代码已被使用") from exc
        raise

    roles = {
        spec.code: Role(
            id=new_id(),
            tenant_id=tenant.id,
            code=spec.code,
            name=spec.name,
            permissions=sorted(spec.permissions),
            is_system=True,
        )
        for spec in DEFAULT_ROLES
    }
    session.add_all(roles.values())
    session.add(default_web_channel(tenant.id, tenant.code))
    session.add(RoutingPolicy(id=new_id(), tenant_id=tenant.id, name="默认策略", is_default=True))
    admin = Staff(
        id=new_id(),
        tenant_id=tenant.id,
        username=payload.admin.username,
        display_name=payload.admin.display_name,
        password_hash=hash_password(payload.admin.password),
    )
    session.add(admin)
    await session.flush()
    session.add(
        StaffRole(tenant_id=tenant.id, staff_id=admin.id, role_id=roles[TENANT_ADMIN_ROLE].id)
    )
    record_audit(
        session,
        action="tenant.provision",
        actor_type="platform",
        actor_id=actor_id,
        tenant_id=tenant.id,
        resource_type="tenant",
        resource_id=str(tenant.id),
        detail={"code": tenant.code, "admin_username": admin.username},
        ip=ip,
    )
    await session.commit()
    await session.refresh(tenant)
    return tenant


async def list_tenants(session: AsyncSession) -> list[Tenant]:
    return list((await session.scalars(select(Tenant).order_by(Tenant.created_at.desc()))).all())


async def get_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("租户不存在")
    return tenant


async def update_tenant(
    session: AsyncSession,
    tenant_id: UUID,
    payload: TenantUpdate,
    *,
    actor_id: UUID,
    ip: str | None,
) -> Tenant:
    tenant = await get_tenant(session, tenant_id)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True, mode="json")
    for field, value in changes.items():
        setattr(tenant, field, value)
    if changes:
        record_audit(
            session,
            action="tenant.update",
            actor_type="platform",
            actor_id=actor_id,
            tenant_id=tenant.id,
            resource_type="tenant",
            resource_id=str(tenant.id),
            detail=changes,
            ip=ip,
        )
        await session.commit()
        await session.refresh(tenant)
    return tenant
