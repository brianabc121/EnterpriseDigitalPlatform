"""HTTP 请求的服务端 span 与指标（路由模板、状态码、租户）。"""

import time

from opentelemetry import propagate
from opentelemetry.trace import SpanKind, StatusCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import metrics, tracing
from app.observability.context import begin_request, end_request

# 探针请求很多又没有分析价值，不记录。
_SKIP_PATHS = frozenset({"/healthz", "/readyz"})


def _route_of(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    # 没有匹配到路由的请求（404）合并成一个标签，避免任意路径撑大指标。
    return path if isinstance(path, str) else "unmatched"


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return
        method: str = scope["method"]
        status = 500
        started = time.perf_counter()
        request, token = begin_request()
        headers = {
            name.decode("latin-1"): value.decode("latin-1")
            for name, value in scope.get("headers") or ()
        }

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        span_cm = tracing.tracer().start_as_current_span(
            method,
            context=propagate.extract(headers),
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method, "url.path": str(scope.get("path", ""))},
        )
        try:
            with span_cm as span:
                try:
                    await self.app(scope, receive, send_wrapper)
                finally:
                    route = _route_of(scope)
                    span.update_name(f"{method} {route}")
                    span.set_attribute("http.route", route)
                    span.set_attribute("http.response.status_code", status)
                    if request.tenant_id is not None:
                        span.set_attribute(tracing.TENANT_ATTRIBUTE, str(request.tenant_id))
                    if status >= 500:
                        span.set_status(StatusCode.ERROR)
                    elapsed = time.perf_counter() - started
                    metrics.HTTP_REQUESTS.labels(method, route, str(status)).inc()
                    metrics.HTTP_LATENCY.labels(method, route).observe(elapsed)
                    if request.tenant_id is not None:
                        metrics.TENANT_REQUESTS.labels(
                            metrics.tenant_label(request.tenant_id), f"{status // 100}xx"
                        ).inc()
        finally:
            end_request(token)
