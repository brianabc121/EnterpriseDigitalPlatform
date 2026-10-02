"""合同设置（租户设置 tenant_settings.contracts，设计文档 §34.7）：我方信息、编号前缀、
快到期天数。"""

import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.security.models import TenantSetting


class ContractParty(BaseModel):
    """我方信息：填写 {{我方名称}} 等内置填写项。名称为空时用企业的名称。"""

    name: str = Field(default="", max_length=128, description="企业全称")
    address: str = Field(default="", max_length=200, description="地址")
    phone: str = Field(default="", max_length=64, description="电话")
    tax_no: str = Field(default="", max_length=64, description="统一社会信用代码（税号）")
    bank: str = Field(default="", max_length=128, description="开户行")
    account: str = Field(default="", max_length=64, description="账号")
    representative: str = Field(default="", max_length=64, description="法定代表人或授权代表")


class ContractSettings(BaseModel):
    party: ContractParty = Field(default_factory=ContractParty)
    prefix: str = Field(
        default="HT", min_length=1, max_length=8, pattern=r"^[A-Za-z0-9-]+$", description="编号前缀"
    )
    expiring_days: int = Field(
        default=30, ge=1, le=365, description="已签署的合同结束日期在几天内时算快到期"
    )


def parse(raw: dict[str, Any] | None) -> ContractSettings:
    try:
        return ContractSettings.model_validate(raw or {})
    except ValueError:
        return ContractSettings()


async def load(session: AsyncSession, tenant_id: uuid.UUID) -> ContractSettings:
    row = await session.get(TenantSetting, tenant_id)
    return parse(row.contracts if row is not None else None)


async def save(
    session: AsyncSession, tenant_id: uuid.UUID, value: ContractSettings, staff_id: uuid.UUID
) -> ContractSettings:
    """保存（由调用方提交）。"""
    row = await session.get(TenantSetting, tenant_id, with_for_update=True)
    if row is None:
        row = TenantSetting(tenant_id=tenant_id)
        session.add(row)
    row.contracts = value.model_dump(mode="json")
    row.updated_by = staff_id
    return value
