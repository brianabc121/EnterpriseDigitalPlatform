"""按以往领料估算用量（设计文档 §25.17）：成品没有配方时，看最近几张只加工这一个商品的订单实际领了
多少材料（已确认的领料单，按仓管确认时的数量），除以商品数量得到每件的用量，取中位数。

- 加工好几种商品的订单分不清每种用了多少，不算；有没对应到商品库的行的订单也不算。
- 至少一半的订单都领过的材料才算（偶尔补领的不算）；已经停用的材料不算。
- 正在开单的订单本身不算（exclude）。
"""

import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders import service as order_service
from app.modules.orders.models import Order, OrderItem
from app.modules.products.models import Product, ProductStatus
from app.modules.warehouse.models import (
    DocumentKind,
    DocumentStatus,
    StockDocument,
    StockDocumentLine,
)

# 每个成品最多看最近几张订单；每个成品最多查最近多少张领过料的订单（其中只加工它的才算）。
ORDERS = 5
CANDIDATES = 30
# 材料的数量最多三位小数。
THOUSANDTH = Decimal("0.001")


@dataclass(frozen=True)
class Usage:
    """一个成品按以往领料估算的每件用量（材料 → 每件用多少，没有四舍五入），依据的订单数。"""

    per_unit: dict[uuid.UUID, Decimal]
    orders: int


def rounded(value: Decimal) -> Decimal:
    """材料的数量：四舍五入到三位小数。"""
    return value.quantize(THOUSANDTH, rounding=ROUND_HALF_UP)


async def history(
    session: AsyncSession, product_ids: set[uuid.UUID], *, exclude: uuid.UUID | None = None
) -> dict[uuid.UUID, Usage]:
    """这些成品按以往领料估算的每件用量（算不出来的不在结果里）。"""
    if not product_ids:
        return {}
    confirmed = exists().where(
        StockDocument.order_id == Order.id,
        StockDocument.kind == DocumentKind.REQUISITION,
        StockDocument.status == DocumentStatus.CONFIRMED,
    )
    # 每个成品各取自己最近的订单：做得多的商品不会把做得少的挤出候选。
    pairs = (
        select(
            OrderItem.product_id,
            Order.id.label("order_id"),
            func.row_number()
            .over(
                partition_by=OrderItem.product_id,
                order_by=(Order.created_at.desc(), Order.id.desc()),
            )
            .label("rank"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.product_id.in_(product_ids), confirmed)
        .group_by(OrderItem.product_id, Order.id, Order.created_at)
    )
    if exclude is not None:
        pairs = pairs.where(Order.id != exclude)
    ranked = pairs.subquery()
    candidates: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for product_id, order_id in await session.execute(
        select(ranked.c.product_id, ranked.c.order_id)
        .where(ranked.c.rank <= CANDIDATES)
        .order_by(ranked.c.product_id, ranked.c.rank)
    ):
        candidates[product_id].append(order_id)
    order_ids = {order_id for ids in candidates.values() for order_id in ids}
    if not order_ids:
        return {}
    items = list(await session.scalars(select(OrderItem).where(OrderItem.order_id.in_(order_ids))))
    ready = await order_service.ready_made_ids(session, items)
    by_order: dict[uuid.UUID, list[OrderItem]] = defaultdict(list)
    for item in items:
        by_order[item.order_id].append(item)

    # 每个成品：最近几张只加工它的订单，和订单里它的数量（新的在前）。
    chosen: dict[uuid.UUID, list[tuple[uuid.UUID, int]]] = defaultdict(list)
    for product_id, ids in candidates.items():
        for order_id in ids:
            made = [
                i for i in by_order[order_id] if i.product_id is None or i.product_id not in ready
            ]
            quantity = sum(i.quantity for i in made)
            if {i.product_id for i in made} == {product_id} and quantity > 0:
                chosen[product_id].append((order_id, quantity))
                if len(chosen[product_id]) == ORDERS:
                    break
    picked = [order_id for rows in chosen.values() for order_id, _ in rows]
    if not picked:
        return {}

    taken: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = defaultdict(dict)
    rows = await session.execute(
        select(
            StockDocument.order_id,
            StockDocumentLine.product_id,
            func.sum(StockDocumentLine.quantity),
        )
        .join(StockDocument, StockDocument.id == StockDocumentLine.document_id)
        .join(Product, Product.id == StockDocumentLine.product_id)
        .where(
            StockDocument.order_id.in_(picked),
            StockDocument.kind == DocumentKind.REQUISITION,
            StockDocument.status == DocumentStatus.CONFIRMED,
            Product.status == ProductStatus.ON,
        )
        .group_by(StockDocument.order_id, StockDocumentLine.product_id)
    )
    for document_order, material_id, total in rows:
        if document_order is not None and total:
            taken[document_order][material_id] = Decimal(total)

    result: dict[uuid.UUID, Usage] = {}
    for product_id, orders in chosen.items():
        per_order: dict[uuid.UUID, list[Decimal]] = defaultdict(list)
        for order_id, quantity in orders:
            for material_id, total in taken.get(order_id, {}).items():
                if total > 0:
                    per_order[material_id].append(total / quantity)
        # 至少一半的订单都领过的材料。
        per_unit = {
            material_id: Decimal(statistics.median(values))
            for material_id, values in per_order.items()
            if len(values) * 2 >= len(orders)
        }
        if per_unit:
            result[product_id] = Usage(per_unit, len(orders))
    return result
