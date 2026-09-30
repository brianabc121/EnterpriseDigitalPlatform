"""Prometheus 指标（设计文档 §19.3）。

- 每个进程（API、实时消费、调度）在 EDP_METRICS_PORT 上单独暴露 /metrics，不经过对外的 API 端口。
- 计数器和直方图在发生的地方记录。排队、坐席、发件箱、事件积压等状态类指标由持有调度租约的调度进程
  每 15 秒读取一次（state.py），整体替换快照；其他进程不导出这些指标，调度进程交出租约时清空。
- 租户维度统一用标签 tenant（租户 ID）；edp_tenant_info{tenant, code} 给出租户短码，看板按短码筛选。
"""

import logging
import uuid
from collections.abc import Iterable, Sequence

from prometheus_client import (
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    start_http_server,
)
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector

logger = logging.getLogger(__name__)

# 计数器不导出 *_created 样本，减少时间序列。
disable_created_metrics()  # type: ignore[no-untyped-call]

_LATENCY = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_DELAY = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0)
_LLM = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)
_WAIT = (5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0)
_JOB = (0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 300.0)

# ---- API ----
HTTP_REQUESTS = Counter(
    "edp_http_requests", "HTTP 请求数（route 为路由模板）", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "edp_http_request_duration_seconds", "HTTP 请求耗时", ["method", "route"], buckets=_LATENCY
)
TENANT_REQUESTS = Counter(
    "edp_tenant_http_requests", "按租户统计的 HTTP 请求数", ["tenant", "status_class"]
)
RATE_LIMITED = Counter("edp_rate_limited", "被限流拒绝或推迟的请求", ["rule", "tenant"])

# ---- 消息链路 ----
WEBHOOKS = Counter("edp_webhooks", "收到的渠道回调", ["source", "tenant", "outcome"])
WEBHOOK_DELAY = Histogram(
    "edp_webhook_delay_seconds",
    "消息在 IM 发出到平台收到回调的时间",
    ["source"],
    buckets=_DELAY,
)
RECONCILE_RECOVERED = Counter("edp_reconcile_recovered", "对账补录的消息（回调丢失）", ["tenant"])
MESSAGES = Counter("edp_messages", "归入会话的消息", ["tenant", "sender_type"])

# ---- 事件流 ----
EVENTS_PUBLISHED = Counter("edp_events_published", "发布的事件", ["type", "tenant"])
EVENTS_PROCESSED = Counter(
    "edp_events_processed", "处理完的事件（ok 成功，dead 进入死信）", ["type", "tenant", "outcome"]
)
EVENT_RETRIES = Counter("edp_event_retries", "事件处理失败后在进程内重试的次数", ["type"])
EVENT_HANDLE = Histogram("edp_event_handle_seconds", "事件处理耗时", ["type"], buckets=_LATENCY)
EVENT_DELAY = Histogram(
    "edp_event_delay_seconds", "事件从发布到开始处理的时间", ["type"], buckets=_DELAY
)
DEAD_LETTERS = Counter("edp_dead_letters", "进入死信流的事件", ["type", "tenant"])

# ---- IM 发件箱 ----
IM_OPS = Counter(
    "edp_im_ops", "IM 发件箱操作的执行结果（done、retry、failed）", ["op", "tenant", "outcome"]
)

# ---- 大模型 ----
LLM_CALLS = Counter(
    "edp_llm_calls", "大模型调用", ["tenant", "scene", "provider", "model", "status"]
)
LLM_LATENCY = Histogram(
    "edp_llm_call_seconds", "大模型调用耗时（成功的调用）", ["scene", "provider"], buckets=_LLM
)
LLM_TOKENS = Counter(
    "edp_llm_tokens", "大模型 tokens（input、output）", ["tenant", "scene", "kind"]
)
LLM_COST = Counter("edp_llm_cost_fen", "大模型估算费用（分）", ["tenant", "scene"])

# ---- 会话与 AI ----
AI_HANDOFFS = Counter("edp_ai_handoffs", "AI 接待中的会话转人工", ["tenant", "reason"])
SESSIONS_CLOSED = Counter(
    "edp_sessions_closed",
    "结束的会话（served_by：ai 只有 AI 接待，human 人工接待过，none 排队中或无人接待）",
    ["tenant", "served_by"],
)
QUEUE_WAIT = Histogram(
    "edp_queue_wait_seconds", "从排队到分配给坐席的等待时间", ["tenant"], buckets=_WAIT
)
QUEUE_TIMEOUTS = Counter("edp_queue_timeouts", "排队超时转为留言的会话", ["tenant"])

# ---- 企业微信 ----
WECOM_API_ERRORS = Counter(
    "edp_wecom_api_errors",
    "企业微信接口返回的错误（errcode，-1 为网络或服务端故障）",
    ["api", "errcode"],
)

# ---- 调度任务 ----
# 标签用 task 而不是 job：job 是 Prometheus 的抓取任务标签，同名会被改成 exported_job。
JOB_RUNS = Counter("edp_job_runs", "调度任务的执行次数（ok、error）", ["task", "outcome"])
JOB_DURATION = Histogram("edp_job_duration_seconds", "调度任务耗时", ["task"], buckets=_JOB)
JOB_LAST_SUCCESS = Gauge(
    "edp_job_last_success_timestamp_seconds", "调度任务最近一次成功的时间", ["task"]
)
JOB_INTERVAL = Gauge("edp_job_interval_seconds", "调度任务的执行间隔", ["task"])


def tenant_label(tenant_id: uuid.UUID | str | None) -> str:
    return str(tenant_id) if tenant_id else ""


class _StateCollector(Collector):
    """状态类指标：导出最近一次读取的快照（调度进程整体替换）。"""

    def __init__(self) -> None:
        self.snapshot: Sequence[Metric] = ()

    def collect(self) -> Iterable[Metric]:
        return list(self.snapshot)

    def describe(self) -> Iterable[Metric]:
        return ()


STATE = _StateCollector()
REGISTRY.register(STATE)


def publish_state(metrics: Sequence[Metric]) -> None:
    STATE.snapshot = tuple(metrics)


_server_port: int | None = None


def start_metrics_server(port: int) -> None:
    """在 port 上暴露 /metrics（每个进程一次；端口被占用时只记日志）。"""
    global _server_port
    if port <= 0 or _server_port is not None:
        return
    try:
        start_http_server(port)
    except OSError:
        logger.warning("metrics port %s is not available; metrics are not exported", port)
        return
    _server_port = port
    logger.info("metrics on :%s/metrics", port)
