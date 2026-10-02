"""意向客户的设置（租户设置 tenant_settings.prospects，设计文档 §35.6）：AI 转入的方式、最低意向、
默认跟进天数。"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.security.models import TenantSetting

AiMode = Literal["auto", "suggest", "off"]


class ProspectSettings(BaseModel):
    ai_mode: AiMode = Field(
        default="auto", description="AI 转入：auto 自动转入，suggest 只建议（员工确认），off 关闭"
    )
    min_stage: int = Field(
        default=2,
        ge=2,
        le=4,
        description="AI 转入的最低意向（意图判断的下单意向）：2 有兴趣，3 意向明确，4 准备下单",
    )
    follow_days: int = Field(default=3, ge=1, le=60, description="默认几天后跟进")


def parse(raw: dict[str, Any] | None) -> ProspectSettings:
    try:
        return ProspectSettings.model_validate(raw or {})
    except ValueError:
        return ProspectSettings()


async def load(session: AsyncSession, tenant_id: uuid.UUID) -> ProspectSettings:
    row = await session.get(TenantSetting, tenant_id)
    return parse(row.prospects if row is not None else None)


async def save(
    session: AsyncSession, tenant_id: uuid.UUID, value: ProspectSettings, staff_id: uuid.UUID
) -> ProspectSettings:
    """保存（由调用方提交）。"""
    row = await session.get(TenantSetting, tenant_id, with_for_update=True)
    if row is None:
        row = TenantSetting(tenant_id=tenant_id)
        session.add(row)
    row.prospects = value.model_dump(mode="json")
    row.updated_by = staff_id
    return value
