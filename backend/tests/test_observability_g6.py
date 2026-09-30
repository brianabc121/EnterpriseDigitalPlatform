"""G6 可观测性：HTTP 与业务指标、状态类指标、结构化日志，以及贯穿
回调 → 事件 → AI → 大模型 → 投递的链路追踪。
"""

import json
import logging
import re
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from prometheus_client import REGISTRY

from app.core.config import Settings
from app.observability import metrics, state, tracing
from app.observability.context import bind_tenant
from app.observability.logs import JsonFormatter
from app.scheduler import Job, run_once
from tests.desk import Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_ai_reception import ANSWER, bot_texts, enable_ai


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


@pytest.fixture
def spans(settings: Settings) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = tracing.new_provider(settings, "test")
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with tracing.use_provider(provider):
        yield exporter
    provider.shutdown()


def hex_trace(span: ReadableSpan) -> str:
    assert span.context is not None
    return format(span.context.trace_id, "032x")


async def test_http_requests_are_counted_by_route_template_and_tenant(desk: Desk) -> None:
    created = await desk.client.post(
        "/api/v1/kb/items",
        headers=desk.admin,
        json={"title": "退货地址", "content": "上海市", "publish": True},
    )
    assert created.status_code == 201, created.text
    route = "/api/v1/kb/items/{item_id}"
    before = sample("edp_http_requests_total", method="GET", route=route, status="200")
    by_tenant = sample(
        "edp_tenant_http_requests_total", tenant=str(desk.tenant_id), status_class="2xx"
    )

    response = await desk.client.get(f"/api/v1/kb/items/{created.json()['id']}", headers=desk.admin)
    assert response.status_code == 200, response.text

    assert sample("edp_http_requests_total", method="GET", route=route, status="200") == before + 1
    assert (
        sample("edp_tenant_http_requests_total", tenant=str(desk.tenant_id), status_class="2xx")
        == by_tenant + 1
    )
    # 没有匹配到路由的请求合并成一个标签，不把任意路径写进指标。
    unmatched = sample("edp_http_requests_total", method="GET", route="unmatched", status="404")
    await desk.client.get(f"/no/such/{uuid.uuid4()}")
    assert (
        sample("edp_http_requests_total", method="GET", route="unmatched", status="404")
        == unmatched + 1
    )


async def test_request_span_continues_the_callers_trace(
    desk: Desk, spans: InMemorySpanExporter
) -> None:
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    headers = {**desk.admin, "traceparent": f"00-{trace_id}-b7ad6b7169203331-01"}

    response = await desk.client.get("/api/v1/customers", headers=headers)
    assert response.status_code == 200, response.text

    [server] = [s for s in spans.get_finished_spans() if s.kind == SpanKind.SERVER]
    assert server.name == "GET /api/v1/customers"
    assert hex_trace(server) == trace_id
    assert server.attributes is not None
    assert server.attributes["edp.tenant_id"] == str(desk.tenant_id)
    assert server.attributes["http.response.status_code"] == 200
    # 请求里的数据库查询等子 span 也带租户。
    children = [s for s in spans.get_finished_spans() if s.kind != SpanKind.SERVER]
    assert all(hex_trace(s) == trace_id for s in children)


async def test_trace_runs_from_callback_through_ai_reply_to_delivery(
    desk: Desk, spans: InMemorySpanExporter
) -> None:
    await enable_ai(desk)
    visitor = await desk.visitor()
    spans.clear()
    llm_before = sample(
        "edp_llm_calls_total",
        tenant=str(desk.tenant_id),
        scene="reply",
        provider="fake",
        model="fake-chat",
        status="ok",
    )
    events_before = sample(
        "edp_events_processed_total",
        type="message.received",
        tenant=str(desk.tenant_id),
        outcome="ok",
    )

    await desk.say(visitor, "快递几天能到")

    assert bot_texts(desk, visitor) == [ANSWER]
    finished = spans.get_finished_spans()
    [reply] = [s for s in finished if s.name == "ai reply"]
    trace_id = hex_trace(reply)
    in_trace = {s.name for s in finished if hex_trace(s) == trace_id}
    # 回调 → 发布事件 → 实时消费 → AI 回复 → 大模型 → 发件箱投递，都在同一条链路里。
    assert {
        "POST /hooks/openim/{secret}/{command}",
        "publish message.received",
        "event message.received",
        "ai reply",
        "llm reply",
        "im_op bot_message",
    } <= in_trace
    [consumer] = [
        s for s in finished if s.name == "event message.received" and hex_trace(s) == trace_id
    ]
    assert consumer.kind == SpanKind.CONSUMER
    assert consumer.attributes is not None
    assert consumer.attributes["edp.tenant_id"] == str(desk.tenant_id)
    [llm] = [s for s in finished if s.name == "llm reply" and hex_trace(s) == trace_id]
    assert llm.attributes is not None
    assert llm.attributes["gen_ai.request.model"] == "fake-chat"
    assert int(str(llm.attributes["gen_ai.usage.input_tokens"])) > 0

    assert (
        sample(
            "edp_llm_calls_total",
            tenant=str(desk.tenant_id),
            scene="reply",
            provider="fake",
            model="fake-chat",
            status="ok",
        )
        == llm_before + 1
    )
    assert (
        sample(
            "edp_events_processed_total",
            type="message.received",
            tenant=str(desk.tenant_id),
            outcome="ok",
        )
        > events_before
    )


async def test_state_metrics_describe_queue_agents_and_backlog(desk: Desk) -> None:
    visitor = await desk.visitor()
    await desk.say(visitor, "你好，在吗")  # 没有坐席在线：排队

    families = {f.name: f for f in await state.collect(desk.ctx)}
    tenant = str(desk.tenant_id)

    def value(name: str, **labels: str) -> float | None:
        for s in families[name].samples:
            if all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
        return None

    assert value("edp_tenant_info", tenant=tenant, code=desk.code) == 1
    assert value("edp_queue_length", tenant=tenant) == 1
    oldest = value("edp_queue_oldest_wait_seconds", tenant=tenant)
    assert oldest is not None and oldest >= 0
    assert value("edp_agents_online", tenant=tenant) is None
    assert value("edp_dead_letter_size") == 0

    wait = sample("edp_queue_wait_seconds_count", tenant=tenant)
    alice = await desk.agent("alice")  # 上线后分配排队的会话
    families = {f.name: f for f in await state.collect(desk.ctx)}
    assert value("edp_queue_length", tenant=tenant) is None
    assert value("edp_agents_online", tenant=tenant) == 1
    assert value("edp_agent_capacity", tenant=tenant) == 5
    assert value("edp_human_sessions", tenant=tenant) == 1
    assert sample("edp_queue_wait_seconds_count", tenant=tenant) == wait + 1
    assert alice.staff_id

    # 调度任务把快照导出到 /metrics；交出租约后清空。
    await state.refresh(desk.ctx)
    assert sample("edp_agents_online", tenant=tenant) == 1
    state.clear()
    assert sample("edp_agents_online", tenant=tenant) == 0


async def test_scheduler_jobs_record_runs_and_last_success(desk: Desk) -> None:
    async def ok(_: object) -> int:
        return 1

    async def broken(_: object) -> int:
        raise RuntimeError("boom")

    assert await run_once(desk.ctx, Job("test-ok", 5, ok)) == 1
    assert sample("edp_job_runs_total", task="test-ok", outcome="ok") >= 1
    assert sample("edp_job_last_success_timestamp_seconds", task="test-ok") > 0
    with pytest.raises(RuntimeError):
        await run_once(desk.ctx, Job("test-broken", 5, broken))
    assert sample("edp_job_runs_total", task="test-broken", outcome="error") >= 1
    assert sample("edp_job_last_success_timestamp_seconds", task="test-broken") == 0


def test_json_logs_carry_tenant_and_trace(settings: Settings) -> None:
    exporter = InMemorySpanExporter()
    provider = tracing.new_provider(settings, "test")
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tenant = uuid.uuid4()
    formatter = JsonFormatter()
    with (
        tracing.use_provider(provider),
        bind_tenant(tenant),
        tracing.tracer().start_as_current_span("work") as span,
    ):
        record = logging.LogRecord(
            "app.test", logging.INFO, __file__, 1, "处理 %s", ("消息",), None
        )
        entry = json.loads(formatter.format(record))
    assert entry["message"] == "处理 消息"
    assert entry["tenant_id"] == str(tenant)
    assert entry["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert entry["level"] == "info"


def test_metrics_endpoint_serves_prometheus_text() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    metrics.start_metrics_server(port)
    metrics.HTTP_REQUESTS.labels("GET", "/probe", "200").inc()
    with httpx.Client(trust_env=False) as http:
        body = http.get(f"http://127.0.0.1:{port}/metrics").text
    assert 'edp_http_requests_total{method="GET",route="/probe",status="200"}' in body
    assert "edp_http_requests_created" not in body


REPO = Path(__file__).resolve().parents[2]
_METRIC = re.compile(r"\bedp_[a-z_]+")
_SUFFIXES = ("_bucket", "_count", "_sum", "_total")


def _base(name: str) -> str:
    for suffix in _SUFFIXES:
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name


async def test_alerts_and_dashboard_only_use_exported_metrics(desk: Desk) -> None:
    exported = {_base(m.name) for m in REGISTRY.collect()}
    exported |= {m.name for m in await state.collect(desk.ctx)}

    rules = yaml.safe_load((REPO / "deploy/observability/prometheus/alerts.yml").read_text())
    exprs = [rule["expr"] for group in rules["groups"] for rule in group["rules"]]
    dashboard = json.loads(
        (REPO / "deploy/observability/grafana/edp-overview.json").read_text(encoding="utf-8")
    )
    exprs += [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
    exprs.append(dashboard["templating"]["list"][1]["definition"])

    used = {_base(name) for expr in exprs for name in _METRIC.findall(expr)}
    assert used, "no metrics found"
    assert used <= exported, sorted(used - exported)
    # 设计文档 §19.3 要求的告警都在。
    names = {rule["alert"] for group in rules["groups"] for rule in group["rules"]}
    assert {
        "EdpQueueWaitTooLong",
        "EdpLlmErrorRateHigh",
        "EdpEventBacklog",
        "EdpReconcileRecoveredHigh",
        "EdpWecomAuthCancelled",
        "EdpTenantMessageSpike",
    } <= names


def test_k8s_prometheus_rule_matches_the_alert_rules() -> None:
    """deploy/k8s 的 PrometheusRule 与告警规则文件保持一致。"""
    rules = yaml.safe_load((REPO / "deploy/observability/prometheus/alerts.yml").read_text())
    crd = yaml.safe_load(
        (REPO / "deploy/k8s/components/monitoring/prometheus-rule.yaml").read_text()
    )
    assert crd["kind"] == "PrometheusRule"
    assert crd["spec"]["groups"] == rules["groups"]
