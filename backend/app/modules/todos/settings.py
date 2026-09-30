"""待办设置（租户设置 tenant_settings.todos）。没有保存过时按默认值。"""

import uuid

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.security.models import TenantSetting


class TodoSettings(BaseModel):
    ai_hourly_limit: int = Field(
        default=3, ge=1, le=20, description="每个会话每小时最多由 AI 登记几条待办（防止刷单）"
    )
    extract_enabled: bool = Field(
        default=True, description="会话结束后由 AI 解析未解决的诉求和坐席答应的事，进入待确认页"
    )
    extract_min_confidence: float = Field(
        default=0.6, ge=0, le=1, description="会话后解析的置信度低于这个值时不进入待确认页"
    )
    pending_remind_minutes: int = Field(
        default=120, ge=10, le=24 * 60, description="待确认超过这么多工作分钟还没处理，再提醒一次"
    )
    digest_enabled: bool = Field(default=True, description="每个工作日上班时发送今日待办汇总")
    reopen_days: int = Field(default=7, ge=1, le=90, description="完成后多少天内可以重新打开")
    visitor_progress: bool = Field(
        default=False,
        description="访客在 Widget 的服务进度里查看自己的待办（类型、状态、预计时间）",
    )


async def load(session: AsyncSession, tenant_id: uuid.UUID) -> TodoSettings:
    row = await session.get(TenantSetting, tenant_id)
    return TodoSettings.model_validate((row.todos if row is not None else None) or {})


async def save(
    session: AsyncSession, tenant_id: uuid.UUID, value: TodoSettings, staff_id: uuid.UUID
) -> TodoSettings:
    """保存（由调用方提交）。"""
    row = await session.get(TenantSetting, tenant_id, with_for_update=True)
    if row is None:
        row = TenantSetting(tenant_id=tenant_id)
        session.add(row)
    row.todos = value.model_dump()
    row.updated_by = staff_id
    return value
