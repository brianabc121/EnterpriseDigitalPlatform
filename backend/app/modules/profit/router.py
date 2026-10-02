"""盈利报表接口（设计文档 §30.7）。查看和导出需要 profit:view（全公司的数据，不受订单数据范围
限制）；登记、修改、删除收支需要 profit:manage。套餐里要有订单功能。"""

from datetime import UTC, date, datetime
from typing import Annotated
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.core.dates import today as today_in
from app.core.deps import client_ip
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.core.xlsx import XLSX_MEDIA_TYPE
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import require_feature
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.profit import entries, export, periods, report
from app.modules.profit.periods import Period
from app.modules.profit.schemas import (
    CategoryOptions,
    CopyRecurringIn,
    CopyRecurringResult,
    Dimension,
    Direction,
    EntryIn,
    EntryKindValue,
    EntryOut,
    EntryPage,
    ExportIn,
    ProfitBreakdown,
    ProfitSummary,
    ProfitTrend,
    SortKey,
)

router = APIRouter(prefix="/api/v1/profit", tags=["profit"], responses=ERROR_RESPONSES)


def _feature(*permissions: str):  # type: ignore[no-untyped-def]
    async def dependency(
        principal: Annotated[Principal, Depends(require_permission(*permissions))],
        session: TenantDb,
    ) -> Principal:
        await require_feature(session, principal.tenant_id, "orders")
        return principal

    return dependency


CanView = Annotated[Principal, Depends(_feature(Permission.PROFIT_VIEW))]
CanManage = Annotated[Principal, Depends(_feature(Permission.PROFIT_MANAGE))]
Start = Annotated[date | None, Query(description="开始日期（含），默认本月 1 日")]
End = Annotated[date | None, Query(description="结束日期（含），默认今天")]
Shift = Annotated[
    int | None, Query(ge=1, le=24, description="上期往前移几个月；不传时按期间跨的月数")
]


async def _period(
    session: TenantDb, start: date | None, end: date | None
) -> tuple[ZoneInfo, Period, date]:
    tz = await report.tenant_zone(session)
    day = today_in(tz)
    end = end or day
    start = start or min(end, day).replace(day=1)
    return tz, periods.check(start, end), day


@router.get("/summary", response_model=ProfitSummary)
async def summary(
    session: TenantDb, _: CanView, start: Start = None, end: End = None, shift: Shift = None
) -> ProfitSummary:
    """利润表：本期、上期、去年同期；费用和其他收入按类别；回款、本期订单未收；成本缺失。"""
    tz, period, _day = await _period(session, start, end)
    return await report.summary(session, tz, period, shift)


@router.get("/trend", response_model=ProfitTrend)
async def trend(session: TenantDb, _: CanView, end: End = None) -> ProfitTrend:
    """截至 end 所在月的 12 个月：订单数、销售收入、成本、毛利、其他收入、费用、净利润。"""
    tz = await report.tenant_zone(session)
    return await report.trend(session, tz, end or today_in(tz))


@router.get("/breakdown", response_model=ProfitBreakdown)
async def breakdown(
    session: TenantDb,
    _: CanView,
    by: Dimension = "product",
    start: Start = None,
    end: End = None,
    sort: SortKey = "profit",
    direction: Direction = "desc",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProfitBreakdown:
    """毛利分析：按商品、客户、处理人、来源、渠道、订单汇总收入、成本、毛利、毛利率。"""
    tz, period, _day = await _period(session, start, end)
    return await report.breakdown(
        session, tz, period, by, sort=sort, direction=direction, limit=limit, offset=offset
    )


@router.get("/entries", response_model=EntryPage)
async def list_entries(
    session: TenantDb,
    _: CanView,
    start: Start = None,
    end: End = None,
    kind: EntryKindValue | None = None,
    category: Annotated[str | None, Query(max_length=20)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EntryPage:
    """期间内的收支明细、合计、按类别小计，以及还没登记的每月固定收支。"""
    _tz, period, day = await _period(session, start, end)
    return await entries.list_entries(
        session, period, day, kind=kind, category=category, limit=limit, offset=offset
    )


@router.get("/categories", response_model=CategoryOptions)
async def categories(session: TenantDb, _: CanView) -> CategoryOptions:
    """登记收支时可选的类别：常用的在前，后面是本企业用过的。"""
    return await entries.categories(session)


@router.post(
    "/export",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "盈利报表"}},
)
async def export_report(
    payload: ExportIn, request: Request, session: TenantDb, principal: CanView
) -> Response:
    """导出 Excel：利润表、每月、按商品、按客户、按处理人、订单明细、收支明细。记审计。"""
    tz, period, day = await _period(session, payload.start, payload.end)
    content = await export.workbook(
        session,
        tz,
        period,
        payload.shift,
        company=principal.tenant_name,
        generated_by=principal.display_name,
        now=datetime.now(UTC).astimezone(tz),
        today=day,
    )
    record_audit(
        session,
        action="profit.export",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="profit",
        detail={"start": period.start.isoformat(), "end": period.end.isoformat()},
        ip=client_ip(request),
    )
    await session.commit()
    name = quote(f"盈利报表-{period.start:%Y%m%d}-{period.end:%Y%m%d}.xlsx")
    return Response(
        content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )


@router.post("/entries", response_model=EntryOut, status_code=status.HTTP_201_CREATED)
async def create_entry(
    payload: EntryIn, request: Request, session: TenantDb, principal: CanManage
) -> EntryOut:
    """登记一笔费用（支出）或其他收入。"""
    tz = await report.tenant_zone(session)
    return await entries.create(session, principal, payload, today_in(tz), ip=client_ip(request))


@router.put("/entries/{profit_entry_id}", response_model=EntryOut)
async def update_entry(
    profit_entry_id: UUID,
    payload: EntryIn,
    request: Request,
    session: TenantDb,
    principal: CanManage,
) -> EntryOut:
    """修改一笔收支（记审计：改了哪些、改前改后）。"""
    tz = await report.tenant_zone(session)
    return await entries.update(
        session, principal, profit_entry_id, payload, today_in(tz), ip=client_ip(request)
    )


@router.delete("/entries/{profit_entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    profit_entry_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> Response:
    """删除一笔收支（记审计：删掉的内容）。"""
    await entries.delete(session, principal, profit_entry_id, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/entries/copy-recurring", response_model=CopyRecurringResult)
async def copy_recurring(
    payload: CopyRecurringIn, request: Request, session: TenantDb, principal: CanManage
) -> CopyRecurringResult:
    """把上个月标了"每月固定"、还没登记到这个月的收支，按原内容登记到这个月。"""
    tz = await report.tenant_zone(session)
    return await entries.copy_recurring(
        session,
        principal,
        periods.parse_month(payload.month),
        today_in(tz),
        ip=client_ip(request),
    )
