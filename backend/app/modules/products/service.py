"""商品库（设计文档 §25.2）：新建与修改、检索、对 AI 与客户可见的字段、商品缺口、向量刷新。

检索：代码、型号精确匹配得满分；关键词为名称、别名、规格、分类、型号、代码的词项重合（GIN）；
有向量模型时再做语义检索，取两路的较高分。只返回达到最低相关度的候选。
"""

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, cast, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.types import Float, Text

from app.context import AppContext
from app.core.ids import new_id
from app.db.types import vector_literal
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway
from app.modules.kb.text import normalize, terms
from app.modules.products import suggest
from app.modules.products.models import Product, ProductGap, ProductKind, ProductStatus

logger = logging.getLogger(__name__)

CANDIDATES = 50
MIN_SCORE = 0.3
EMBED_BATCH = 64
# 对 AI 和客户可见的字段（白名单）：成本价、备注永远不在其中。
PUBLIC_FIELDS = ("code", "name", "model", "spec", "category", "image_url", "retail_price")
ALIAS_SPLIT = re.compile(r"[、,，;；|\n]+")


def split_aliases(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    parts = value if isinstance(value, list) else ALIAS_SPLIT.split(value)
    seen: list[str] = []
    for part in parts:
        alias = part.strip()[:64]
        if alias and alias not in seen:
            seen.append(alias)
    return seen[:20]


def normalize_category(value: str) -> str:
    return "/".join(p.strip() for p in value.split("/") if p.strip())[:128]


def product_terms(product: Product) -> list[str]:
    parts = [
        product.name,
        product.model,
        product.spec,
        product.category.replace("/", " "),
        product.code or "",
        *product.aliases,
    ]
    return terms(" ".join(p for p in parts if p))


def embed_text(product: Product) -> str:
    """向量化的文本：对客户可见的描述（不含价格、成本和备注）。"""
    parts = [
        product.name,
        product.model,
        product.spec,
        product.category,
        "、".join(product.aliases),
    ]
    return " ".join(p for p in parts if p)


def refresh(product: Product) -> None:
    """名称等检索字段变化后：重算词项和联想的检索键，清空向量（由调度任务重新生成）。"""
    product.terms = product_terms(product)
    product.search_key = suggest.search_key(product)
    product.embedding = None


def money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def public_fields(product: Product) -> dict[str, Any]:
    """对 AI 和客户可见的商品信息（白名单组装）。"""
    return {
        "product_id": str(product.id),
        "code": product.code,
        "name": product.name,
        "model": product.model,
        "spec": product.spec,
        "category": product.category,
        "image_url": product.image_url,
        "retail_price": money(product.retail_price),
        "on_shelf": product.status == ProductStatus.ON,
    }


def public_columns() -> Any:
    """只加载对客可见的字段（AI 用到的商品数据在查询这一层就不取出成本价和备注）；
    访问其他字段会直接报错，不会悄悄再查一次。"""
    return load_only(
        Product.id,
        Product.tenant_id,
        Product.code,
        Product.name,
        Product.model,
        Product.spec,
        Product.category,
        Product.image_url,
        Product.retail_price,
        Product.aliases,
        Product.status,
        raiseload=True,
    )


def label(product: Product) -> str:
    """一行描述：名称 型号 规格。"""
    return " ".join(p for p in (product.name, product.model, product.spec) if p)


@dataclass(frozen=True)
class Candidate:
    product: Product
    score: float
    exact: bool = False


async def search(
    ctx: AppContext | None,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
    *,
    limit: int = 5,
    on_shelf: bool = True,
    min_score: float = MIN_SCORE,
    public_only: bool = False,
    vector: list[float] | None = None,
    kind: ProductKind = ProductKind.GOODS,
) -> list[Candidate]:
    """public_only：只加载对客可见的字段（给 AI 用）。vector：调用方已经算好的问题向量。
    kind：默认只找成品（材料不给 AI，也不能下单；开领料单时找材料）。"""
    query = query.strip()[:200]
    if not query:
        return []
    base: list[ColumnElement[bool]] = [Product.tenant_id == tenant_id, Product.kind == kind]
    if on_shelf:
        base.append(Product.status == ProductStatus.ON)
    options = [public_columns()] if public_only else []
    scores: dict[uuid.UUID, float] = {}
    products: dict[uuid.UUID, Product] = {}
    exact_ids: set[uuid.UUID] = set()

    lowered = query.lower()
    for row in await session.scalars(
        select(Product)
        .options(*options)
        .where(
            *base,
            or_(func.lower(Product.code) == lowered, func.lower(Product.model) == lowered),
        )
        .limit(limit)
    ):
        products[row.id], scores[row.id] = row, 1.0
        exact_ids.add(row.id)

    query_terms = terms(query)
    if query_terms:
        q = literal(query_terms, ARRAY(Text))
        term = func.unnest(Product.terms).table_valued("term").render_derived()
        overlap = (
            select(func.count())
            .select_from(term)
            .where(term.c.term == func.any(q))
            .correlate(Product)
            .scalar_subquery()
        )
        rows = await session.execute(
            select(Product, overlap.label("overlap"))
            .options(*options)
            .where(*base, Product.terms.overlap(q))
            .order_by(text("overlap DESC"), Product.name)
            .limit(CANDIDATES)
        )
        for product, matched in rows:
            products[product.id] = product
            scores[product.id] = max(scores.get(product.id, 0.0), matched / len(query_terms))

    if vector is None and ctx is not None and await ctx.llms.embed_enabled():
        try:
            [vector] = await gateway.embed(ctx, tenant_id, [query], scene="product_search")
        except LLMUnavailable as exc:
            logger.warning("embedding failed, keyword product search only: %s", exc)
    if vector is not None:
        v = cast(literal(vector_literal(vector)), Product.embedding.type)
        distance = Product.embedding.op("<=>", return_type=Float)(v)
        await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        rows = await session.execute(
            select(Product, distance.label("d"))
            .options(*options)
            .where(*base, Product.embedding.is_not(None))
            .order_by(distance)
            .limit(CANDIDATES)
        )
        for product, d in rows:
            products[product.id] = product
            scores[product.id] = max(scores.get(product.id, 0.0), 1.0 - float(d))

    ranked = sorted(
        (pid for pid in scores if scores[pid] >= min_score),
        key=lambda pid: (-scores[pid], products[pid].name),
    )
    return [
        Candidate(products[pid], round(scores[pid], 4), pid in exact_ids) for pid in ranked[:limit]
    ]


async def record_gap(session: AsyncSession, tenant_id: uuid.UUID, query: str) -> None:
    """客户问到、商品库里匹配不到的商品：按规范化后的说法累计次数（由调用方提交）。"""
    term = normalize(query)[:128]
    if not term:
        return
    now = datetime.now(UTC)
    statement = insert(ProductGap).values(
        id=new_id(), tenant_id=tenant_id, term=term, sample=query.strip()[:500]
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ProductGap.tenant_id, ProductGap.term],
            set_={
                "count": ProductGap.count + 1,
                "sample": statement.excluded.sample,
                "last_seen_at": now,
                "resolved_at": None,
            },
        )
    )


async def embed_pending(ctx: AppContext, *, limit: int = 500) -> int:
    """调度任务：为还没有向量的上架成品生成向量（没有配置向量模型时跳过；材料不做语义检索）。"""
    if not await ctx.llms.embed_enabled():
        return 0
    async with ctx.db.platform_sessionmaker() as session:
        pending = (
            await session.execute(
                select(Product.tenant_id, Product.id)
                .where(
                    Product.embedding.is_(None),
                    Product.status == ProductStatus.ON,
                    Product.kind == ProductKind.GOODS,
                )
                .order_by(Product.updated_at)
                .limit(limit)
            )
        ).all()
    done = 0
    by_tenant: dict[uuid.UUID, list[uuid.UUID]] = {}
    for tenant_id, product_id in pending:
        by_tenant.setdefault(tenant_id, []).append(product_id)
    for tenant_id, ids in by_tenant.items():
        for start in range(0, len(ids), EMBED_BATCH):
            batch = ids[start : start + EMBED_BATCH]
            async with ctx.db.tenant_session(tenant_id) as session:
                rows = (await session.scalars(select(Product).where(Product.id.in_(batch)))).all()
                if not rows:
                    continue
                try:
                    vectors = await gateway.embed(
                        ctx, tenant_id, [embed_text(p) for p in rows], scene="product_embed"
                    )
                except LLMUnavailable as exc:
                    logger.warning("product embedding failed for tenant %s: %s", tenant_id, exc)
                    break
                for product, vector in zip(rows, vectors, strict=True):
                    # 只更新向量，不改修改时间（updated_at 反映员工的修改）。
                    await session.execute(
                        update(Product)
                        .where(Product.id == product.id)
                        .values(embedding=vector, updated_at=Product.updated_at)
                    )
                await session.commit()
                done += len(rows)
    return done


async def find_existing(
    session: AsyncSession, *, code: str | None, name: str, model: str, spec: str
) -> Product | None:
    """导入时的匹配：有代码的按代码；没有代码的按"名称 + 型号 + 规格"。"""
    if code:
        return await session.scalar(select(Product).where(Product.code == code))
    return await session.scalar(
        select(Product)
        .where(
            and_(
                func.lower(Product.name) == name.lower(),
                func.lower(Product.model) == model.lower(),
                func.lower(Product.spec) == spec.lower(),
            )
        )
        .order_by(Product.created_at)
        .limit(1)
    )
