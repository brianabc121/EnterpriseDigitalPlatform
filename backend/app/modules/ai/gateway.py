"""大模型调用的统一入口：调用客户端并记账（llm_calls，设计文档 §11.5）。

记账使用独立的短事务：调用失败、业务事务回滚时也留有记录。
"""

import logging
import uuid
from typing import Any

from app.context import AppContext
from app.integrations.llm import ChatResult, LLMUnavailable
from app.modules.ai.models import LlmCall

logger = logging.getLogger(__name__)


async def _record(ctx: AppContext, tenant_id: uuid.UUID, **values: Any) -> None:
    try:
        async with ctx.db.tenant_session(tenant_id) as session:
            session.add(LlmCall(tenant_id=tenant_id, **values))
            await session.commit()
    except Exception:  # 记账失败不影响业务
        logger.exception("failed to record llm call")


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
) -> ChatResult:
    client = await ctx.llms.chat_client(tenant_id, scene)
    try:
        result = await client.chat(messages, fast=fast, json_mode=json_mode, max_tokens=max_tokens)
    except LLMUnavailable as exc:
        primary = client.primary
        await _record(
            ctx,
            tenant_id,
            scene=scene,
            provider=primary.provider if primary else "none",
            model=(primary.chat_model if primary else "") or "none",
            status="error",
            error=str(exc)[:500],
            session_id=session_id,
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
    )
    return result


async def embed(
    ctx: AppContext, tenant_id: uuid.UUID, texts: list[str], *, scene: str = "embed"
) -> list[list[float]]:
    client = await ctx.llms.embed_client()
    endpoint = client.embedding
    try:
        result = await client.embed(texts)
    except LLMUnavailable as exc:
        await _record(
            ctx,
            tenant_id,
            scene=scene,
            provider="embed",
            model=endpoint.model if endpoint else "none",
            status="error",
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
    )
    return result.vectors
