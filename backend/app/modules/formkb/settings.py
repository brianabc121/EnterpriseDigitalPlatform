"""表单知识的设置（租户设置 tenant_settings.form_kb，设计文档 §25.18）。"""

import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.security.models import TenantSetting


class FormKbSettings(BaseModel):
    auto_activate: bool = Field(
        default=True,
        description="学到的知识达到条件就生效；关掉后先标“待确认”，有人确认后才用上",
    )
    learn_aliases: bool = Field(default=True, description="学习叫法（输入的文字对应的商品）")
    learn_usage: bool = Field(default=True, description="学习用量（成品每件用多少材料）")
    learn_companions: bool = Field(default=True, description="学习搭配（常一起开的商品）")


async def load(session: AsyncSession, tenant_id: uuid.UUID) -> FormKbSettings:
    value = await session.scalar(
        select(TenantSetting.form_kb).where(TenantSetting.tenant_id == tenant_id)
    )
    return FormKbSettings.model_validate(value or {})


async def save(
    session: AsyncSession, tenant_id: uuid.UUID, value: FormKbSettings, staff_id: uuid.UUID
) -> FormKbSettings:
    """保存（由调用方提交）。"""
    row = await session.get(TenantSetting, tenant_id, with_for_update=True)
    if row is None:
        row = TenantSetting(tenant_id=tenant_id)
        session.add(row)
    row.form_kb = value.model_dump(mode="json")
    row.updated_by = staff_id
    return value
