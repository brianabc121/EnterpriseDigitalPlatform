"""应收账款接口（设计文档 §28.8）。查看需要 finance:view（能看到全公司的应收）；跟进、催收、导出
需要 finance:manage；登记收款仍走订单接口（order:payment）。"""

from datetime import date, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import require_feature
from app.modules.finance import export, service
from app.modules.finance.schemas import (
    Bucket,
    CollectIn,
    CustomerReceivablePage,
    CustomerSort,
    CustomerStatement,
    FollowupIn,
    ReceivableFilters,
    ReceivableOut,
    ReceivablePage,
    ReceivableSummary,
    RecentPaymentList,
    Sort,
    StaffOptionList,
    View,
)
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.orders.schemas import PaymentMethodValue

router = APIRouter(prefix="/api/v1/finance", tags=["finance"], responses=ERROR_RESPONSES)
Context = Annotated[AppContext, Depends(get_context)]


def _feature(*permissions: str):  # type: ignore[no-untyped-def]
    async def dependency(
        principal: Annotated[Principal, Depends(require_permission(*permissions))],
        session: TenantDb,
    ) -> Principal:
        await require_feature(session, principal.tenant_id, "orders")
        return principal

    return dependency


CanView = Annotated[Principal, Depends(_feature(Permission.FINANCE_VIEW))]
CanManage = Annotated[
    Principal, Depends(_feature(Permission.FINANCE_VIEW, Permission.FINANCE_MANAGE))
]


@router.get("/receivables/summary", response_model=ReceivableSummary)
async def receivable_summary(session: TenantDb, principal: CanView) -> ReceivableSummary:
    """应收合计、逾期、今天到期、7 天内到期、本月已收、账龄分段（全公司）。"""
    cal = await service.calendar(session, principal.tenant_id)
    return await service.summary(session, cal)


@router.get("/receivables", response_model=ReceivablePage)
async def list_receivables(
    session: TenantDb,
    principal: CanView,
    view: View = "open",
    bucket: Bucket | None = None,
    payment_method: PaymentMethodValue | None = None,
    assignee_id: UUID | None = None,
    customer_id: UUID | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    sort: Sort = "due",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReceivablePage:
    """未收清的订单（按订单）：范围、账龄分段、收款方式、处理人、客户、关键字筛选；默认按到期日。"""
    cal = await service.calendar(session, principal.tenant_id)
    filters = ReceivableFilters(
        view=view,
        bucket=bucket,
        payment_method=payment_method,
        assignee_id=assignee_id,
        customer_id=customer_id,
        q=q,
    )
    return await service.list_receivables(
        session, cal, filters, sort=sort, limit=limit, offset=offset
    )


@router.get("/receivables/customers", response_model=CustomerReceivablePage)
async def list_customers(
    session: TenantDb,
    principal: CanView,
    q: Annotated[str | None, Query(max_length=100)] = None,
    overdue_only: bool = False,
    sort: CustomerSort = "outstanding",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CustomerReceivablePage:
    """按客户汇总：未收合计、逾期金额、订单数、最早到期日、最近收款和跟进。"""
    cal = await service.calendar(session, principal.tenant_id)
    return await service.list_customers(
        session, cal, q=q, overdue_only=overdue_only, sort=sort, limit=limit, offset=offset
    )


@router.get("/receivables/recent-payments", response_model=RecentPaymentList)
async def recent_payments(
    session: TenantDb,
    principal: CanView,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> RecentPaymentList:
    """最近登记的收款和退款（首页）。"""
    return await service.recent_payments(session, limit=limit)


@router.get("/staff", response_model=StaffOptionList)
async def staff_options(session: TenantDb, _: CanView) -> StaffOptionList:
    """启用状态的员工：筛选处理人、指定催收人（财务没有查看员工的权限）。"""
    return await service.staff_options(session)


@router.post(
    "/receivables/export",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/csv": {}}, "description": "CSV 文件"}},
)
async def export_receivables(
    payload: ReceivableFilters,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> StreamingResponse:
    """导出当前筛选的应收明细（CSV，不含收货信息和联系方式）。每次导出记审计。"""
    cal = await service.calendar(session, principal.tenant_id)
    total = await service.count(session, cal, payload)
    record_audit(
        session,
        action="finance.export",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="order",
        detail={"rows": total, "view": payload.view, "bucket": payload.bucket, "q": payload.q},
        ip=client_ip(request),
    )
    await session.commit()
    name = f"receivables-{cal.today:%Y%m%d}.csv"
    return StreamingResponse(
        export.rows(ctx, principal, cal, payload),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/receivables/{order_id}/followup", response_model=ReceivableOut)
async def follow_up(
    order_id: UUID, payload: FollowupIn, session: TenantDb, principal: CanManage
) -> ReceivableOut:
    """记一次跟进：客户承诺的付款日和备注（写入订单动态）。"""
    order = await service.follow_up(session, principal, order_id, payload)
    cal = await service.calendar(session, principal.tenant_id)
    return await service.one(session, cal, order.id)


@router.post("/receivables/{order_id}/collect", response_model=ReceivableOut)
async def collect(
    order_id: UUID,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
    payload: CollectIn | None = None,
) -> ReceivableOut:
    """生成一条"催收"待办，默认交给订单处理人；一个订单同时只有一条未完成的催收待办。"""
    order = await service.collect(ctx, session, principal, order_id, payload or CollectIn())
    cal = await service.calendar(session, principal.tenant_id)
    return await service.one(session, cal, order.id)


@router.get("/customers/{customer_id}/statement", response_model=CustomerStatement)
async def customer_statement(
    customer_id: UUID,
    session: TenantDb,
    principal: CanView,
    period_from: Annotated[date | None, Query(alias="from")] = None,
    period_to: Annotated[date | None, Query(alias="to")] = None,
) -> CustomerStatement:
    """客户对账单：期初未收、期间内的订单和收款、期末未收；默认本月。"""
    cal = await service.calendar(session, principal.tenant_id)
    end = period_to or cal.today
    start = period_from or min(end, cal.today.replace(day=1))
    if end < start:
        end = start
    if (end - start) > timedelta(days=366 * 3):
        start = end - timedelta(days=366 * 3)
    name = await session.scalar(select(Staff.display_name).where(Staff.id == principal.staff_id))
    return await service.statement(session, cal, customer_id, start, end, generated_by=name or "")
