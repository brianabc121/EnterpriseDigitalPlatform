"""企业的管理员账号（设计文档 §38.2、§38.3）：运营后台查看企业拥有者和管理员账号，
填写原因后重置管理员的密码。

- 管理员账号是有"租户管理员"角色的员工；企业拥有者是开通企业时创建的账号（企业里最早
  创建的账号）。
- 平台重置的一律是临时密码：管理员登录后要先设置新密码（§38.5），运营人员知道的密码只能
  用来设置新密码。
- 记入企业的操作日志（操作人"平台运维"，带原因），企业的其他管理员收到提醒。

使用 edp_platform 连接（不受行级安全限制），所有查询都按租户过滤。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.permissions import TENANT_ADMIN_ROLE
from app.modules.audit.models import AuditLog
from app.modules.audit.service import record_audit
from app.modules.iam import passwords
from app.modules.iam.models import Role, Staff, StaffRole, StaffStatus
from app.modules.iam.schemas import PasswordResetResult
from app.modules.notifications import service as notifications
from app.modules.notifications.push import notify_staff
from app.modules.tenancy.models import PlatformUser, Tenant, TenantStatus
from app.modules.tenancy.schemas import AdminPasswordReset, TenantAdminOut

LOGIN_ACTION = "auth.login"


def _admin_ids(tenant_id: UUID) -> Select[UUID]:
    """有租户管理员角色的员工。"""
    return (
        select(StaffRole.staff_id)
        .join(Role, Role.id == StaffRole.role_id)
        .where(StaffRole.tenant_id == tenant_id, Role.code == TENANT_ADMIN_ROLE)
    )


async def _owner_id(session: AsyncSession, tenant_id: UUID) -> UUID | None:
    return await session.scalar(
        select(Staff.id)
        .where(Staff.tenant_id == tenant_id)
        .order_by(Staff.created_at, Staff.id)
        .limit(1)
    )


async def list_admins(session: AsyncSession, tenant: Tenant) -> list[TenantAdminOut]:
    """企业的管理员账号：拥有者在前，其余按创建的先后。"""
    staff = list(
        (
            await session.scalars(
                select(Staff)
                .where(Staff.tenant_id == tenant.id, Staff.id.in_(_admin_ids(tenant.id)))
                .order_by(Staff.created_at, Staff.id)
            )
        ).all()
    )
    owner = await _owner_id(session, tenant.id)
    logins: dict[UUID, datetime] = {}
    if staff:
        rows = await session.execute(
            select(AuditLog.actor_id, func.max(AuditLog.created_at))
            .where(
                AuditLog.tenant_id == tenant.id,
                AuditLog.action == LOGIN_ACTION,
                AuditLog.actor_type == "staff",
                AuditLog.actor_id.in_([s.id for s in staff]),
            )
            .group_by(AuditLog.actor_id)
        )
        logins = {actor_id: at for actor_id, at in rows.all() if actor_id is not None}
    items = [
        TenantAdminOut(
            id=s.id,
            username=s.username,
            display_name=s.display_name,
            status=s.status,
            owner=s.id == owner,
            created_at=s.created_at,
            last_login_at=logins.get(s.id),
            password_changed_at=s.password_changed_at,
            must_change_password=s.must_change_password,
        )
        for s in staff
    ]
    return sorted(items, key=lambda item: not item.owner)


async def reset_password(
    ctx: AppContext,
    session: AsyncSession,
    tenant: Tenant,
    staff_id: UUID,
    payload: AdminPasswordReset,
    *,
    actor: PlatformUser,
    ip: str | None,
) -> PasswordResetResult:
    """重置企业管理员的密码：新密码是临时密码，现有的登录全部失效；留痕并提醒企业的其他管理员。"""
    if tenant.status == TenantStatus.CLOSED:
        raise Conflict("租户已注销")
    reason = payload.reason.strip()
    if len(reason) < 2:
        raise Unprocessable("请填写重置的原因")
    staff = await session.scalar(
        select(Staff)
        .where(
            Staff.tenant_id == tenant.id,
            Staff.id == staff_id,
            Staff.id.in_(_admin_ids(tenant.id)),
        )
        .with_for_update()
    )
    if staff is None:
        raise NotFound("管理员账号不存在")
    generated = await passwords.reset(session, staff, payload.password, must_change=True)
    record_audit(
        session,
        action=passwords.RESET_ACTION,
        actor_type="platform",
        actor_id=actor.id,
        tenant_id=tenant.id,
        resource_type="staff",
        resource_id=str(staff.id),
        detail={
            "username": staff.username,
            "reason": reason,
            "generated": generated is not None,
            "must_change": True,
        },
        ip=ip,
    )
    others = list(
        (
            await session.scalars(
                select(Staff.id)
                .where(
                    Staff.tenant_id == tenant.id,
                    Staff.id.in_(_admin_ids(tenant.id)),
                    Staff.status == StaffStatus.ACTIVE,
                    Staff.id != staff.id,
                )
                .order_by(Staff.created_at, Staff.id)
            )
        ).all()
    )
    title = f"平台运维人员重置了管理员 {staff.display_name}（{staff.username}）的密码"
    body = f"原因：{reason}。{staff.display_name} 下次登录时要先设置新密码；详情见操作日志。"
    notifications.add(
        session, tenant.id, others, kind="security", title=title, body=body, link="/audit"
    )
    await session.commit()
    await notify_staff(ctx, tenant.id, others, title=title, description=body, path="/audit")
    await passwords.im_logout(ctx, tenant.code, staff.id)
    return PasswordResetResult(temporary_password=generated, must_change_password=True)
