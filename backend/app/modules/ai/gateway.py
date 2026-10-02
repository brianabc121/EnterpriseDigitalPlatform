"""大模型调用的统一入口：并发限制、调用客户端并记账（llm_calls，设计文档 §11.5）。

记账使用独立的短事务：调用失败、业务事务回滚时也留有记录。费用按供应商登记的价格估算（分），
同时记下使用的提示词版本和触发调用的员工（企业 token 计费，设计文档 §37）。每次调用同时计入
Prometheus 指标，并在链路追踪里是一个 span。
"""

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry.trace import Span, SpanKind, StatusCode

from app.context import AppContext
from app.integrations.llm import ChatResult, LLMUnavailable
from app.integrations.typesafe import DecideResult, JudgeClient, Question
from app.modules.ai import limiter
from app.modules.ai.models import LlmCall
from app.observability import metrics, tracing
from app.observability.context import current_staff

logger = logging.getLogger(__name__)


def _observe(tenant_id: uuid.UUID, values: dict[str, Any]) -> None:
    tenant = metrics.tenant_label(tenant_id)
    scene, provider, status = values["scene"], values["provider"], values["status"]
    metrics.LLM_CALLS.labels(tenant, scene, provider, values["model"], status).inc()
    if status == "ok":
        metrics.LLM_LATENCY.labels(scene, provider).observe(values.get("latency_ms", 0) / 1000)
    for kind, key in (("input", "prompt_tokens"), ("output", "completion_tokens")):
        if values.get(key):
            metrics.LLM_TOKENS.labels(tenant, scene, kind).inc(values[key])
    if values.get("cost"):
        metrics.LLM_COST.labels(tenant, scene).inc(values["cost"])


@contextmanager
def _span(operation: str, scene: str) -> Iterator[Span]:
    with tracing.tracer().start_as_current_span(
        f"llm {scene}",
        kind=SpanKind.CLIENT,
        attributes={"gen_ai.operation.name": operation, "edp.llm.scene": scene},
    ) as span:
        yield span


def _annotate(span: Span, values: dict[str, Any]) -> None:
    span.set_attribute("gen_ai.system", values["provider"])
    span.set_attribute("gen_ai.request.model", values["model"])
    if values.get("prompt_tokens"):
        span.set_attribute("gen_ai.usage.input_tokens", values["prompt_tokens"])
    if values.get("completion_tokens"):
        span.set_attribute("gen_ai.usage.output_tokens", values["completion_tokens"])
    if values["status"] != "ok":
        span.set_status(StatusCode.ERROR, values.get("error") or values["status"])


async def _record(ctx: AppContext, tenant_id: uuid.UUID, span: Span, **values: Any) -> None:
    _observe(tenant_id, values)
    _annotate(span, values)
    try:
        async with ctx.db.tenant_session(tenant_id) as session:
            session.add(LlmCall(tenant_id=tenant_id, staff_id=current_staff(tenant_id), **values))
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
    with _span("chat", scene) as span:
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
                span,
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
            span,
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


async def decide(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    client: JudgeClient,
    state: str,
    questions: dict[str, Question],
    *,
    scene: str = "intent",
    session_id: uuid.UUID | None = None,
) -> DecideResult:
    """判断模型（设计文档 §32）：一次请求回答多个问题。和大模型一样占并发名额、记账。"""
    endpoint = client.endpoint
    with _span("decide", scene) as span:
        try:
            async with limiter.slot(ctx, tenant_id):
                result = await client.decide(state, questions)
        except LLMUnavailable as exc:
            await _record(
                ctx,
                tenant_id,
                span,
                scene=scene,
                provider=endpoint.provider if endpoint else "none",
                model=(endpoint.model if endpoint else "") or "none",
                status=_status(exc),
                error=str(exc)[:500],
                session_id=session_id,
            )
            raise
        await _record(
            ctx,
            tenant_id,
            span,
            scene=scene,
            provider=result.provider,
            model=result.model,
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            latency_ms=result.latency_ms,
            status="ok",
            session_id=session_id,
            cost=round(result.cost, 4),
        )
    return result


async def embed(
    ctx: AppContext, tenant_id: uuid.UUID, texts: list[str], *, scene: str = "embed"
) -> list[list[float]]:
    client = await ctx.llms.embed_client()
    endpoint = client.embedding
    with _span("embeddings", scene) as span:
        try:
            async with limiter.slot(ctx, tenant_id):
                result = await client.embed(texts)
        except LLMUnavailable as exc:
            await _record(
                ctx,
                tenant_id,
                span,
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
            span,
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
    with _span("rerank", "rerank") as span:
        try:
            async with limiter.slot(ctx, tenant_id):
                result = await client.rerank(query, documents)
        except LLMUnavailable as exc:
            await _record(
                ctx,
                tenant_id,
                span,
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
            span,
            scene="rerank",
            provider=result.provider,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            latency_ms=result.latency_ms,
            status="ok",
            cost=round(result.cost, 4),
        )
    return result.scores
