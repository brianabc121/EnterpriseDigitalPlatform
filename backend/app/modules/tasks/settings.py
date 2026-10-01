"""个人待办的设置（租户设置 tenant_settings.tasks）。没有保存过时按默认值。"""

import uuid

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.security.models import TenantSetting


class TaskSettings(BaseModel):
    remind_before_minutes: int = Field(
        default=60, ge=0, le=7 * 24 * 60, description="有截止时间的事项默认提前多少分钟提醒"
    )
    digest_enabled: bool = Field(
        default=True, description="每个工作日上班时给有事项的员工发送今日个人待办汇总"
    )


async def load(session: AsyncSession, tenant_id: uuid.UUID) -> TaskSettings:
    row = await session.get(TenantSetting, tenant_id)
    return TaskSettings.model_validate((row.tasks if row is not None else None) or {})


async def save(
    session: AsyncSession, tenant_id: uuid.UUID, value: TaskSettings, staff_id: uuid.UUID
) -> TaskSettings:
    """保存（由调用方提交）。"""
    row = await session.get(TenantSetting, tenant_id, with_for_update=True)
    if row is None:
        row = TenantSetting(tenant_id=tenant_id)
        session.add(row)
    row.tasks = value.model_dump()
    row.updated_by = staff_id
    return value
