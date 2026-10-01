"""开单时的商品联想（设计文档 §25.16）：数据库里粗筛、打分排序，常用程度，自己最近用过的商品。

下单（source=orders）看订单明细，开领料单和入库单（source=documents）看单据明细。
"""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.iam.principal import Principal
from app.modules.orders.models import Order, OrderItem
from app.modules.products import suggest
from app.modules.products.models import Product, ProductKind, ProductStatus
from app.modules.warehouse.models import StockDocument, StockDocumentLine

Source = Literal["orders", "documents"]
# 常用程度和"最近用过"都只看最近 90 天。
USAGE_DAYS = 90


@dataclass(frozen=True)
class Suggestion:
    product: Product
    score: float
    field: suggest.SuggestField | None
    match: suggest.SuggestMatch | Literal["recent"]


def _since() -> datetime:
    return datetime.now(UTC) - timedelta(days=USAGE_DAYS)


def _boost(product: Product, uses: int) -> float:
    """分数接近时：常用的（最多加 0.04）、有库存的（加 0.01）靠前。"""
    usage = 0.04 * min(1.0, math.log10(1 + uses) / 2)
    return usage + (0.01 if product.stock is not None and product.stock > 0 else 0.0)


async def usage_counts(
    session: AsyncSession, ids: list[uuid.UUID], source: Source
) -> dict[uuid.UUID, int]:
    """最近 90 天每个商品出现在多少行订单明细（或单据明细）里。"""
    if not ids:
        return {}
    if source == "orders":
        statement = (
            select(OrderItem.product_id, func.count())
            .join(Order, Order.id == OrderItem.order_id)
            .where(OrderItem.product_id.in_(ids), Order.created_at >= _since())
            .group_by(OrderItem.product_id)
        )
    else:
        statement = (
            select(StockDocumentLine.product_id, func.count())
            .join(StockDocument, StockDocument.id == StockDocumentLine.document_id)
            .where(StockDocumentLine.product_id.in_(ids), StockDocument.submitted_at >= _since())
            .group_by(StockDocumentLine.product_id)
        )
    return {pid: int(n) for pid, n in (await session.execute(statement)).all() if pid}


async def recent(
    session: AsyncSession,
    principal: Principal,
    *,
    kind: ProductKind,
    source: Source,
    limit: int,
) -> list[Product]:
    """自己最近开单用过的商品，最近用的在前（还在用的：上架或启用的）。"""
    if source == "orders":
        used = (
            select(OrderItem.product_id.label("pid"), func.max(Order.created_at).label("used_at"))
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.created_by == principal.staff_id,
                OrderItem.product_id.is_not(None),
                Order.created_at >= _since(),
            )
            .group_by(OrderItem.product_id)
            .subquery()
        )
    else:
        used = (
            select(
                StockDocumentLine.product_id.label("pid"),
                func.max(StockDocument.submitted_at).label("used_at"),
            )
            .join(StockDocument, StockDocument.id == StockDocumentLine.document_id)
            .where(
                StockDocument.created_by == principal.staff_id,
                StockDocument.submitted_at >= _since(),
            )
            .group_by(StockDocumentLine.product_id)
            .subquery()
        )
    rows = await session.scalars(
        select(Product)
        .join(used, used.c.pid == Product.id)
        .where(Product.kind == kind, Product.status == ProductStatus.ON)
        .order_by(used.c.used_at.desc(), Product.name)
        .limit(limit)
    )
    return list(rows.all())


async def suggestions(
    session: AsyncSession,
    principal: Principal,
    query: str,
    *,
    kind: ProductKind,
    source: Source,
    limit: int,
) -> tuple[list[Suggestion], bool]:
    """联想的结果，以及是不是"最近用过的"（没有关键词时）。只找上架或启用的商品。"""
    if not query.strip():
        found = await recent(session, principal, kind=kind, source=source, limit=limit)
        return [Suggestion(p, 0.0, None, "recent") for p in found], True
    filtering = suggest.prefilter(query)
    if filtering is None:
        return [], False
    condition, rank = filtering
    candidates = (
        await session.scalars(
            select(Product)
            .where(Product.kind == kind, Product.status == ProductStatus.ON, condition)
            .order_by(rank.desc(), Product.name)
            .limit(suggest.CANDIDATES)
        )
    ).all()
    scores = suggest.score_all(query, candidates)
    floor = suggest.keep([s.score for s in scores])
    scored = [(p, s) for p, s in zip(candidates, scores, strict=True) if s.score >= floor]
    uses = await usage_counts(session, [p.id for p, _ in scored], source)
    scored.sort(key=lambda ps: (-(ps[1].score + _boost(ps[0], uses.get(ps[0].id, 0))), ps[0].name))
    return [Suggestion(p, s.score, s.field, s.match) for p, s in scored[:limit]], False
