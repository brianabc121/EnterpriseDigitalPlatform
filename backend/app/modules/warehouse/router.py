"""仓库接口（设计文档 §25.13）：材料和成品的库存、领料单和入库单、库存记录、仓库设置（仓管）。

- 查看库存和单据、开不关联订单的单据：inventory:manage（仓管另外获得）；工人（production:work）
  可以查库存、给自己加工的订单开领料单，只看到自己开的和自己加工的订单的单据。
- 确认或退回单据：warehouse:confirm（仓管另外获得）。
- 仓库设置：order:config。
属于订单功能（套餐不含订单时不可用）。
"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request, status
from sqlalchemy import ColumnElement, and_, func, or_, select

from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES, Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import require_feature
from app.modules.iam.deps import CurrentPrincipal, TenantDb, require_permission
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.orders.models import Order
from app.modules.products import lookup, stock, suggest
from app.modules.products.models import Product, ProductKind, ProductMaterial, StockKind
from app.modules.products.models import StockMovement as Movement
from app.modules.products.schemas import CategoryList, StockMovementPage
from app.modules.warehouse import documents, movements
from app.modules.warehouse import settings as warehouse_settings
from app.modules.warehouse.models import DocumentKind, DocumentStatus
from app.modules.warehouse.schemas import (
    DocumentConfirm,
    DocumentDraft,
    DocumentIn,
    DocumentKindValue,
    DocumentOut,
    DocumentPage,
    DocumentStatusValue,
    DocumentUpdate,
    LinkableOrder,
    LinkableOrderList,
    ReasonIn,
    StockItemOut,
    StockItemPage,
    StockSuggestion,
    StockSuggestions,
    WarehouseCounts,
    WarehouseSettingsIn,
    WarehouseSettingsOut,
)

router = APIRouter(prefix="/api/v1/warehouse", tags=["warehouse"], responses=ERROR_RESPONSES)

WORK = (Permission.PRODUCTION_WORK, Permission.INVENTORY_MANAGE, Permission.WAREHOUSE_CONFIRM)
MANAGE = (Permission.INVENTORY_MANAGE, Permission.WAREHOUSE_CONFIRM)


def _any_of(*permissions: str):  # type: ignore[no-untyped-def]
    async def dependency(principal: CurrentPrincipal, session: TenantDb) -> Principal:
        if not any(principal.has(p) for p in permissions):
            raise Forbidden("没有执行该操作的权限")
        await require_feature(session, principal.tenant_id, "orders")
        return principal

    return dependency


async def _can_config(
    principal: Annotated[Principal, Depends(require_permission(Permission.ORDER_CONFIG))],
    session: TenantDb,
) -> Principal:
    await require_feature(session, principal.tenant_id, "orders")
    return principal


CanWork = Annotated[Principal, Depends(_any_of(*WORK))]
CanManage = Annotated[Principal, Depends(_any_of(*MANAGE))]
CanSeeSettings = Annotated[Principal, Depends(_any_of(*WORK, Permission.ORDER_CONFIG))]
CanConfig = Annotated[Principal, Depends(_can_config)]


# ---- 设置 ----


async def _settings_out(session: TenantDb, principal: Principal) -> WarehouseSettingsOut:
    value = await warehouse_settings.load(session, principal.tenant_id)
    keeper = await warehouse_settings.keeper(session, principal.tenant_id, value)
    ids = {i for i in (value.keeper_id, keeper.staff_id, *keeper.by_role) if i is not None}
    names = dict(
        (await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(ids)))).all()
    )
    if keeper.staff_id is not None:
        effective: str | None = names.get(keeper.staff_id)
    elif keeper.by_role:
        effective = "、".join(names[i] for i in keeper.by_role if i in names)
    else:
        effective = None
    return WarehouseSettingsOut(
        confirm_required=value.confirm_required,
        keeper_id=value.keeper_id,
        keeper_name=names.get(value.keeper_id) if value.keeper_id else None,
        effective_keeper_id=keeper.staff_id,
        effective_keeper_name=effective,
        fallback=keeper.fallback,
        by_role=bool(keeper.by_role),
        can_edit=principal.has(Permission.ORDER_CONFIG),
    )


@router.get("/settings", response_model=WarehouseSettingsOut)
async def get_settings(session: TenantDb, principal: CanSeeSettings) -> WarehouseSettingsOut:
    """仓库设置：谁是仓管（没有指定时由最早创建的工人担任）、单据是否需要仓管确认。"""
    return await _settings_out(session, principal)


@router.put("/settings", response_model=WarehouseSettingsOut)
async def put_settings(
    payload: WarehouseSettingsIn, request: Request, session: TenantDb, principal: CanConfig
) -> WarehouseSettingsOut:
    if payload.keeper_id is not None:
        found = await session.scalar(select(Staff.status).where(Staff.id == payload.keeper_id))
        if found != StaffStatus.ACTIVE:
            raise Unprocessable("仓管要是启用状态的员工")
    await warehouse_settings.save(
        session,
        principal.tenant_id,
        warehouse_settings.WarehouseSettings(
            confirm_required=payload.confirm_required, keeper_id=payload.keeper_id
        ),
        principal.staff_id,
    )
    record_audit(
        session,
        action="warehouse.settings",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="warehouse",
        detail={
            "confirm_required": payload.confirm_required,
            "keeper_id": str(payload.keeper_id) if payload.keeper_id else None,
        },
        ip=client_ip(request),
    )
    await session.commit()
    return await _settings_out(session, principal)


# ---- 库存 ----


@router.get("/counts", response_model=WarehouseCounts)
async def counts(session: TenantDb, _: CanManage) -> WarehouseCounts:
    """待确认的单据数和库存不足的数量（菜单和标签页上的数字）。"""
    pending = await documents.pending_counts(session)
    reserved = stock.reserved_subquery()
    joined = Product.__table__.outerjoin(reserved, reserved.c.product_id == Product.id)
    low = dict(
        (
            await session.execute(
                select(Product.kind, func.count())
                .select_from(joined)
                .where(stock.low_condition(reserved))
                .group_by(Product.kind)
            )
        ).all()
    )
    return WarehouseCounts(
        pending_requisitions=pending.get(DocumentKind.REQUISITION, 0),
        pending_receipts=pending.get(DocumentKind.RECEIPT, 0),
        low_materials=int(low.get(ProductKind.MATERIAL, 0)),
        low_goods=int(low.get(ProductKind.GOODS, 0)),
    )


def _item_filters(
    kind: ProductKind, q: str | None, category: str | None, status_: str | None
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [Product.kind == kind]
    if q and q.strip():
        like = f"%{q.strip()}%"
        # 也按联想的检索键找（§25.16）：拼音首字母、不同写法的代码和规格、几个词组合。
        by_key = suggest.keyword_condition(q)
        conditions.append(
            or_(
                Product.name.ilike(like),
                Product.code.ilike(like),
                Product.model.ilike(like),
                Product.spec.ilike(like),
                func.array_to_string(Product.aliases, " ").ilike(like),
                *([by_key] if by_key is not None else []),
            )
        )
    if category:
        conditions.append(
            or_(Product.category == category, Product.category.startswith(f"{category}/"))
        )
    if status_:
        conditions.append(Product.status == status_)
    return conditions


@router.get("/categories", response_model=CategoryList)
async def list_categories(
    session: TenantDb,
    _: CanWork,
    kind: Annotated[Literal["goods", "material"], Query(description="成品或材料")] = "material",
) -> CategoryList:
    """成品或材料的分类（开单时"批量选择"按分类筛选）。"""
    rows = await session.scalars(
        select(Product.category)
        .where(Product.category != "", Product.kind == kind)
        .distinct()
        .order_by(Product.category)
    )
    return CategoryList(items=list(rows))


@router.get("/items", response_model=StockItemPage)
async def list_items(
    session: TenantDb,
    _: CanWork,
    kind: Annotated[Literal["goods", "material"], Query(description="成品或材料")] = "material",
    q: Annotated[str | None, Query(max_length=100, description="名称、代码、型号、规格")] = None,
    category: Annotated[str | None, Query(max_length=128)] = None,
    status_: Annotated[str | None, Query(alias="status", pattern="^(on|off)$")] = None,
    stock_: Annotated[
        Literal["low", "tracked", "untracked"] | None,
        Query(alias="stock", description="low 库存不足、tracked 管理库存的、untracked 不管理的"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StockItemPage:
    """材料库存或成品库存（没有价格）：现有、占用（成品：要从库存发出的订单；材料：待确认的领料单）、
    可用和预警值。"""
    product_kind = ProductKind(kind)
    reserved = stock.reserved_subquery()
    conditions = _item_filters(product_kind, q, category, status_)
    if stock_ == "low":
        conditions.append(stock.low_condition(reserved))
    elif stock_ == "tracked":
        conditions.append(Product.stock.is_not(None))
    elif stock_ == "untracked":
        conditions.append(Product.stock.is_(None))
    joined = Product.__table__.outerjoin(reserved, reserved.c.product_id == Product.id)
    where = and_(*conditions)
    total = int(await session.scalar(select(func.count()).select_from(joined).where(where)) or 0)
    low = int(
        await session.scalar(
            select(func.count())
            .select_from(joined)
            .where(Product.kind == product_kind, stock.low_condition(reserved))
        )
        or 0
    )
    rows = list(
        (
            await session.scalars(
                select(Product)
                .select_from(joined)
                .where(where)
                .order_by(Product.category, Product.name, Product.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return StockItemPage(items=await item_outs(session, rows), total=total, low_stock=low)


@router.get("/suggest", response_model=StockSuggestions)
async def suggest_items(
    session: TenantDb,
    principal: CanWork,
    kind: Annotated[Literal["goods", "material"], Query(description="成品或材料")] = "material",
    q: Annotated[
        str,
        Query(
            max_length=100,
            description="输入的名称、代码、规格、拼音首字母等；为空时返回自己最近开单用过的",
        ),
    ] = "",
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
) -> StockSuggestions:
    """开领料单、入库单时的商品联想（§25.16）：启用的材料或成品，带库存（没有价格），按匹配程度、
    常用程度和库存排序。"""
    found, recent = await lookup.suggestions(
        session, principal, q, kind=ProductKind(kind), source="documents", limit=limit
    )
    outs = await item_outs(session, [s.product for s in found])
    return StockSuggestions(
        items=[
            StockSuggestion(item=out, score=s.score, field=s.field, match=s.match)
            for out, s in zip(outs, found, strict=True)
        ],
        recent=recent,
    )


async def item_outs(session: TenantDb, products: list[Product]) -> list[StockItemOut]:
    ids = [p.id for p in products]
    known = await stock.levels(session, ids)
    boms = dict(
        (
            await session.execute(
                select(ProductMaterial.product_id, func.count())
                .where(ProductMaterial.product_id.in_(ids))
                .group_by(ProductMaterial.product_id)
            )
        ).all()
    )
    result: list[StockItemOut] = []
    for p in products:
        level = known.get(p.id, stock.UNTRACKED)
        result.append(
            StockItemOut(
                id=p.id,
                code=p.code,
                name=p.name,
                model=p.model,
                spec=p.spec,
                category=p.category,
                unit=p.unit,
                kind=p.kind,
                ready_made=p.ready_made,
                status=p.status,
                stock=level.stock,
                stock_reserved=level.reserved,
                stock_available=level.available,
                stock_alert=p.stock_alert,
                stock_low=level.low,
                materials=int(boms.get(p.id, 0)),
                remark=p.remark,
            )
        )
    return result


@router.get("/movements", response_model=StockMovementPage)
async def list_movements(
    session: TenantDb,
    _: CanManage,
    kind: Annotated[Literal["goods", "material"] | None, Query(description="成品或材料")] = None,
    product_id: UUID | None = None,
    type_: Annotated[
        list[str] | None, Query(alias="type", description="变化的种类，例如 requisition、receipt")
    ] = None,
    q: Annotated[str | None, Query(max_length=100, description="商品名称或代码")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StockMovementPage:
    """库存记录：全部商品的每一次库存变化（新的在前）。"""
    conditions: list[ColumnElement[bool]] = []
    if kind is not None:
        conditions.append(Product.kind == kind)
    if product_id is not None:
        conditions.append(Movement.product_id == product_id)
    if type_:
        known = {k.value for k in StockKind}
        conditions.append(Movement.kind.in_([t for t in type_ if t in known]))
    if q and q.strip():
        like = f"%{q.strip()}%"
        conditions.append(or_(Product.name.ilike(like), Product.code.ilike(like)))
    base = select(Movement).join(Product, Product.id == Movement.product_id).where(*conditions)
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = list(
        (
            await session.scalars(
                base.order_by(Movement.created_at.desc(), Movement.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return StockMovementPage(items=await movements.outs(session, rows), total=total)


# ---- 单据 ----


@router.get("/drafts", response_model=DocumentDraft)
async def draft(
    session: TenantDb,
    principal: CanWork,
    kind: DocumentKindValue,
    order_id: UUID,
) -> DocumentDraft:
    """给订单开单的预填：领料单按配方（订单里需要加工的商品数量 × 配方用量，减去已经领过的），
    入库单按订单里需要加工的成品。"""
    order = await session.get(Order, order_id)
    mine = order is not None and order.worker_id == principal.staff_id
    if order is None or not (
        mine or principal.has(Permission.PRODUCTION_ASSIGN) or documents.manages(principal)
    ):
        raise NotFound(documents.ORDER_NOT_FOUND)
    if DocumentKind(kind) == DocumentKind.REQUISITION:
        lines, missing = await documents.requisition_draft(session, order)
    else:
        lines, missing = await documents.receipt_draft(session, order)
    return DocumentDraft(kind=kind, order_id=order.id, lines=lines, missing=missing)


@router.get("/orders", response_model=LinkableOrderList)
async def linkable_orders(
    session: TenantDb,
    principal: CanWork,
    q: Annotated[str | None, Query(max_length=32, description="订单号")] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> LinkableOrderList:
    """开领料单时可以关联的订单：已确认或处理中、有需要加工的商品、还没加工完成；工人只看自己
    领取的（仓库的员工和能指派加工的员工看全部）。"""
    from app.modules.orders import service as order_service
    from app.modules.orders.models import STATUS_LABELS as ORDER_STATUS
    from app.modules.orders.models import OrderItem, OrderStatus

    conditions: list[ColumnElement[bool]] = [
        Order.status.in_((OrderStatus.CONFIRMED, OrderStatus.FULFILLING)),
        Order.processed_at.is_(None),
        order_service.needs_production(),
    ]
    if not (documents.manages(principal) or principal.has(Permission.PRODUCTION_ASSIGN)):
        conditions.append(Order.worker_id == principal.staff_id)
    if q and q.strip():
        term = q.strip().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        conditions.append(Order.no.ilike(f"%{term}%"))
    orders = list(
        (
            await session.scalars(
                select(Order).where(*conditions).order_by(Order.created_at.desc()).limit(limit)
            )
        ).all()
    )
    items: dict[UUID, list[OrderItem]] = {o.id: [] for o in orders}
    if orders:
        for item in await session.scalars(
            select(OrderItem).where(OrderItem.order_id.in_(items)).order_by(OrderItem.sort)
        ):
            items[item.order_id].append(item)
    ready = await order_service.ready_made_ids(
        session, [i for rows in items.values() for i in rows]
    )
    workers = {o.worker_id for o in orders if o.worker_id}
    names: dict[UUID, str] = {}
    if workers:
        rows = await session.execute(
            select(Staff.id, Staff.display_name).where(Staff.id.in_(workers))
        )
        names = {r.id: r.display_name for r in rows}
    return LinkableOrderList(
        items=[
            LinkableOrder(
                id=o.id,
                no=o.no,
                status_label=ORDER_STATUS.get(o.status, o.status),
                worker_name=names.get(o.worker_id) if o.worker_id else None,
                items="、".join(
                    f"{order_service.item_label(i)} × {i.quantity}"
                    for i in documents.made_items(items[o.id], ready)
                ),
            )
            for o in orders
        ]
    )


@router.get("/documents", response_model=DocumentPage)
async def list_documents(
    session: TenantDb,
    principal: CanWork,
    kind: DocumentKindValue | None = None,
    status_: Annotated[DocumentStatusValue | None, Query(alias="status")] = None,
    order_id: UUID | None = None,
    q: Annotated[str | None, Query(max_length=64, description="单号、订单号或商品名称")] = None,
    mine: Annotated[bool, Query(description="只看我开的")] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentPage:
    """领料单和入库单（待确认的在前）。"""
    rows, total = await documents.page(
        session,
        principal,
        kind=DocumentKind(kind) if kind else None,
        status=DocumentStatus(status_) if status_ else None,
        order_id=order_id,
        q=q,
        mine=mine,
        limit=limit,
        offset=offset,
    )
    return DocumentPage(items=await documents.outs(session, principal, rows), total=total)


@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def create_document(
    payload: DocumentIn, request: Request, session: TenantDb, principal: CanWork
) -> DocumentOut:
    """开单：给订单开领料单（工人给自己加工的订单），或者仓库直接开单（不关联订单）。仓管开的、
    或者设置为不需要确认的，开单即确认（修改库存）。"""
    document = await documents.create(
        get_context(request),
        session,
        principal,
        kind=DocumentKind(payload.kind),
        order_id=payload.order_id,
        lines=payload.lines,
        note=payload.note,
    )
    return await documents.one(session, principal, document.id)


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(document_id: UUID, session: TenantDb, principal: CanWork) -> DocumentOut:
    return await documents.one(session, principal, document_id)


@router.put("/documents/{document_id}", response_model=DocumentOut)
async def update_document(
    document_id: UUID,
    payload: DocumentUpdate,
    request: Request,
    session: TenantDb,
    principal: CanWork,
) -> DocumentOut:
    """修改后重新提交（待确认、已退回的单据；开单人或仓管）。"""
    await documents.resubmit(
        get_context(request),
        session,
        principal,
        document_id,
        lines=payload.lines,
        note=payload.note,
    )
    return await documents.one(session, principal, document_id)


@router.post("/documents/{document_id}/confirm", response_model=DocumentOut)
async def confirm_document(
    document_id: UUID,
    request: Request,
    session: TenantDb,
    principal: CanWork,
    payload: Annotated[DocumentConfirm | None, Body()] = None,
) -> DocumentOut:
    """仓管确认：领料单扣减材料库存（不够也可以领，提示盘点），入库单增加成品库存；订单的入库单
    确认后订单加工完成、进入"待发货"。可以按实际数量修改。"""
    lines = payload.lines if payload else None
    quantities = {line.id: line.quantity for line in lines} if lines else None
    await documents.confirm(
        get_context(request), session, principal, document_id, quantities=quantities
    )
    return await documents.one(session, principal, document_id)


@router.post("/documents/{document_id}/reject", response_model=DocumentOut)
async def reject_document(
    document_id: UUID,
    payload: ReasonIn,
    request: Request,
    session: TenantDb,
    principal: CanWork,
) -> DocumentOut:
    """仓管退回（写明原因），开单人修改后重新提交。"""
    await documents.reject(get_context(request), session, principal, document_id, payload.reason)
    return await documents.one(session, principal, document_id)


@router.post("/documents/{document_id}/void", response_model=DocumentOut)
async def void_document(
    document_id: UUID,
    request: Request,
    session: TenantDb,
    principal: CanWork,
    payload: Annotated[ReasonIn | None, Body()] = None,
) -> DocumentOut:
    """作废还没生效的单据（开单人或仓管）。"""
    reason = payload.reason if payload else ""
    await documents.void(get_context(request), session, principal, document_id, reason)
    return await documents.one(session, principal, document_id)
