"""AI 唤醒的设置（租户设置 tenant_settings.wake，设计文档 §33.8）。没有保存过时按默认值。

检查项的开关和数字只保存改过的（checks: {检查项: {"enabled": ..., "params": {...}}}），其余按
checks.py 里每个检查项的默认值。
"""

import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.security.models import TenantSetting

TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class CheckConfig(BaseModel):
    enabled: bool = True
    params: dict[str, int] = Field(default_factory=dict)


class WakeSettings(BaseModel):
    enabled: bool = Field(default=True, description="开启 AI 唤醒：关掉后不再巡检、不再整理知识库")
    hourly: bool = Field(default=True, description="工作时间内每小时检查需要及时处理的问题")
    daily_time: str = Field(default="08:30", pattern=TIME_PATTERN, description="每日巡检的时间")
    daily_workdays_only: bool = Field(
        default=True, description="每日巡检只在工作日（按默认路由策略的工作时间）"
    )
    brief_staff_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=20, description="除租户管理员外，还有谁收到每日简报"
    )
    escalate_days: int = Field(
        default=2, ge=1, le=30, description="问题超过几天没处理就升级给管理员"
    )
    kb_enabled: bool = Field(default=True, description="定期整理知识库，对照现行的规章制度")
    kb_weekday: int = Field(default=1, ge=1, le=7, description="每周哪一天整理（1 是周一）")
    kb_time: str = Field(default="08:00", pattern=TIME_PATTERN, description="知识库整理的时间")
    kb_on_policy_change: bool = Field(
        default=True, description="规章制度新增、修改、废止后 10 分钟自动整理"
    )
    kb_hold_conflicts: bool = Field(
        default=False,
        description="和现行制度冲突、还没处理的知识先暂停用于 AI 接待（坐席仍然能看到）",
    )
    checks: dict[str, CheckConfig] = Field(
        default_factory=dict, description="检查项的开关和数字（只保存改过的）"
    )

    @field_validator("brief_staff_ids")
    @classmethod
    def _unique(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(dict.fromkeys(value))

    def check(self, code: str) -> CheckConfig:
        return self.checks.get(code) or CheckConfig()


def parse(raw: dict[str, Any] | None) -> WakeSettings:
    """保存的设置；格式不对的值按默认（不让一次错误的保存停掉巡检）。"""
    try:
        return WakeSettings.model_validate(raw or {})
    except ValueError:
        return WakeSettings()


async def load(session: AsyncSession, tenant_id: uuid.UUID) -> WakeSettings:
    row = await session.get(TenantSetting, tenant_id)
    return parse(row.wake if row is not None else None)


async def save(
    session: AsyncSession, tenant_id: uuid.UUID, value: WakeSettings, staff_id: uuid.UUID
) -> WakeSettings:
    """保存（由调用方提交）。"""
    row = await session.get(TenantSetting, tenant_id, with_for_update=True)
    if row is None:
        row = TenantSetting(tenant_id=tenant_id)
        session.add(row)
    row.wake = value.model_dump(mode="json")
    row.updated_by = staff_id
    return value
