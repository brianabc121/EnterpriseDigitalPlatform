"""库存（设计文档 §25.12、§25.13）：现有、占用和可用库存，库存调整与库存记录，订单出库与退回。

- 库存分成品库存和材料库存（商品的类别）。现有库存（products.stock）为空表示不管理这个商品的
  库存（材料总是管理）；可以是负数（发出多于记录的库存，提示需要盘点）。数量最多三位小数，成品只用
  整数。
- 成品的占用：已确认、还没发货的订单（已确认、处理中）里要从库存发出的数量：现货商品下单即占用；
  需要加工的商品在加工完成、入库之后才占用（入库之前它还在生产，不看库存）。可用 = 现有 − 占用。
- 材料的占用（待领）：还没确认的领料单里的数量。
- 订单发货时出库（没有发货环节的在完成时），每个订单只扣一次（orders.stock_out_at）；已出库的
  订单被取消时（只有企业系统回传可能出现）按出库记录退回。
- 库存不足只提示、不拦截：从库存发货的订单行按确认的先后依次占用现有库存，排在后面、占不到的标为
  库存不足；还没确认的订单按可用库存判断。
- 每一次变化都写库存记录（变化前后的数量、原因、操作人，关联订单、导入或单据）。
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import ColumnElement, Subquery, and_, case, cast, func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, Unprocessable
from app.modules.orders.models import Order, OrderItem, OrderStatus
from app.modules.products.models import (
    QTY,
    Product,
    ProductKind,
    StockKind,
    StockMovement,
)
from app.modules.warehouse.models import (
    DocumentKind,
    DocumentStatus,
    StockDocument,
    StockDocumentLine,
)

# 占用库存的订单状态：已确认、还没发货（处理中）。
OPEN = (OrderStatus.CONFIRMED, OrderStatus.FULFILLING)
# 还没占用库存、按可用库存判断够不够的订单状态。
UNCONFIRMED = (OrderStatus.DRAFT, OrderStatus.PENDING_REVIEW)
MAX_STOCK = Decimal(100_000_000)
ZERO = Decimal(0)
AdjustMode = Literal["set", "add", "remove", "untrack"]

KIND_LABELS = {
    StockKind.IMPORT_SET: "导入盘点",
    StockKind.IMPORT_ADD: "导入入库",
    StockKind.ADJUST_SET: "盘点",
    StockKind.ADJUST_ADD: "入库",
    StockKind.ADJUST_REMOVE: "出库",
    StockKind.UNTRACK: "不再管理库存",
    StockKind.ORDER_OUT: "订单出库",
    StockKind.ORDER_RETURN: "订单取消退回",
    StockKind.API_SET: "企业系统同步",
    StockKind.REQUISITION: "领料",
    StockKind.RECEIPT: "生产入库",
}


def fmt(value: Decimal | int | None) -> str:
    """数量的显示：去掉多余的 0（2.500 → 2.5，10.000 → 10）；为空时是"—"。"""
    if value is None:
        return "—"
    text = f"{Decimal(value).normalize():f}"
    return "0" if text in ("-0", "0") else text


def is_whole(value: Decimal) -> bool:
    return value == value.to_integral_value()


def check_quantity(product: Product, value: Decimal, *, label: str = "数量") -> None:
    """成品的数量只能是整数，材料最多三位小数。"""
    if product.kind == ProductKind.GOODS and not is_whole(value):
        raise Unprocessable(f"「{product.name}」是成品，{label}只能是整数")
    if value != value.quantize(Decimal("0.001")):
        raise Unprocessable(f"{label}最多三位小数")


@dataclass(frozen=True)
class Actor:
    type: str  # staff / api / system
    id: uuid.UUID | None


@dataclass(frozen=True)
class Level:
    """一个商品的库存：现有（为空表示不管理库存）、占用（成品）或待领（材料）、预警值。"""

    stock: Decimal | None
    reserved: Decimal
    alert: Decimal | None
    kind: str = ProductKind.GOODS
    ready_made: bool = False

    @property
    def tracked(self) -> bool:
        return self.stock is not None

    @property
    def available(self) -> Decimal | None:
        return None if self.stock is None else self.stock - self.reserved

    @property
    def low(self) -> bool:
        """库存不足：可用库存为负数，或不高于预警值（材料和现货没有设预警值时按 0；需要加工的
        成品只在设了预警值时比较）。"""
        available = self.available
        if available is None:
            return False
        threshold = self.alert
        if threshold is None and (self.kind == ProductKind.MATERIAL or self.ready_made):
            threshold = ZERO
        return available < 0 or (threshold is not None and available <= threshold)


UNTRACKED = Level(None, ZERO, None)


@dataclass(frozen=True)
class LineStock:
    """订单行的库存情况：available 为空表示不看库存（不管理库存、需要加工还没入库，或者订单已经
    出库、取消）。"""

    available: Decimal | None = None
    short: bool = False


# ---- 查询 ----


def _from_stock() -> ColumnElement[bool]:
    """订单行从库存发货：现货商品，或者订单已经加工完成（成品已入库）。"""
    return or_(Product.ready_made.is_(True), Order.processed_at.is_not(None))


def reserved_subquery() -> Subquery:
    """每个商品的占用：成品是已确认、还没发货的订单里要从库存发出的数量；材料是还没确认的领料单
    里的数量。"""
    orders = (
        select(
            OrderItem.product_id.label("product_id"),
            cast(OrderItem.quantity, QTY).label("qty"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(Order.status.in_(OPEN), _from_stock())
    )
    requisitions = (
        select(
            StockDocumentLine.product_id.label("product_id"),
            StockDocumentLine.quantity.label("qty"),
        )
        .join(StockDocument, StockDocument.id == StockDocumentLine.document_id)
        .where(
            StockDocument.kind == DocumentKind.REQUISITION,
            StockDocument.status == DocumentStatus.PENDING,
        )
    )
    both = union_all(orders, requisitions).subquery()
    return (
        select(both.c.product_id.label("product_id"), func.sum(both.c.qty).label("qty"))
        .group_by(both.c.product_id)
        .subquery()
    )


def low_condition(reserved: Subquery) -> ColumnElement[bool]:
    """库存不足（与 Level.low 一致），用于列表的筛选；需要 outer join reserved_subquery。"""
    available = Product.stock - func.coalesce(reserved.c.qty, 0)
    threshold = func.coalesce(
        Product.stock_alert,
        case(
            (or_(Product.kind == ProductKind.MATERIAL, Product.ready_made.is_(True)), 0),
            else_=None,
        ),
    )
    return and_(Product.stock.is_not(None), or_(available < 0, available <= threshold))


async def levels(session: AsyncSession, product_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Level]:
    ids = list({i for i in product_ids if i is not None})
    if not ids:
        return {}
    reserved = reserved_subquery()
    rows = await session.execute(
        select(
            Product.id,
            Product.stock,
            Product.stock_alert,
            Product.kind,
            Product.ready_made,
            reserved.c.qty,
        )
        .outerjoin(reserved, reserved.c.product_id == Product.id)
        .where(Product.id.in_(ids))
    )
    return {
        pid: Level(stock, Decimal(qty or 0), alert, kind, ready_made)
        for pid, stock, alert, kind, ready_made, qty in rows
    }


async def line_stock(
    session: AsyncSession, orders: Iterable[Order], items: Iterable[OrderItem]
) -> dict[uuid.UUID, LineStock]:
    """订单行的库存情况（订单详情、加工页），只看从库存发货的行（现货，或者订单已加工完成）：
    已确认、处理中的订单按确认先后依次占用现有库存；草稿和待审核的按可用库存判断；已发货、完成、
    取消的不再提示。"""
    orders = list(orders)
    status = {o.id: o.status for o in orders}
    processed = {o.id for o in orders if o.processed_at is not None}
    items = [i for i in items if i.product_id is not None and i.order_id in status]
    if not items:
        return {}
    known = await levels(session, (i.product_id for i in items if i.product_id))
    result: dict[uuid.UUID, LineStock] = {}
    open_ids: list[uuid.UUID] = []
    for item in items:
        level = known.get(item.product_id) if item.product_id else None
        if level is None or level.available is None:
            continue
        if not (level.ready_made or item.order_id in processed):
            continue
        if status[item.order_id] in UNCONFIRMED:
            result[item.id] = LineStock(level.available, level.available < item.quantity)
        elif status[item.order_id] in OPEN:
            open_ids.append(item.id)
            result[item.id] = LineStock(level.available)
    if open_ids:
        wanted = set(open_ids)
        products = list({i.product_id for i in items if i.id in wanted and i.product_id})
        upto = func.sum(OrderItem.quantity).over(
            partition_by=OrderItem.product_id,
            order_by=(
                Order.confirmed_at.asc().nulls_last(),
                Order.created_at,
                Order.id,
                OrderItem.sort,
                OrderItem.id,
            ),
            rows=(None, 0),
        )
        lines = (
            select(
                OrderItem.id.label("id"),
                OrderItem.product_id.label("product_id"),
                upto.label("upto"),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .join(Product, Product.id == OrderItem.product_id)
            .where(Order.status.in_(OPEN), OrderItem.product_id.in_(products), _from_stock())
            .subquery()
        )
        for item_id, product_id, total in await session.execute(
            select(lines.c.id, lines.c.product_id, lines.c.upto).where(lines.c.id.in_(open_ids))
        ):
            level = known[product_id]
            stock = level.stock if level.stock is not None else ZERO
            result[item_id] = LineStock(level.available, Decimal(total) > stock)
    return result


# ---- 修改 ----


async def lock(session: AsyncSession, product_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Product]:
    """按 ID 顺序锁定商品行（避免多个订单、单据同时修改库存时死锁）。"""
    ids = sorted(set(product_ids))
    if not ids:
        return {}
    rows = await session.scalars(
        select(Product).where(Product.id.in_(ids)).order_by(Product.id).with_for_update()
    )
    return {p.id: p for p in rows}


def record(
    session: AsyncSession,
    product: Product,
    kind: StockKind,
    after: Decimal | None,
    *,
    actor: Actor,
    order_id: uuid.UUID | None = None,
    import_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    note: str = "",
) -> StockMovement:
    """把商品的现有库存改为 after 并写一条库存记录（调用方已锁定商品行，由调用方提交）。"""
    before = product.stock
    if after is not None and not -MAX_STOCK <= after <= MAX_STOCK:
        raise Unprocessable("库存数量超出范围")
    movement = StockMovement(
        tenant_id=product.tenant_id,
        product_id=product.id,
        kind=kind.value,
        delta=(after or ZERO) - (before or ZERO),
        stock_before=before,
        stock_after=after,
        order_id=order_id,
        import_id=import_id,
        document_id=document_id,
        note=note.strip()[:500],
        actor_type=actor.type,
        actor_id=actor.id,
    )
    session.add(movement)
    product.stock = after
    return movement


def target(before: Decimal | None, mode: Literal["set", "add"], quantity: Decimal) -> Decimal:
    """盘点：表格里的数就是现有库存；入库：加到现有库存上（原来不管理库存的从 0 开始）。"""
    return quantity if mode == "set" else (before or ZERO) + quantity


async def adjust(
    session: AsyncSession,
    product_id: uuid.UUID,
    *,
    mode: AdjustMode,
    quantity: Decimal | None,
    note: str,
    actor: Actor,
) -> Product:
    """手动调整：盘点（改为这个数）、入库（增加）、出库（减少，不能超过现有库存）、不再管理库存
    （材料总是管理库存）。由调用方提交。"""
    product = (await lock(session, [product_id])).get(product_id)
    if product is None:
        raise NotFound("商品不存在")
    before = product.stock
    if mode == "untrack":
        if product.kind == ProductKind.MATERIAL:
            raise Unprocessable("材料总是管理库存")
        if before is not None:
            record(session, product, StockKind.UNTRACK, None, actor=actor, note=note)
        return product
    if quantity is None:
        raise Unprocessable("请填写数量")
    check_quantity(product, quantity)
    if mode == "remove":
        if before is None:
            raise Unprocessable("这个商品没有管理库存")
        if quantity > before:
            raise Unprocessable(f"出库数量不能超过现有库存 {fmt(before)}")
        record(session, product, StockKind.ADJUST_REMOVE, before - quantity, actor=actor, note=note)
        return product
    kind = StockKind.ADJUST_SET if mode == "set" else StockKind.ADJUST_ADD
    record(session, product, kind, target(before, mode, quantity), actor=actor, note=note)
    return product


async def ship_out(
    session: AsyncSession, order: Order, items: Iterable[OrderItem], *, actor: Actor, now: datetime
) -> None:
    """订单出库：发货时（没有发货环节的在完成时）按商品汇总数量扣减管理库存的成品，每个订单只扣
    一次（由调用方提交）。"""
    if order.stock_out_at is not None:
        return
    order.stock_out_at = now
    quantities: dict[uuid.UUID, Decimal] = {}
    for item in items:
        if item.product_id is not None:
            quantities[item.product_id] = quantities.get(item.product_id, ZERO) + item.quantity
    products = await lock(session, quantities)
    for product_id in sorted(quantities):
        product = products.get(product_id)
        if product is None or product.stock is None:
            continue
        record(
            session,
            product,
            StockKind.ORDER_OUT,
            product.stock - quantities[product_id],
            actor=actor,
            order_id=order.id,
            note=f"订单 {order.no}",
        )


async def return_order(session: AsyncSession, order: Order, *, actor: Actor) -> None:
    """已出库的订单被取消：按这个订单的出库记录退回（之后不再管理库存的商品不退）。由调用方提交。"""
    if order.stock_out_at is None:
        return
    order.stock_out_at = None
    net = dict(
        (
            await session.execute(
                select(StockMovement.product_id, func.sum(StockMovement.delta))
                .where(
                    StockMovement.order_id == order.id,
                    StockMovement.kind.in_((StockKind.ORDER_OUT, StockKind.ORDER_RETURN)),
                )
                .group_by(StockMovement.product_id)
            )
        ).all()
    )
    products = await lock(session, (pid for pid, delta in net.items() if delta < 0))
    for product_id in sorted(products):
        product = products[product_id]
        if product.stock is None:
            continue
        record(
            session,
            product,
            StockKind.ORDER_RETURN,
            product.stock - Decimal(net[product_id]),
            actor=actor,
            order_id=order.id,
            note=f"订单 {order.no} 取消",
        )


async def movements(
    session: AsyncSession, product_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[StockMovement], int]:
    where = StockMovement.product_id == product_id
    total = int(
        await session.scalar(select(func.count()).select_from(StockMovement).where(where)) or 0
    )
    rows = await session.scalars(
        select(StockMovement)
        .where(where)
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), total


def availability(level: Level | None) -> str | None:
    """给 AI 的库存状态：只对管理库存的现货商品说有没有现货，不说数量；需要加工的商品、不管理库存
    的商品不提。"""
    if level is None or level.available is None or not level.ready_made:
        return None
    return "有现货" if level.available > 0 else "暂时缺货"


def snapshot(level: Level) -> dict[str, Any]:
    return {
        "stock": level.stock,
        "reserved": level.reserved,
        "available": level.available,
        "alert": level.alert,
        "low": level.low,
    }
