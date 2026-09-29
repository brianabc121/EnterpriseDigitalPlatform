from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.config import Settings
from app.core.dates import date_range, zone
from app.core.deps import get_app_settings
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.reports import service
from app.modules.reports.schemas import AgentReport, Overview, Realtime

router = APIRouter(prefix="/api/v1/reports", tags=["reports"], responses=ERROR_RESPONSES)

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
CanView = Annotated[Principal, Depends(require_permission(Permission.REPORT_VIEW))]
CanSeeDashboard = Annotated[Principal, Depends(require_permission(Permission.DASHBOARD_VIEW))]
Start = Annotated[date | None, Query(description="开始日期（含），默认最近 7 天")]
End = Annotated[date | None, Query(description="结束日期（含），默认今天")]
Tz = Annotated[str | None, Query(description="划分日期的时区，默认 Asia/Shanghai")]
DEFAULT_DAYS = 7


@router.get("/overview", response_model=Overview)
async def overview(
    session: TenantDb,
    principal: CanView,
    settings: SettingsDep,
    start: Start = None,
    end: End = None,
    tz: Tz = None,
) -> Overview:
    """服务概览：会话、排队与响应时长、满意度、消息、留言与转接，以及每日趋势。"""
    zone_ = zone(tz or settings.usage_timezone)
    first, last = date_range(start, end, zone_, default_days=DEFAULT_DAYS)
    return await service.overview(session, principal, first, last, zone_)


@router.get("/agents", response_model=AgentReport)
async def agents(
    session: TenantDb,
    principal: CanView,
    settings: SettingsDep,
    start: Start = None,
    end: End = None,
    tz: Tz = None,
) -> AgentReport:
    """坐席工作量与服务质量。主管只看到所带团队。"""
    zone_ = zone(tz or settings.usage_timezone)
    first, last = date_range(start, end, zone_, default_days=DEFAULT_DAYS)
    return await service.agents(session, principal, first, last, zone_)


@router.get("/realtime", response_model=Realtime)
async def realtime(
    session: TenantDb, principal: CanSeeDashboard, settings: SettingsDep
) -> Realtime:
    """首页实时数据：排队、接待中、坐席状态、今日会话与满意度，以及我的接待情况。"""
    return await service.realtime(session, principal, zone(settings.usage_timezone))
