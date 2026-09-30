"""仓库设置（租户设置 tenant_settings.warehouse，设计文档 §25.13）：仓管、单据是否需要仓管确认。

仓管：管理员指定的一名员工；没有指定（或者指定的员工已停用）时由最早创建、启用状态的工人担任。
仓管在每次请求时额外获得 inventory:manage 和 warehouse:confirm（见 iam.service.principal_for），
可以确认领料单和入库单、盘点和调整库存。
"""

import uuid
from dataclasses import dataclass

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.modules.iam.models import Role, Staff, StaffRole, StaffStatus
from app.modules.security.models import TenantSetting

KEEPER_PERMISSIONS = frozenset({Permission.INVENTORY_MANAGE, Permission.WAREHOUSE_CONFIRM})
WORKER_ROLE = "worker"


class WarehouseSettings(BaseModel):
    confirm_required: bool = Field(
        default=True,
        description="领料单和入库单要仓管确认后才修改库存（关闭后开单即生效）",
    )
    keeper_id: uuid.UUID | None = Field(
        default=None, description="仓管；为空时由最早创建的工人担任"
    )


async def load(session: AsyncSession, tenant_id: uuid.UUID) -> WarehouseSettings:
    # 只取这一列：不把设置行放进会话（之后可能要 FOR UPDATE 读取）。
    value = await session.scalar(
        select(TenantSetting.warehouse).where(TenantSetting.tenant_id == tenant_id)
    )
    return WarehouseSettings.model_validate(value or {})


async def save(
    session: AsyncSession, tenant_id: uuid.UUID, value: WarehouseSettings, staff_id: uuid.UUID
) -> WarehouseSettings:
    """保存（由调用方提交）。"""
    row = await session.get(TenantSetting, tenant_id, with_for_update=True)
    if row is None:
        row = TenantSetting(tenant_id=tenant_id)
        session.add(row)
    row.warehouse = value.model_dump(mode="json")
    row.updated_by = staff_id
    return value


async def first_worker(session: AsyncSession) -> uuid.UUID | None:
    """最早创建、启用状态的工人（系统角色"工人"）。"""
    return await session.scalar(
        select(Staff.id)
        .join(StaffRole, StaffRole.staff_id == Staff.id)
        .join(Role, Role.id == StaffRole.role_id)
        .where(Role.code == WORKER_ROLE, Staff.status == StaffStatus.ACTIVE)
        .order_by(Staff.created_at, Staff.id)
        .limit(1)
    )


@dataclass(frozen=True)
class Keeper:
    staff_id: uuid.UUID | None
    # 没有指定仓管（或者指定的员工已停用），由最早创建的工人担任。
    fallback: bool


async def keeper(
    session: AsyncSession, tenant_id: uuid.UUID, settings: WarehouseSettings | None = None
) -> Keeper:
    settings = settings or await load(session, tenant_id)
    if settings.keeper_id is not None:
        status = await session.scalar(select(Staff.status).where(Staff.id == settings.keeper_id))
        if status == StaffStatus.ACTIVE:
            return Keeper(settings.keeper_id, False)
    return Keeper(await first_worker(session), True)


async def keeper_permissions(
    session: AsyncSession, tenant_id: uuid.UUID, staff_id: uuid.UUID
) -> frozenset[str]:
    """员工作为仓管额外获得的权限（不是仓管时为空）。"""
    found = await keeper(session, tenant_id)
    return KEEPER_PERMISSIONS if found.staff_id == staff_id else frozenset()
