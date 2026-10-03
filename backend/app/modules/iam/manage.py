"""员工与角色管理（设计文档 §3.2、§13.1）。

- 修改员工的名称、角色和状态；停用后立即退出登录（吊销刷新令牌，访问令牌在下一次请求时失效）、
  坐席下线、接待中的会话退回队列，IM 登录也被踢下线。名下客户需要另行交接。
- 不能停用自己；至少保留一名启用状态的租户管理员；启用时检查坐席额度。
- 不能修改权限高于自己的员工，也不能分配超出自己权限的角色（防止越权提权）。
- 按员工设置的页面和权限（§31）：只记录和角色的差别，多给的不能超出自己的权限；保存后员工的有效
  权限也不能超出自己的权限（例如把别人去掉的权限加回来）。租户管理员不能单独调整。
- 自定义角色：权限不能超出自己的权限；系统角色不能修改或删除；还有员工使用的角色不能删除。
"""

import logging
from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.config import Settings
from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.ids import new_id
from app.core.permissions import PERMISSION_INFO, TENANT_ADMIN_ROLE
from app.core.security import verify_password
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import check_limit
from app.modules.conversation import imids, outbox
from app.modules.iam import access, owner, passwords
from app.modules.iam.console import role_profile
from app.modules.iam.models import Role, Staff, StaffRole, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.iam.schemas import (
    PasswordChange,
    PasswordReset,
    PasswordResetResult,
    PermissionInfo,
    RoleCreate,
    RoleOut,
    RoleUpdate,
    StaffOut,
    StaffUpdate,
)
from app.modules.iam.service import (
    IssuedTokens,
    effective_permissions,
    granted_by,
    issue_tokens,
    role_permissions,
    roles_of,
    staff_out,
)
from app.modules.routing.models import AgentState, AgentStatus
from app.modules.sessions.engine import assign_queued, lock_tenant_routing, requeue_unanswered
from app.modules.todos.assign import reassign_from as reassign_todos

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def permission_catalog() -> list[PermissionInfo]:
    return [
        PermissionInfo(code=code, name=name, group=group)
        for code, (name, group) in PERMISSION_INFO.items()
    ]


# ---- 员工 ----


async def _target(session: AsyncSession, principal: Principal, staff_id: UUID) -> Staff:
    """要管理的员工：不存在返回 404；权限（有效权限，§31.4）高于自己时拒绝。"""
    staff = await session.get(Staff, staff_id, with_for_update=True)
    if staff is None:
        raise NotFound("员工不存在")
    if not effective_permissions(staff, await roles_of(session, staff.id)) <= principal.permissions:
        raise Forbidden("不能管理权限高于自己的员工")
    return staff


async def _other_active_admins(session: AsyncSession, staff_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count(func.distinct(Staff.id)))
            .join(StaffRole, StaffRole.staff_id == Staff.id)
            .join(Role, Role.id == StaffRole.role_id)
            .where(
                Role.code == TENANT_ADMIN_ROLE,
                Staff.status == StaffStatus.ACTIVE,
                Staff.id != staff_id,
            )
        )
        or 0
    )


async def _resolve_roles(
    session: AsyncSession, principal: Principal, codes: list[str]
) -> list[Role]:
    requested = set(codes)
    roles = list((await session.scalars(select(Role).where(Role.code.in_(requested)))).all())
    missing = requested - {role.code for role in roles}
    if missing:
        raise Unprocessable(f"角色不存在：{'、'.join(sorted(missing))}")
    if any(not role_permissions(role) <= principal.permissions for role in roles):
        raise Forbidden("不能分配超出自身权限的角色")
    return roles


async def update_staff(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    staff_id: UUID,
    payload: StaffUpdate,
    *,
    ip: str | None,
) -> StaffOut:
    staff = await _target(session, principal, staff_id)
    is_owner = await owner.refuse_others(
        session, principal, staff.id, "企业所有者的账号只能由本人修改"
    )
    current = await roles_of(session, staff.id)
    roles = current
    if payload.role_codes is not None:
        held = {r.code for r in current}
        owner.check_assignable(set(payload.role_codes), held)
        if is_owner and set(payload.role_codes) != held:
            raise Unprocessable("企业所有者的角色不能修改")
        roles = await _resolve_roles(session, principal, payload.role_codes)
    saved = access.snapshot(staff)
    if "access" in payload.model_fields_set:
        access.apply(
            staff,
            payload.access,
            roles=roles,
            granted=granted_by(roles),
            operator=principal.permissions,
        )
    elif not access.adjustable(roles):
        # 改成租户管理员后不能再单独调整，清掉原来的设置。
        access.apply(staff, None, roles=roles, granted=frozenset(), operator=frozenset())
    # 保存后的有效权限不能超出自己的权限（例如把别人去掉的权限恢复回来）。
    if not effective_permissions(staff, roles) <= principal.permissions:
        raise Forbidden("不能给出超出自身权限的权限")
    status = payload.status or staff.status
    if staff.id == principal.staff_id and status != StaffStatus.ACTIVE:
        raise Unprocessable("不能停用自己的账号")
    was_admin = staff.status == StaffStatus.ACTIVE and any(
        r.code == TENANT_ADMIN_ROLE for r in current
    )
    stays_admin = status == StaffStatus.ACTIVE and any(r.code == TENANT_ADMIN_ROLE for r in roles)
    if was_admin and not stays_admin and await _other_active_admins(session, staff.id) == 0:
        raise Conflict("至少需要保留一名启用状态的企业所有者")
    enabling = status == StaffStatus.ACTIVE and staff.status != StaffStatus.ACTIVE
    disabling = status == StaffStatus.DISABLED and staff.status == StaffStatus.ACTIVE
    if enabling:
        await check_limit(session, principal.tenant_id, "seats")

    changes: dict[str, object] = {}
    if payload.display_name is not None and payload.display_name != staff.display_name:
        staff.display_name = payload.display_name
        changes["display_name"] = payload.display_name
    if payload.role_codes is not None and {r.id for r in roles} != {r.id for r in current}:
        await session.execute(delete(StaffRole).where(StaffRole.staff_id == staff.id))
        session.add_all(
            StaffRole(tenant_id=principal.tenant_id, staff_id=staff.id, role_id=role.id)
            for role in roles
        )
        changes["roles"] = sorted(r.code for r in roles)
    if access.snapshot(staff) != saved:
        changes["access"] = access.snapshot(staff)
    rooms: set[UUID] = set()
    if status != staff.status:
        staff.status = status
        changes["status"] = status
    if disabling:
        await passwords.revoke_tokens(session, staff.id)
        await lock_tenant_routing(session, principal.tenant_id)
        now = _now()
        state = await session.scalar(
            select(AgentState).where(AgentState.staff_id == staff.id).with_for_update()
        )
        if state is not None and state.status != AgentStatus.OFFLINE:
            state.status = AgentStatus.OFFLINE
            state.status_changed_at = now
        rooms = await requeue_unanswered(
            session, staff.id, now, reason="staff_disabled", include_answered=True
        )
        # 未完成的待办按规则重新分派（设计文档 §24.6）。
        await reassign_todos(session, staff.id, actor_id=principal.staff_id)
    if changes:
        record_audit(
            session,
            action="staff.update",
            actor_type="staff",
            actor_id=principal.staff_id,
            tenant_id=principal.tenant_id,
            resource_type="staff",
            resource_id=str(staff.id),
            detail={"username": staff.username, **changes},
            ip=ip,
        )
    await session.commit()
    await session.refresh(staff)
    if disabling:
        await outbox.flush_rooms(ctx, principal.tenant_id, rooms)
        if rooms:
            await assign_queued(ctx, principal.tenant_id)
        try:
            await ctx.im.force_logout(imids.staff_user(principal.tenant_code, staff.id))
        except Exception:
            logger.warning("cannot log staff %s out of IM", staff.id, exc_info=True)
    return staff_out(staff, roles, owner=is_owner)


async def reset_password(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    staff_id: UUID,
    payload: PasswordReset,
    *,
    ip: str | None,
) -> PasswordResetResult:
    """管理员重置员工的密码（设计文档 §38.4）。

    适用于全部角色，但不能重置权限高于自己的员工，也不能重置自己的（要输入当前密码修改）。
    新密码不填时自动生成；员工现有的登录全部失效。
    """
    if staff_id == principal.staff_id:
        raise Unprocessable("不能重置自己的密码，请在右上角的账号菜单里修改密码")
    staff = await _target(session, principal, staff_id)
    await owner.refuse_others(
        session, principal, staff.id, "企业所有者的密码由本人修改，或由平台运维人员重置"
    )
    generated = await passwords.reset(
        session, staff, payload.password, must_change=payload.must_change
    )
    record_audit(
        session,
        action=passwords.RESET_ACTION,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="staff",
        resource_id=str(staff.id),
        detail={
            "username": staff.username,
            "generated": generated is not None,
            "must_change": payload.must_change,
        },
        ip=ip,
    )
    await session.commit()
    await passwords.im_logout(ctx, principal.tenant_code, staff.id)
    return PasswordResetResult(
        temporary_password=generated, must_change_password=payload.must_change
    )


async def change_own_password(
    session: AsyncSession,
    settings: Settings,
    principal: Principal,
    payload: PasswordChange,
    *,
    ip: str | None,
) -> IssuedTokens:
    """修改自己的密码：其他设备上的登录失效，当前页面换发新的令牌。"""
    staff = await session.get(Staff, principal.staff_id, with_for_update=True)
    assert staff is not None
    if not verify_password(staff.password_hash, payload.current_password):
        raise Unprocessable("当前密码不正确")
    if payload.new_password == payload.current_password:
        raise Unprocessable("新密码不能与当前密码相同")
    # 重置后要求设置的新密码（§38.5）也在这里设置，设置后清除标记。
    await passwords.set_password(session, staff, payload.new_password, must_change=False)
    tokens, _ = issue_tokens(session, settings, staff)
    record_audit(
        session,
        action="staff.change_password",
        actor_type="staff",
        actor_id=staff.id,
        tenant_id=staff.tenant_id,
        resource_type="staff",
        resource_id=str(staff.id),
        ip=ip,
    )
    await session.commit()
    return tokens


# ---- 角色 ----


async def role_members(session: AsyncSession) -> Counter[UUID]:
    rows = await session.execute(
        select(StaffRole.role_id, func.count()).group_by(StaffRole.role_id)
    )
    return Counter({role_id: count for role_id, count in rows})


def role_out(role: Role, members: int) -> RoleOut:
    return RoleOut(
        id=role.id,
        code=role.code,
        name={
            "tenant_admin": "企业所有者",
            "agent": "客服",
            "worker": "工厂工人",
            "finance": "财务",
            "cashier": "出纳",
        }.get(role.code, role.name)
        if role.is_system
        else role.name,
        permissions=sorted(role_permissions(role)),
        is_system=role.is_system,
        members=members,
        console=role_profile(role),
        console_auto=not role.is_system and role.console is None,
    )


def _check_grantable(principal: Principal, permissions: list[str]) -> list[str]:
    granted = sorted(set(permissions))
    if not set(granted) <= principal.permissions:
        raise Forbidden("角色的权限不能超出自己拥有的权限")
    return granted


async def create_role(
    session: AsyncSession, principal: Principal, payload: RoleCreate, *, ip: str | None
) -> RoleOut:
    if await session.scalar(select(Role.id).where(Role.code == payload.code)):
        raise Conflict("角色代码已存在")
    role = Role(
        id=new_id(),
        tenant_id=principal.tenant_id,
        code=payload.code,
        name=payload.name,
        permissions=_check_grantable(principal, [str(p) for p in payload.permissions]),
        is_system=False,
        console=payload.console,
    )
    session.add(role)
    record_audit(
        session,
        action="role.create",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="role",
        resource_id=str(role.id),
        detail={"code": role.code, "permissions": role.permissions, "console": role.console},
        ip=ip,
    )
    await session.commit()
    await session.refresh(role)
    return role_out(role, 0)


async def _custom_role(session: AsyncSession, role_id: UUID) -> Role:
    role = await session.get(Role, role_id, with_for_update=True)
    if role is None:
        raise NotFound("角色不存在")
    if role.is_system:
        raise Conflict("系统角色不能修改或删除")
    return role


async def update_role(
    session: AsyncSession,
    principal: Principal,
    role_id: UUID,
    payload: RoleUpdate,
    *,
    ip: str | None,
) -> RoleOut:
    role = await _custom_role(session, role_id)
    if not role_permissions(role) <= principal.permissions:
        raise Forbidden("不能修改权限高于自己的角色")
    if payload.name is not None:
        role.name = payload.name
    if payload.permissions is not None:
        role.permissions = _check_grantable(principal, [str(p) for p in payload.permissions])
    if "console" in payload.model_fields_set:
        role.console = payload.console
    record_audit(
        session,
        action="role.update",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="role",
        resource_id=str(role.id),
        detail={
            "code": role.code,
            "name": role.name,
            "permissions": role.permissions,
            "console": role.console,
        },
        ip=ip,
    )
    await session.commit()
    await session.refresh(role)
    return role_out(role, (await role_members(session))[role.id])


async def delete_role(
    session: AsyncSession, principal: Principal, role_id: UUID, *, ip: str | None
) -> None:
    role = await _custom_role(session, role_id)
    if not role_permissions(role) <= principal.permissions:
        raise Forbidden("不能删除权限高于自己的角色")
    if (await role_members(session))[role.id]:
        raise Conflict("还有员工使用这个角色，请先调整他们的角色")
    await session.delete(role)
    record_audit(
        session,
        action="role.delete",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="role",
        resource_id=str(role.id),
        detail={"code": role.code},
        ip=ip,
    )
    await session.commit()
