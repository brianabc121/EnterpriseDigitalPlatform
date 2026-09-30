"""知识检索（设计文档 §12.2）：语义检索与关键词检索各取候选，按 RRF 融合，每个条目取最相关的一段；
配置了重排序模型时再对融合后的候选重排，相关度改用重排序得分。

- 语义：问题向量与切片向量的余弦相似度（pgvector HNSW）。没有配置向量模型时跳过。
- 关键词：中文二元组词项的重合（GIN 索引），相关度为问题词项被覆盖的比例。
- 过滤全部在 SQL 里完成：租户、已发布、可见范围、有效期、知识空间（渠道限定了空间时）。
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, cast, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Float, Text

from app.context import AppContext
from app.db.types import vector_literal
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway
from app.modules.kb.models import ItemKind, ItemStatus, KbChunk, KbItem
from app.modules.kb.text import terms

logger = logging.getLogger(__name__)

CANDIDATES = 50
RRF_K = 60
# 相关度低于这个值的结果不返回：向量检索总能找到"最近"的切片，即使毫不相关。
MIN_SCORE = 0.3
# 重排的候选条目数，以及重排序得分低于这个值的结果不返回。
RERANK_CANDIDATES = 15
RERANK_MIN_SCORE = 0.1


@dataclass(frozen=True)
class Hit:
    item_id: uuid.UUID
    kind: str
    title: str
    text: str
    dense: float | None
    lexical: float
    fused: float
    rerank: float | None = None

    @property
    def score(self) -> float:
        if self.rerank is not None:
            return self.rerank
        return max(self.dense or 0.0, self.lexical)


def _visible(
    visibilities: tuple[str, ...], now: datetime, space_ids: list[uuid.UUID] | None = None
) -> ColumnElement[bool]:
    conditions = [
        KbItem.status == ItemStatus.PUBLISHED,
        KbItem.visibility.in_(visibilities),
        or_(KbItem.valid_from.is_(None), KbItem.valid_from <= now),
        or_(KbItem.valid_to.is_(None), KbItem.valid_to > now),
    ]
    if space_ids:
        conditions.append(KbItem.space_id.in_(space_ids))
    return and_(*conditions)


def _joined(columns: list[Any]) -> Select[Any]:
    return select(*columns).join(
        KbItem, and_(KbItem.tenant_id == KbChunk.tenant_id, KbItem.id == KbChunk.item_id)
    )


async def search(
    ctx: AppContext,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
    *,
    visibilities: tuple[str, ...],
    limit: int = 5,
    min_score: float = MIN_SCORE,
    now: datetime | None = None,
    space_ids: list[uuid.UUID] | None = None,
    vector: list[float] | None = None,
    rerank: bool = True,
) -> list[Hit]:
    """vector 为问题的向量（调用方已经算好时传入，省一次调用）；rerank=False 时不重排。"""
    now = now or datetime.now(UTC)
    visible = _visible(visibilities, now, space_ids)
    chunks: dict[uuid.UUID, tuple[uuid.UUID, str, str]] = {}
    dense: dict[uuid.UUID, float] = {}
    lexical: dict[uuid.UUID, float] = {}

    query_terms = terms(query)
    if query_terms:
        q = literal(query_terms, ARRAY(Text))
        # 这个切片的词项里有几个出现在问题中（按行关联的子查询）。
        term = func.unnest(KbChunk.terms).table_valued("term").render_derived()
        overlap = (
            select(func.count())
            .select_from(term)
            .where(term.c.term == func.any(q))
            .correlate(KbChunk)
            .scalar_subquery()
        )

        rows = await session.execute(
            _joined(
                [KbChunk.id, KbChunk.item_id, KbChunk.kind, KbChunk.text, overlap.label("overlap")]
            )
            .where(KbChunk.tenant_id == tenant_id, KbChunk.terms.overlap(q), visible)
            .order_by(text("overlap DESC"))
            .limit(CANDIDATES)
        )
        for chunk_id, item_id, kind, chunk_text, matched in rows:
            chunks[chunk_id] = (item_id, kind, chunk_text)
            lexical[chunk_id] = matched / len(query_terms)

    if vector is None and await ctx.llms.embed_enabled():
        try:
            [vector] = await gateway.embed(ctx, tenant_id, [query], scene="search")
        except LLMUnavailable as exc:
            logger.warning("embedding failed, keyword search only: %s", exc)
    if vector is not None:
        v = cast(literal(vector_literal(vector)), KbChunk.embedding.type)
        distance = KbChunk.embedding.op("<=>", return_type=Float)(v)
        await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        rows = await session.execute(
            _joined([KbChunk.id, KbChunk.item_id, KbChunk.kind, KbChunk.text, distance.label("d")])
            .where(KbChunk.tenant_id == tenant_id, KbChunk.embedding.is_not(None), visible)
            .order_by(distance)
            .limit(CANDIDATES)
        )
        for chunk_id, item_id, kind, chunk_text, d in rows:
            chunks[chunk_id] = (item_id, kind, chunk_text)
            dense[chunk_id] = max(0.0, 1.0 - float(d))

    if not chunks:
        return []

    # RRF 融合：两路排名的倒数和；一个条目取它得分最高的切片。
    fused: dict[uuid.UUID, float] = {}
    for ranking in (dense, lexical):
        for rank, chunk_id in enumerate(sorted(ranking, key=ranking.__getitem__, reverse=True)):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    best: dict[uuid.UUID, uuid.UUID] = {}
    for chunk_id in sorted(fused, key=fused.__getitem__, reverse=True):
        if max(dense.get(chunk_id, 0.0), lexical.get(chunk_id, 0.0)) >= min_score:
            best.setdefault(chunks[chunk_id][0], chunk_id)
    reranked: dict[uuid.UUID, float] = {}
    if rerank and len(best) > 1 and await ctx.llms.rerank_enabled():
        candidates = list(best)[:RERANK_CANDIDATES]
        try:
            scores = await gateway.rerank(
                ctx, tenant_id, query, [chunks[best[i]][2] for i in candidates]
            )
        except LLMUnavailable as exc:
            logger.warning("rerank failed, using fused order: %s", exc)
        else:
            reranked = {
                item_id: score
                for item_id, score in zip(candidates, scores, strict=True)
                if score >= RERANK_MIN_SCORE
            }
            best = {i: best[i] for i in sorted(reranked, key=reranked.__getitem__, reverse=True)}
    item_ids = list(best)[:limit]
    items = {
        item.id: item
        for item in (await session.scalars(select(KbItem).where(KbItem.id.in_(item_ids)))).all()
    }
    hits: list[Hit] = []
    for item_id in item_ids:
        item = items.get(item_id)
        if item is None:
            continue
        chunk_id = best[item_id]
        # 同一条目各切片中最好的语义、关键词得分。
        same = [c for c, (i, _, _) in chunks.items() if i == item_id]
        best_dense = max((dense[c] for c in same if c in dense), default=None)
        best_lexical = max((lexical.get(c, 0.0) for c in same), default=0.0)
        passage = chunks[chunk_id][2]
        hits.append(
            Hit(
                item_id=item_id,
                kind=item.kind,
                title=item.title,
                text=item.content if item.kind == ItemKind.FAQ else passage,
                dense=best_dense,
                lexical=round(best_lexical, 4),
                fused=fused[chunk_id],
                rerank=round(reranked[item_id], 4) if item_id in reranked else None,
            )
        )
    return hits
