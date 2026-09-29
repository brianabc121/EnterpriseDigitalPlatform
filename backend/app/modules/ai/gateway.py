"""大模型调用的统一入口：并发限制、调用客户端并记账（llm_calls，设计文档 §11.5）。

记账使用独立的短事务：调用失败、业务事务回滚时也留有记录。费用按供应商登记的价格估算（分），
同时记下使用的提示词版本。
"""

import logging
import uuid
from typing import Any

from app.context import AppContext
from app.integrations.llm import ChatResult, LLMUnavailable
from app.modules.ai import limiter
from app.modules.ai.models import LlmCall

logger = logging.getLogger(__name__)


async def _record(ctx: AppContext, tenant_id: uuid.UUID, **values: Any) -> None:
    try:
        async with ctx.db.tenant_session(tenant_id) as session:
            session.add(LlmCall(tenant_id=tenant_id, **values))
            await session.commit()
    except Exception:  # 记账失败不影响业务
        logger.exception("failed to record llm call")


def _status(exc: LLMUnavailable) -> str:
    return "busy" if isinstance(exc, limiter.LlmBusy) else "error"


async def chat(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    messages: list[dict[str, Any]],
    *,
    scene: str,
    session_id: uuid.UUID | None = None,
    fast: bool = False,
    json_mode: bool = False,
    max_tokens: int = 800,
    tools: list[dict[str, Any]] | None = None,
    prompt_version: str | None = None,
) -> ChatResult:
    client = await ctx.llms.chat_client(tenant_id, scene)
    try:
        async with limiter.slot(ctx, tenant_id):
            result = await client.chat(
                messages, fast=fast, json_mode=json_mode, max_tokens=max_tokens, tools=tools
            )
    except LLMUnavailable as exc:
        primary = client.primary
        await _record(
            ctx,
            tenant_id,
            scene=scene,
            provider=primary.provider if primary else "none",
            model=(primary.chat_model if primary else "") or "none",
            status=_status(exc),
            error=str(exc)[:500],
            session_id=session_id,
            prompt_version=prompt_version,
        )
        raise
    await _record(
        ctx,
        tenant_id,
        scene=scene,
        provider=result.provider,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        latency_ms=result.latency_ms,
        status="ok",
        session_id=session_id,
        cost=round(result.cost, 4),
        prompt_version=prompt_version,
    )
    return result


async def embed(
    ctx: AppContext, tenant_id: uuid.UUID, texts: list[str], *, scene: str = "embed"
) -> list[list[float]]:
    client = await ctx.llms.embed_client()
    endpoint = client.embedding
    try:
        async with limiter.slot(ctx, tenant_id):
            result = await client.embed(texts)
    except LLMUnavailable as exc:
        await _record(
            ctx,
            tenant_id,
            scene=scene,
            provider="embed",
            model=endpoint.model if endpoint else "none",
            status=_status(exc),
            error=str(exc)[:500],
        )
        raise
    await _record(
        ctx,
        tenant_id,
        scene=scene,
        provider=result.provider,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        latency_ms=result.latency_ms,
        status="ok",
        cost=round(result.cost, 4),
    )
    return result.vectors


async def rerank(
    ctx: AppContext, tenant_id: uuid.UUID, query: str, documents: list[str]
) -> list[float]:
    """重排序得分（0 到 1，与 documents 一一对应）。"""
    client = await ctx.llms.rerank_client()
    endpoint = client.reranker
    try:
        async with limiter.slot(ctx, tenant_id):
            result = await client.rerank(query, documents)
    except LLMUnavailable as exc:
        await _record(
            ctx,
            tenant_id,
            scene="rerank",
            provider="rerank",
            model=endpoint.model if endpoint else "none",
            status=_status(exc),
            error=str(exc)[:500],
        )
        raise
    await _record(
        ctx,
        tenant_id,
        scene="rerank",
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency_ms,
        status="ok",
    )
    return result.scores
