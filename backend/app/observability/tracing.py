"""OpenTelemetry 链路追踪（设计文档 §19.3）：渠道回调 → 事件流 → 实时消费 → 大模型 → 投递。

- 配置了 EDP_OTEL_ENDPOINT 时启用，经 OTLP/HTTP 上报，按 EDP_OTEL_SAMPLE_RATIO 采样（有上游时跟随
  上游的决定）。没有父 span 的客户端 span（空转轮询里的数据库查询、HTTP 调用）不上报。
- 每个 span 带 edp.tenant_id（当前租户，见 context.py）。
- 事件发布时把链路上下文写进事件，消费时接着这条链路；AI 待回复的会话在 Redis 里记下触发它的链路，
  回复时接上；发件箱操作记下写入时的链路，执行时作为链接（link）。
- 没有启用时 tracer() 返回空实现，开销可以忽略。
"""

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import Link, SpanKind
from opentelemetry.util.types import Attributes
from sqlalchemy import Engine

from app.core.config import Settings
from app.observability.context import current_tenant

logger = logging.getLogger(__name__)

TENANT_ATTRIBUTE = "edp.tenant_id"

_provider: TracerProvider | None = None
_engines_instrumented = False


class TenantSpanProcessor(SpanProcessor):
    """span 开始时记下当前租户。"""

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        tenant = current_tenant()
        if tenant is not None:
            span.set_attribute(TENANT_ATTRIBUTE, str(tenant))

    def on_end(self, span: ReadableSpan) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class DropOrphanClients(Sampler):
    """链路起点的采样：没有父 span 的客户端 span 不要，其余按比例。"""

    def __init__(self, ratio: float) -> None:
        self._ratio = TraceIdRatioBased(ratio)

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: Any = None,
    ) -> SamplingResult:
        if kind == SpanKind.CLIENT:
            return SamplingResult(Decision.DROP)
        return self._ratio.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return f"DropOrphanClients({self._ratio.get_description()})"


def new_provider(settings: Settings, component: str) -> TracerProvider:
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": f"edp-{component}",
                "service.namespace": "edp",
                "deployment.environment": settings.env,
            }
        ),
        sampler=ParentBased(root=DropOrphanClients(settings.otel_sample_ratio)),
    )
    provider.add_span_processor(TenantSpanProcessor())
    return provider


def setup(settings: Settings, component: str) -> None:
    """进程启动时调用一次（component：api、worker、scheduler）。没有配置上报地址时什么也不做。"""
    global _provider
    if _provider is not None or not settings.otel_endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    provider = new_provider(settings, component)
    endpoint = settings.otel_endpoint.rstrip("/") + "/v1/traces"
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    _provider = provider
    logger.info("tracing enabled, exporting to %s", endpoint)


def instrument_engines(engines: Sequence[Engine]) -> None:
    """数据库查询的 span（启用追踪时，每个进程第一次创建连接池时调用）。"""
    global _engines_instrumented
    if _provider is None or _engines_instrumented:
        return
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    # 插件声明只支持 SQLAlchemy < 2.1，但用到的游标事件在 2.1 里没有变化。
    SQLAlchemyInstrumentor().instrument(
        engines=list(engines), tracer_provider=_provider, skip_dep_check=True
    )
    _engines_instrumented = True


def shutdown() -> None:
    if _provider is not None:
        _provider.shutdown()


def enabled() -> bool:
    return _provider is not None


def tracer() -> trace.Tracer:
    if _provider is not None:
        return _provider.get_tracer("edp")
    return trace.get_tracer("edp")


@contextmanager
def use_provider(provider: TracerProvider | None) -> Iterator[None]:
    """临时换用另一个 provider（测试用内存导出器检查 span）。"""
    global _provider
    previous = _provider
    _provider = provider
    try:
        yield
    finally:
        _provider = previous


def inject() -> dict[str, str]:
    """当前链路上下文（W3C traceparent），没有有效的 span 时为空。"""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract(carrier: Mapping[str, str] | None) -> Context | None:
    if not carrier:
        return None
    return propagate.extract(dict(carrier))


def links_from(carrier: Mapping[str, str] | None) -> list[Link]:
    context = extract(carrier)
    if context is None:
        return []
    span_context = trace.get_current_span(context).get_span_context()
    return [Link(span_context)] if span_context.is_valid else []
