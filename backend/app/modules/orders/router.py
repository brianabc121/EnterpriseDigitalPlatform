"""订单接口（设计文档 §25.5、§25.9）。查看订单需要 order:read，数据范围与客户一致。"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.deps import client_ip, get_context, get_rate_limiter
from app.core.errors import ERROR_RESPONSES, NotFound
from app.core.permissions import Permission
from app.core.ratelimit import PASSWORD_CHECK, RateLimiter
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import require_feature
from app.modules.customer.export import confirm_password
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.orders import actions, extract, queries, service
from app.modules.orders import export as order_export
from app.modules.orders import settings as order_settings
from app.modules.orders.models import Order, OrderRevision
from app.modules.orders.schemas import (
    NoticeRequest,
    NotifyFlag,
    OrderAssignRequest,
    OrderCancelRequest,
    OrderConfirmRequest,
    OrderCounts,
    OrderCreate,
    OrderDetail,
    OrderExportRequest,
    OrderExtractRequest,
    OrderNotice,
    OrderPage,
    OrderRevisionDetail,
    OrderRevisionList,
    OrderSourceValue,
    OrderStatusValue,
    OrderSuggestion,
    OrderUpdate,
    PaymentIn,
    ReceiverOut,
    ShipRequest,
    View,
    VoidRequest,
)
from app.modules.orders.settings import OrderSettings

router = APIRouter(prefix="/api/v1", tags=["orders"], responses=ERROR_RESPONSES)
Context = Annotated[AppContext, Depends(get_context)]


def _feature(*permissions: str):  # type: ignore[no-untyped-def]
    async def dependency(
        principal: Annotated[Principal, Depends(require_permission(*permissions))],
        session: TenantDb,
    ) -> Principal:
        await require_feature(session, principal.tenant_id, "orders")
        return principal

    return dependency


CanRead = Annotated[Principal, Depends(_feature(Permission.ORDER_READ))]
CanCreate = Annotated[Principal, Depends(_feature(Permission.ORDER_READ, Permission.ORDER_CREATE))]
CanReview = Annotated[Principal, Depends(_feature(Permission.ORDER_READ, Permission.ORDER_REVIEW))]
CanPay = Annotated[Principal, Depends(_feature(Permission.ORDER_READ, Permission.ORDER_PAYMENT))]
CanReveal = Annotated[
    Principal,
    Depends(_feature(Permission.ORDER_READ, Permission.CUSTOMER_VIEW_SENSITIVE)),
]
CanConfig = Annotated[Principal, Depends(_feature(Permission.ORDER_CONFIG))]
CanExport = Annotated[Principal, Depends(_feature(Permission.ORDER_READ, Permission.ORDER_EXPORT))]
Limiter = Annotated[RateLimiter, Depends(get_rate_limiter)]


class OrderResult(BaseModel):
    order: OrderDetail
    notice: OrderNotice | None = None


async def _detail(
    ctx: AppContext, session: AsyncSession, principal: Principal, order: Order
) -> OrderDetail:
    settings = await order_settings.load(session, principal.tenant_id)
    await session.refresh(order)
    return await queries.detail(ctx, session, principal, order, settings)


async def _result(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order: Order,
    notice: OrderNotice | None,
) -> OrderResult:
    return OrderResult(order=await _detail(ctx, session, principal, order), notice=notice)


@router.get("/orders", response_model=OrderPage)
async def list_orders(
    session: TenantDb,
    principal: CanRead,
    view: View = "all",
    status_: Annotated[OrderStatusValue | None, Query(alias="status")] = None,
    source: OrderSourceValue | None = None,
    assignee_id: UUID | None = None,
    customer_id: UUID | None = None,
    session_id: UUID | None = None,
    q: Annotated[
        str | None, Query(max_length=64, description="订单号、企业系统单号或客户名称")
    ] = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    min_total: Annotated[Decimal | None, Query(ge=0)] = None,
    max_total: Annotated[Decimal | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderPage:
    """订单中心：全部、待审核、处理中、应收（未收清）、修改过的。"""
    return await queries.list_orders(
        session,
        principal,
        view=view,
        status=status_,
        source=source,
        assignee_id=assignee_id,
        customer_id=customer_id,
        session_id=session_id,
        q=q,
        created_from=created_from,
        created_to=created_to,
        min_total=min_total,
        max_total=max_total,
        limit=limit,
        offset=offset,
    )


@router.get("/orders/counts", response_model=OrderCounts)
async def order_counts(session: TenantDb, principal: CanRead) -> OrderCounts:
    return await queries.counts(session, principal)


@router.get("/orders/settings", response_model=OrderSettings)
async def get_order_settings(session: TenantDb, principal: CanRead) -> OrderSettings:
    """订单设置（新建和审核订单时用到：启用的收款方式、必填项、发货环节、折扣上限等）。"""
    return await order_settings.load(session, principal.tenant_id)


@router.post("/orders", response_model=OrderDetail, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreate, ctx: Context, session: TenantDb, principal: CanCreate
) -> OrderDetail:
    """员工新建订单（草稿或直接提交审核）。单价不传时按建议零售价；改价需要 order:price。"""
    order = await actions.create(ctx, session, principal, payload)
    return await _detail(ctx, session, principal, order)


@router.post(
    "/orders/export",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/csv": {}}, "description": "CSV 文件"}},
)
async def export_orders(
    payload: OrderExportRequest,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanExport,
    limiter: Limiter,
) -> StreamingResponse:
    """导出数据范围内、符合筛选条件的订单（CSV）。需要再次输入密码；没有查看敏感信息的权限时，
    收货信息导出掩码；有查看成本价的权限时另外导出成本合计。"""
    await limiter.check(PASSWORD_CHECK, str(principal.staff_id))
    await confirm_password(session, principal, payload.password)
    where = queries.conditions(
        principal,
        view=payload.view,
        status=payload.status,
        source=payload.source,
        q=payload.q,
        created_from=payload.created_from,
        created_to=payload.created_to,
    )
    plaintext = principal.has(Permission.CUSTOMER_VIEW_SENSITIVE)
    cost = principal.has(Permission.PRODUCT_VIEW_COST)
    total = await order_export.count(session, where)
    record_audit(
        session,
        action="order.export",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="order",
        detail={
            "rows": total,
            "plaintext": plaintext,
            "cost": cost,
            "view": payload.view,
            "status": payload.status,
            "q": payload.q,
        },
        ip=client_ip(request),
    )
    await session.commit()
    name = f"orders-{date.today():%Y%m%d}.csv"
    return StreamingResponse(
        order_export.rows(ctx, principal, where, plaintext=plaintext, cost=cost),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/orders/extract", response_model=OrderSuggestion)
async def extract_order(
    payload: OrderExtractRequest, ctx: Context, session: TenantDb, principal: CanCreate
) -> OrderSuggestion:
    """AI 预填订单：从选中的消息（或最近的对话）、粘贴的客户的话里整理商品、收货信息和付款方式，
    商品对应到商品库（只有对客可见的字段和建议零售价）。不保存，员工核对后新建订单。"""
    return await extract.prefill(ctx, session, principal, payload)


@router.get("/orders/{order_id}", response_model=OrderDetail)
async def get_order(
    order_id: UUID, ctx: Context, session: TenantDb, principal: CanRead
) -> OrderDetail:
    order = await service.get_visible(session, principal, order_id)
    return await _detail(ctx, session, principal, order)


@router.patch("/orders/{order_id}", response_model=OrderResult)
async def update_order(
    order_id: UUID,
    payload: OrderUpdate,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanRead,
) -> OrderResult:
    """修改订单（带上打开时的版本号）。改价、改商品、改数量时必须选择原因；每次修改都记录修改人、
    前后差异和原因，不需要客户再次确认，可以选择把最新内容告知客户。"""
    order, notice = await actions.update(
        ctx, session, principal, order_id, payload, ip=client_ip(request)
    )
    return await _result(ctx, session, principal, order, notice)


@router.post("/orders/{order_id}/submit", response_model=OrderDetail)
async def submit_order(
    order_id: UUID, ctx: Context, session: TenantDb, principal: CanRead
) -> OrderDetail:
    order = await actions.submit(ctx, session, principal, order_id)
    return await _detail(ctx, session, principal, order)


@router.post("/orders/{order_id}/confirm", response_model=OrderResult)
async def confirm_order(
    order_id: UUID,
    payload: OrderConfirmRequest,
    ctx: Context,
    session: TenantDb,
    principal: CanReview,
) -> OrderResult:
    """确认订单：确定收款方式（暂欠需要 order:credit），并把确认信息和跟踪链接发给客户。"""
    order, notice = await actions.confirm(ctx, session, principal, order_id, payload)
    return await _result(ctx, session, principal, order, notice)


@router.post("/orders/{order_id}/start", response_model=OrderDetail)
async def start_order(
    order_id: UUID, ctx: Context, session: TenantDb, principal: CanReview
) -> OrderDetail:
    """开始处理（满足收款方式的条件：在线收款已收清、预付定金已收到定金、暂欠已同意）。"""
    order = await actions.start(ctx, session, principal, order_id)
    return await _detail(ctx, session, principal, order)


@router.post("/orders/{order_id}/ship", response_model=OrderResult)
async def ship_order(
    order_id: UUID, payload: ShipRequest, ctx: Context, session: TenantDb, principal: CanReview
) -> OrderResult:
    order, notice = await actions.ship(ctx, session, principal, order_id, payload)
    return await _result(ctx, session, principal, order, notice)


@router.post("/orders/{order_id}/complete", response_model=OrderResult)
async def complete_order(
    order_id: UUID, payload: NotifyFlag, ctx: Context, session: TenantDb, principal: CanReview
) -> OrderResult:
    order, notice = await actions.complete(ctx, session, principal, order_id, payload)
    return await _result(ctx, session, principal, order, notice)


@router.post("/orders/{order_id}/cancel", response_model=OrderResult)
async def cancel_order(
    order_id: UUID,
    payload: OrderCancelRequest,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanRead,
) -> OrderResult:
    """取消订单（草稿由新建的员工取消，其余需要 order:review）。有收款时请登记退款。"""
    order, notice = await actions.cancel(
        ctx, session, principal, order_id, payload, ip=client_ip(request)
    )
    return await _result(ctx, session, principal, order, notice)


@router.post("/orders/{order_id}/payments", response_model=OrderDetail)
async def add_payment(
    order_id: UUID,
    payload: PaymentIn,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanPay,
) -> OrderDetail:
    """登记收款或退款（平台只登记，不直接收款）。收款状态由记录自动计算。"""
    order = await actions.add_payment(
        ctx, session, principal, order_id, payload, ip=client_ip(request)
    )
    return await _detail(ctx, session, principal, order)


@router.post("/orders/{order_id}/payments/{payment_id}/void", response_model=OrderDetail)
async def void_payment(
    order_id: UUID,
    payment_id: UUID,
    payload: VoidRequest,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanPay,
) -> OrderDetail:
    """作废收款记录（填写原因，记入修改记录和审计日志）。"""
    order = await actions.void_payment(
        ctx, session, principal, order_id, payment_id, payload, ip=client_ip(request)
    )
    return await _detail(ctx, session, principal, order)


@router.post("/orders/{order_id}/reveal", response_model=ReceiverOut)
async def reveal_receiver(
    order_id: UUID, request: Request, ctx: Context, session: TenantDb, principal: CanReveal
) -> ReceiverOut:
    """查看完整的收货信息（记审计日志）。"""
    return ReceiverOut(
        receiver=await actions.reveal(ctx, session, principal, order_id, ip=client_ip(request))
    )


@router.post("/orders/{order_id}/tracking-link", response_model=OrderDetail)
async def regenerate_tracking_link(
    order_id: UUID, ctx: Context, session: TenantDb, principal: CanReview
) -> OrderDetail:
    """重新生成跟踪链接（旧链接立即失效）。"""
    order = await actions.regenerate_link(ctx, session, principal, order_id)
    return await _detail(ctx, session, principal, order)


@router.post("/orders/{order_id}/assign", response_model=OrderDetail)
async def assign_order(
    order_id: UUID,
    payload: OrderAssignRequest,
    ctx: Context,
    session: TenantDb,
    principal: CanReview,
) -> OrderDetail:
    order = await actions.assign(ctx, session, principal, order_id, payload)
    return await _detail(ctx, session, principal, order)


@router.post("/orders/{order_id}/notify", response_model=OrderNotice)
async def notify_customer(
    order_id: UUID, payload: NoticeRequest, ctx: Context, session: TenantDb, principal: CanReview
) -> OrderNotice:
    """按客户所在渠道通知客户（官网访客发系统消息；微信客服在回复窗口内发送；企业微信客户
    需要员工在侧边栏发送）。"""
    return await actions.notify(ctx, session, principal, order_id, payload.text)


@router.get("/orders/{order_id}/revisions", response_model=OrderRevisionList)
async def list_revisions(
    order_id: UUID, session: TenantDb, principal: CanRead
) -> OrderRevisionList:
    """修改记录：每个版本的修改人、时间、原因和前后差异。"""
    order = await service.get_visible(session, principal, order_id)
    rows = (await session.scalars(service.revisions_query(order.id))).all()
    staff = await queries.names(session, Staff, Staff.display_name, (r.actor_id for r in rows))
    return OrderRevisionList(items=[queries.revision_out(r, staff) for r in rows])


@router.get("/orders/{order_id}/revisions/{version}", response_model=OrderRevisionDetail)
async def get_revision(
    order_id: UUID, version: int, session: TenantDb, principal: CanRead
) -> OrderRevisionDetail:
    """某个版本的完整内容（可以与 AI 最初生成的第 1 版对比）。"""
    order = await service.get_visible(session, principal, order_id)
    row = await session.scalar(
        select(OrderRevision).where(
            OrderRevision.order_id == order.id, OrderRevision.version == version
        )
    )
    if row is None:
        raise NotFound("这个版本不存在")
    staff = await queries.names(session, Staff, Staff.display_name, [row.actor_id])
    return OrderRevisionDetail(
        **queries.revision_out(row, staff).model_dump(), snapshot=row.snapshot
    )


@router.get("/admin/order-settings", response_model=OrderSettings)
async def get_admin_settings(session: TenantDb, principal: CanConfig) -> OrderSettings:
    return await order_settings.load(session, principal.tenant_id)


@router.put("/admin/order-settings", response_model=OrderSettings)
async def put_admin_settings(
    payload: OrderSettings, session: TenantDb, principal: CanConfig
) -> OrderSettings:
    saved = await order_settings.save(session, principal.tenant_id, payload, principal.staff_id)
    await session.commit()
    return saved
