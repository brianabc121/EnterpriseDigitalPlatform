"""开单时用上表单知识（设计文档 §25.18）：联想里学到的叫法和常一起开的商品、AI 下单时的商品匹配、
一键领料的用量。联想和 AI 只用生效的知识；领料的用量由调用方按状态决定（停用的不列，固定的
优先）。"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass

from sqlalchemy import literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.formkb.models import Form, FormKbEntry, Kind, Status
from app.modules.products import suggest
from app.modules.products.models import Product, ProductKind, ProductStatus

# 输入和叫法一致；输入是叫法的开头；客户的说法里包含叫法（AI 下单）。
EXACT = 1.0
PREFIX = 0.9
CONTAINS = 0.85


@dataclass(frozen=True)
class AliasHit:
    product_id: uuid.UUID
    score: float
    exact: bool


async def aliases(
    session: AsyncSession, query: str, *, contains: bool = False
) -> dict[uuid.UUID, AliasHit]:
    """生效的叫法：输入（统一写法后）和叫法一致或是叫法的开头；contains 时说法里包含叫法也算。"""
    key = suggest.compact(query)
    if not key:
        return {}
    conditions = [FormKbEntry.text == key]
    if len(key) >= 2:
        conditions.append(FormKbEntry.text.startswith(key, autoescape=True))
    if contains:
        conditions.append(literal(key).contains(FormKbEntry.text))
    rows = await session.scalars(
        select(FormKbEntry).where(
            FormKbEntry.kind == Kind.ALIAS,
            FormKbEntry.status == Status.ACTIVE,
            or_(*conditions),
        )
    )
    found: dict[uuid.UUID, AliasHit] = {}
    for entry in rows:
        if entry.text == key:
            hit = AliasHit(entry.product_id, EXACT, True)
        elif entry.text.startswith(key):
            hit = AliasHit(entry.product_id, PREFIX, False)
        else:
            hit = AliasHit(entry.product_id, CONTAINS, False)
        known = found.get(entry.product_id)
        if known is None or hit.score > known.score:
            found[entry.product_id] = hit
    return found


@dataclass(frozen=True)
class Companion:
    product: Product
    anchor: Product
    together: int
    forms: int


async def companions(
    session: AsyncSession,
    form: Form,
    product_ids: Collection[uuid.UUID],
    *,
    kind: ProductKind,
    limit: int,
) -> list[Companion]:
    """单上已经有这些商品时，常和它们一起开的（生效的搭配，单上还没有的，按比例从高到低）。"""
    if not product_ids:
        return []
    rows = (
        await session.execute(
            select(FormKbEntry, Product)
            .join(Product, Product.id == FormKbEntry.related_id)
            .where(
                FormKbEntry.kind == Kind.COMPANION,
                FormKbEntry.form == form,
                FormKbEntry.status == Status.ACTIVE,
                FormKbEntry.product_id.in_(product_ids),
                FormKbEntry.related_id.not_in(product_ids),
                Product.kind == kind,
                Product.status == ProductStatus.ON,
            )
            .order_by(FormKbEntry.share.desc().nulls_last(), Product.name)
        )
    ).all()
    anchors = {
        p.id: p
        for p in await session.scalars(
            select(Product).where(Product.id.in_({e.product_id for e, _ in rows}))
        )
    }
    found: dict[uuid.UUID, Companion] = {}
    for entry, product in rows:
        if product.id in found or entry.product_id not in anchors:
            continue
        forms = entry.stats.get("forms")
        together = entry.stats.get("together")
        found[product.id] = Companion(
            product,
            anchors[entry.product_id],
            together if isinstance(together, int) else entry.evidence,
            forms if isinstance(forms, int) else 0,
        )
        if len(found) >= limit:
            break
    return list(found.values())


async def usage_entries(
    session: AsyncSession, product_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, dict[uuid.UUID, FormKbEntry]]:
    """这些成品的用量知识（全部状态）：成品 → 材料 → 知识。"""
    if not product_ids:
        return {}
    found: dict[uuid.UUID, dict[uuid.UUID, FormKbEntry]] = {}
    for entry in await session.scalars(
        select(FormKbEntry).where(
            FormKbEntry.kind == Kind.USAGE, FormKbEntry.product_id.in_(product_ids)
        )
    ):
        if entry.related_id is not None:
            found.setdefault(entry.product_id, {})[entry.related_id] = entry
    return found


def form_for(source: str, kind: ProductKind) -> Form:
    """联想的来源对应的表单：下单是订单；开单时材料是领料单，成品是入库单。"""
    if source == "orders":
        return Form.ORDER
    return Form.REQUISITION if kind == ProductKind.MATERIAL else Form.RECEIPT
