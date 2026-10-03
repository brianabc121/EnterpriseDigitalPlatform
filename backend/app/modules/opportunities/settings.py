"""商机的设置（租户设置 tenant_settings.opportunities，设计文档 §40.10）：AI 转入的方式、最低意向、
默认跟进天数、自动推进、新线索的分配、输单原因分类、预计金额对谁可见。阶段在 pipeline_stages
表里。"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.opportunities.models import DEFAULT_LOST_REASONS
from app.modules.security.models import TenantSetting

AiMode = Literal["auto", "suggest", "off"]
Assignment = Literal["owner", "round_robin"]
AmountVisibility = Literal["all", "managers"]


class AutoAdvance(BaseModel):
    """自动推进到哪个阶段（阶段代码，§40.5）；为空时这条规则关闭。只往前不往后；赢单的自动推进不能关。"""

    first_followup: str | None = Field(default="contacted", description="第一次跟进后")
    quote: str | None = Field(default="quoted", description="报价待办完成、订单提交审核后")
    contract_final: str | None = Field(default="negotiating", description="合同定稿后")


class LostReason(BaseModel):
    code: str = Field(min_length=1, max_length=16, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=16)


def default_lost_reasons() -> list[LostReason]:
    return [LostReason(code=code, name=name) for code, name in DEFAULT_LOST_REASONS]


class OpportunitySettings(BaseModel):
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
    auto_advance: AutoAdvance = Field(default_factory=AutoAdvance)
    assignment: Assignment = Field(
        default="owner",
        description="新线索分给谁：owner 客户的归属坐席，round_robin 轮流分给技能组",
    )
    assignment_group_id: uuid.UUID | None = Field(default=None, description="轮流分配的技能组")
    lost_reasons: list[LostReason] = Field(default_factory=default_lost_reasons, max_length=10)
    amount_visibility: AmountVisibility = Field(
        default="all", description="预计金额对谁可见：all 全部，managers 只有能分配商机的员工"
    )

    @field_validator("lost_reasons")
    @classmethod
    def _reasons(cls, value: list[LostReason]) -> list[LostReason]:
        if not value:
            raise ValueError("至少要有一个输单原因")
        codes = [r.code for r in value]
        if len(set(codes)) != len(codes):
            raise ValueError("输单原因的代码不能重复")
        return value

    def lost_reason_name(self, code: str | None) -> str | None:
        for reason in self.lost_reasons:
            if reason.code == code:
                return reason.name
        return None


def parse(raw: dict[str, Any] | None) -> OpportunitySettings:
    try:
        return OpportunitySettings.model_validate(raw or {})
    except ValueError:
        return OpportunitySettings()


async def load(session: AsyncSession, tenant_id: uuid.UUID) -> OpportunitySettings:
    row = await session.get(TenantSetting, tenant_id)
    return parse(row.opportunities if row is not None else None)


async def save(
    session: AsyncSession, tenant_id: uuid.UUID, value: OpportunitySettings, staff_id: uuid.UUID
) -> OpportunitySettings:
    """保存（由调用方提交）。"""
    row = await session.get(TenantSetting, tenant_id, with_for_update=True)
    if row is None:
        row = TenantSetting(tenant_id=tenant_id)
        session.add(row)
    row.opportunities = value.model_dump(mode="json")
    row.updated_by = staff_id
    return value
