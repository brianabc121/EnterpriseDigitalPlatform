"""语义缓存（设计文档 §11.5 成本控制）：同一个问题（向量余弦相似度不低于 SIMILARITY）直接用之前
通过了护栏、依据知识作答的回答，不再调用大模型。

- 只缓存第一轮、不含个人信息的问题（回答与上下文、客户无关），查找时用改写后的独立问题。
- 知识发布、下架、删除、到期，AI 设置或提示词变化时清空，保证回答总是基于最新的知识。
- 每条缓存保留 TTL，到期后不再使用。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Float, cast, delete, literal, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.types import vector_literal
from app.modules.ai.models import AiAnswerCache

SIMILARITY = 0.95
TTL = timedelta(days=7)


class CachedAnswer:
    def __init__(self, row: AiAnswerCache, similarity: float) -> None:
        self.id = row.id
        self.answer = row.answer
        self.confidence = row.confidence
        self.knowledge: list[dict[str, Any]] = list(row.knowledge or [])
        self.similarity = similarity


async def lookup(
    ctx: AppContext, tenant_id: uuid.UUID, vector: list[float], *, now: datetime | None = None
) -> CachedAnswer | None:
    now = now or datetime.now(UTC)
    async with ctx.db.tenant_session(tenant_id) as session:
        v = cast(literal(vector_literal(vector)), AiAnswerCache.embedding.type)
        distance = AiAnswerCache.embedding.op("<=>", return_type=Float)(v)
        row = (
            await session.execute(
                select(AiAnswerCache, distance.label("d"))
                .where(AiAnswerCache.tenant_id == tenant_id, AiAnswerCache.expires_at > now)
                .order_by(distance)
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        cached, d = row
        similarity = 1.0 - float(d)
        if similarity < SIMILARITY:
            return None
        await session.execute(
            update(AiAnswerCache)
            .where(AiAnswerCache.id == cached.id)
            .values(hits=AiAnswerCache.hits + 1)
        )
        await session.commit()
        return CachedAnswer(cached, similarity)


async def store(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    *,
    question: str,
    vector: list[float],
    answer: str,
    confidence: float,
    knowledge: list[dict[str, Any]],
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    async with ctx.db.tenant_session(tenant_id) as session:
        session.add(
            AiAnswerCache(
                tenant_id=tenant_id,
                question=question[:1000],
                embedding=vector,
                answer=answer,
                confidence=confidence,
                knowledge=knowledge,
                expires_at=now + TTL,
            )
        )
        await session.commit()


async def clear(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """清空租户的缓存（在调用方的事务里执行）：知识或 AI 设置变化时调用。"""
    await session.execute(delete(AiAnswerCache).where(AiAnswerCache.tenant_id == tenant_id))


async def clear_all(ctx: AppContext) -> None:
    """清空所有租户的缓存：平台启用新的提示词版本时调用。"""
    async with ctx.db.platform_sessionmaker() as session:
        await session.execute(text("DELETE FROM ai_answer_cache"))
        await session.commit()


async def purge_expired(ctx: AppContext, *, now: datetime | None = None) -> int:
    """删除过期的缓存（调度任务）。"""
    now = now or datetime.now(UTC)
    async with ctx.db.platform_sessionmaker() as session:
        result = await session.execute(delete(AiAnswerCache).where(AiAnswerCache.expires_at <= now))
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)
