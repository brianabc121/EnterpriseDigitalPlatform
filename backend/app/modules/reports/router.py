from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.config import Settings
from app.core.dates import date_range, today, zone
from app.core.deps import get_app_settings
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.billing.entitlements import require_feature
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.reports import business, sales, service
from app.modules.reports.schemas import (
    AgentReport,
    OrderReport,
    Overview,
    Realtime,
    SalesReport,
    TodoReport,
)

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


@router.get("/todos", response_model=TodoReport)
async def todos_report(
    session: TenantDb,
    principal: CanView,
    settings: SettingsDep,
    start: Start = None,
    end: End = None,
    tz: Tz = None,
) -> TodoReport:
    """待办：数量、时效、AI 生成的质量和每日趋势（数据范围与待办中心一致）。"""
    await require_feature(session, principal.tenant_id, "todos")
    zone_ = zone(tz or settings.usage_timezone)
    first, last = date_range(start, end, zone_, default_days=DEFAULT_DAYS)
    return await business.todo_report(session, principal, first, last, zone_)


@router.get("/orders", response_model=OrderReport)
async def orders_report(
    session: TenantDb,
    principal: CanView,
    settings: SettingsDep,
    start: Start = None,
    end: End = None,
    tz: Tz = None,
) -> OrderReport:
    """订单：AI 下单、业务、收款、安全和当前积压（数据范围与订单中心一致）。"""
    await require_feature(session, principal.tenant_id, "orders")
    zone_ = zone(tz or settings.usage_timezone)
    first, last = date_range(start, end, zone_, default_days=DEFAULT_DAYS)
    return await business.order_report(session, principal, first, last, zone_, today(zone_))


@router.get("/sales", response_model=SalesReport)
async def sales_report(
    session: TenantDb,
    principal: CanView,
    settings: SettingsDep,
    start: Start = None,
    end: End = None,
    tz: Tz = None,
) -> SalesReport:
    """销售：漏斗、进行中的预计金额、赢单率、输单原因、按负责人、按来源（数据范围按客户的可见范围，
    默认最近 30 天）。"""
    zone_ = zone(tz or settings.usage_timezone)
    first, last = date_range(start, end, zone_, default_days=30)
    return await sales.sales_report(session, principal, first, last, zone_)
