"""加工接口（设计文档 §25.11、§25.13）：工人（production:work）领取订单、标记商品完成或缺货、
完成加工（开入库单）；主管（production:assign）指派或改派加工人。领料单在仓库接口开
（/api/v1/warehouse/documents）。属于订单功能（套餐不含订单时不可用）。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.context import AppContext
from app.core.deps import get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.billing.entitlements import require_feature
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.orders import production
from app.modules.orders.schemas import (
    AssignWorkerIn,
    CompleteProductionIn,
    ProductionCounts,
    ProductionOrder,
    ProductionPage,
    ProductionView,
    ShortageIn,
    WorkerOption,
    WorkerOptions,
)

router = APIRouter(prefix="/api/v1/production", tags=["production"], responses=ERROR_RESPONSES)
Context = Annotated[AppContext, Depends(get_context)]


def _feature(permission: str):  # type: ignore[no-untyped-def]
    async def dependency(
        principal: Annotated[Principal, Depends(require_permission(permission))],
        session: TenantDb,
    ) -> Principal:
        await require_feature(session, principal.tenant_id, "orders")
        return principal

    return dependency


CanWork = Annotated[Principal, Depends(_feature(Permission.PRODUCTION_WORK))]
CanAssign = Annotated[Principal, Depends(_feature(Permission.PRODUCTION_ASSIGN))]


@router.get("/orders", response_model=ProductionPage)
async def list_orders(
    session: TenantDb,
    principal: CanWork,
    view: ProductionView = "mine",
    q: Annotated[str | None, Query(max_length=64, description="订单号或商品名称")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProductionPage:
    """待领取（pool）、我加工中的（mine）、我最近完成的（done）；有指派权限时还有全部加工中的（all）。"""
    return await production.list_orders(
        session, principal, view=view, q=q, limit=limit, offset=offset
    )


@router.get("/counts", response_model=ProductionCounts)
async def counts(session: TenantDb, principal: CanWork) -> ProductionCounts:
    return await production.counts(session, principal)


@router.get("/workers", response_model=WorkerOptions)
async def workers(session: TenantDb, principal: CanAssign) -> WorkerOptions:
    """可以指派的加工人。"""
    return WorkerOptions(
        items=[WorkerOption(id=i, name=n) for i, n in await production.workers(session)]
    )


@router.get("/orders/{order_id}", response_model=ProductionOrder)
async def get_order(order_id: UUID, session: TenantDb, principal: CanWork) -> ProductionOrder:
    return await production.get(session, principal, order_id)


@router.post("/orders/{order_id}/claim", response_model=ProductionOrder)
async def claim(
    order_id: UUID, ctx: Context, session: TenantDb, principal: CanWork
) -> ProductionOrder:
    """领取：已确认的订单同时开始处理。"""
    await production.claim(ctx, session, principal, order_id)
    return await production.get(session, principal, order_id)


@router.post("/orders/{order_id}/release", response_model=ProductionOrder)
async def release(
    order_id: UUID, ctx: Context, session: TenantDb, principal: CanWork
) -> ProductionOrder:
    """放弃：退回待领取。"""
    await production.release(ctx, session, principal, order_id)
    return await production.get(session, principal, order_id)


@router.post("/orders/{order_id}/items/{order_item_id}/done", response_model=ProductionOrder)
async def item_done(
    order_id: UUID, order_item_id: UUID, ctx: Context, session: TenantDb, principal: CanWork
) -> ProductionOrder:
    await production.set_done(ctx, session, principal, order_id, order_item_id, done=True)
    return await production.get(session, principal, order_id)


@router.post("/orders/{order_id}/items/{order_item_id}/undo", response_model=ProductionOrder)
async def item_undo(
    order_id: UUID, order_item_id: UUID, ctx: Context, session: TenantDb, principal: CanWork
) -> ProductionOrder:
    await production.set_done(ctx, session, principal, order_id, order_item_id, done=False)
    return await production.get(session, principal, order_id)


@router.put("/orders/{order_id}/items/{order_item_id}/shortage", response_model=ProductionOrder)
async def item_shortage(
    order_id: UUID,
    order_item_id: UUID,
    payload: ShortageIn,
    ctx: Context,
    session: TenantDb,
    principal: CanWork,
) -> ProductionOrder:
    """登记或修改缺货：订单进入订单中心的"缺货"，客服收到"缺货处理"待办。"""
    await production.mark_shortage(ctx, session, principal, order_id, order_item_id, payload)
    return await production.get(session, principal, order_id)


@router.post("/orders/{order_id}/items/{order_item_id}/restock", response_model=ProductionOrder)
async def item_restock(
    order_id: UUID, order_item_id: UUID, ctx: Context, session: TenantDb, principal: CanWork
) -> ProductionOrder:
    """到货：缺货的商品回到待加工。"""
    await production.worker_restock(ctx, session, principal, order_id, order_item_id)
    return await production.get(session, principal, order_id)


@router.post("/orders/{order_id}/complete", response_model=ProductionOrder)
async def complete(
    order_id: UUID,
    payload: CompleteProductionIn,
    ctx: Context,
    session: TenantDb,
    principal: CanWork,
) -> ProductionOrder:
    """完成加工：开入库单（生产好的成品），仓管确认入库后订单进入"待发货"，客服在待办里收到提醒；
    没有要入库的成品时直接进入"待发货"。"""
    await production.complete(
        ctx,
        session,
        principal,
        order_id,
        mark_all=payload.mark_all,
        lines=payload.lines,
        note=payload.note,
    )
    return await production.get(session, principal, order_id)


@router.post("/orders/{order_id}/assign", response_model=ProductionOrder)
async def assign(
    order_id: UUID,
    payload: AssignWorkerIn,
    ctx: Context,
    session: TenantDb,
    principal: CanAssign,
) -> ProductionOrder:
    """主管指派或改派加工人（为空时退回待领取）。"""
    await production.assign(ctx, session, principal, order_id, payload.worker_id)
    return await production.get(session, principal, order_id)
