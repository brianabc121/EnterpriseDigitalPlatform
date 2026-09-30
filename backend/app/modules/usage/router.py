from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.config import Settings
from app.core.dates import date_range, zone
from app.core.deps import get_app_settings
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.tenancy import service as tenancy
from app.modules.tenancy.deps import CurrentPlatformUser, PlatformDb
from app.modules.usage import service
from app.modules.usage.schemas import TenantUsageList, UsageReport

router = APIRouter(prefix="/api/v1", tags=["usage"], responses=ERROR_RESPONSES)
platform_router = APIRouter(prefix="/platform/v1", tags=["platform"], responses=ERROR_RESPONSES)

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
CanManage = Annotated[Principal, Depends(require_permission(Permission.SETTINGS_MANAGE))]
Start = Annotated[date | None, Query(description="开始日期（含），默认最近 30 天")]
End = Annotated[date | None, Query(description="结束日期（含），默认今天")]


@router.get("/usage", response_model=UsageReport)
async def my_usage(
    session: TenantDb,
    principal: CanManage,
    settings: SettingsDep,
    start: Start = None,
    end: End = None,
) -> UsageReport:
    """本租户的每日用量。"""
    tz = zone(settings.usage_timezone)
    first, last = date_range(start, end, tz, default_days=service.DEFAULT_DAYS)
    return await service.usage_report(session, principal.tenant_id, first, last, tz)


@platform_router.get("/usage", response_model=TenantUsageList)
async def tenants_usage(
    session: PlatformDb,
    _: CurrentPlatformUser,
    settings: SettingsDep,
    start: Start = None,
    end: End = None,
) -> TenantUsageList:
    """各租户在范围内的用量合计。"""
    tz = zone(settings.usage_timezone)
    first, last = date_range(start, end, tz, default_days=service.DEFAULT_DAYS)
    return await service.tenants_usage(session, first, last, tz)


@platform_router.get("/tenants/{tenant_id}/usage", response_model=UsageReport)
async def tenant_usage(
    tenant_id: UUID,
    session: PlatformDb,
    _: CurrentPlatformUser,
    settings: SettingsDep,
    start: Start = None,
    end: End = None,
) -> UsageReport:
    """某个租户的每日用量。"""
    await tenancy.get_tenant(session, tenant_id)
    tz = zone(settings.usage_timezone)
    first, last = date_range(start, end, tz, default_days=service.DEFAULT_DAYS)
    return await service.usage_report(session, tenant_id, first, last, tz)
