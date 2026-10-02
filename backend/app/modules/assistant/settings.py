"""AI 公司助理的设置（租户设置 tenant_settings.assistant）。没有保存过时按默认值。"""

import uuid
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.security.models import TenantSetting

GroupReplyMode = Literal["silent", "mentioned"]


class AssistantSettings(BaseModel):
    enabled: bool = Field(default=True, description="启用 AI 公司助理（需要套餐包含 AI）")
    name: str = Field(default="小助", min_length=1, max_length=32, description="助理的名称")
    persona: str = Field(default="", max_length=500, description="语气与风格的补充说明")
    group_reply_mode: GroupReplyMode = Field(
        default="silent", description="群里的默认行为：silent 只记录不说话；mentioned 被 @ 时回答"
    )
    group_extraction: bool = Field(default=True, description="从记录的群聊里提炼知识候选")
    notify_enabled: bool = Field(default=True, description="通过助理给员工发送平台提醒")
    per_minute: int = Field(default=20, ge=1, le=120, description="每个员工每分钟最多提问几次")


async def load(session: AsyncSession, tenant_id: uuid.UUID) -> AssistantSettings:
    row = await session.get(TenantSetting, tenant_id)
    return AssistantSettings.model_validate((row.assistant if row is not None else None) or {})


async def save(
    session: AsyncSession, tenant_id: uuid.UUID, value: AssistantSettings, staff_id: uuid.UUID
) -> AssistantSettings:
    """保存（由调用方提交）。"""
    row = await session.get(TenantSetting, tenant_id, with_for_update=True)
    if row is None:
        row = TenantSetting(tenant_id=tenant_id)
        session.add(row)
    row.assistant = value.model_dump()
    row.updated_by = staff_id
    return value
