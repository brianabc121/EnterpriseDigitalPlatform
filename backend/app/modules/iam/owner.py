"""企业所有者（设计文档 §39.5）：开通企业（或企业自助注册）时由平台创建的账号。

- 企业所有者的角色（tenant_admin）只能由平台创建，企业里不能分配给别人；以前分配过的照常保留。
- 有这个角色的员工里最早创建的是企业所有者：员工导图最顶部的卡片，运营后台"管理员账号"里的拥有者。
- 企业所有者的账号只能由本人修改，角色不能改；别人不能停用、删除，也不能重置密码（由平台
  运维人员重置）。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, Unprocessable
from app.core.permissions import TENANT_ADMIN_ROLE
from app.modules.iam.models import Role, Staff, StaffRole
from app.modules.iam.principal import Principal

NOT_ASSIGNABLE = "企业所有者由平台在开通企业时创建，不能分配这个角色"


async def owner_id(session: AsyncSession, tenant_id: UUID) -> UUID | None:
    """有企业所有者角色的员工里最早创建的。"""
    return await session.scalar(
        select(Staff.id)
        .join(StaffRole, StaffRole.staff_id == Staff.id)
        .join(Role, Role.id == StaffRole.role_id)
        .where(
            Staff.tenant_id == tenant_id,
            StaffRole.tenant_id == tenant_id,
            Role.code == TENANT_ADMIN_ROLE,
        )
        .order_by(Staff.created_at, Staff.id)
        .limit(1)
    )


def check_assignable(requested: set[str], held: set[str] | frozenset[str] = frozenset()) -> None:
    """企业里不能给出企业所有者的角色（员工原来就有的保留）。"""
    if TENANT_ADMIN_ROLE in requested - held:
        raise Unprocessable(NOT_ASSIGNABLE)


async def refuse_others(
    session: AsyncSession, principal: Principal, staff_id: UUID, message: str
) -> bool:
    """别人不能对企业所有者做这件事；返回目标是不是企业所有者。"""
    owner = await owner_id(session, principal.tenant_id)
    if staff_id == owner and principal.staff_id != staff_id:
        raise Forbidden(message)
    return staff_id == owner
