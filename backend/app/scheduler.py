"""调度进程：周期任务。

- 按 seq 对账，补录回调丢失的消息（每分钟）；
- 会话定时处理：断线坐席下线、排队超时转留言、空闲会话结束、分配排队会话（每 10 秒）；
- 超时未接受的会话转接退回原坐席（每 5 秒）；
- 执行到期的 IM 发件箱操作（每 5 秒）；
- 重新发布没有归入会话的消息事件（每 30 秒）；
- 汇总当天和前一天的用量（每 10 分钟）；
- 有效期已过的知识自动下线（每 10 分钟）；
- 从最近结束的会话提炼知识候选（每小时）；
- 每周一生成上一周的知识周报（每小时检查）；
- 微信客服兜底拉取消息，防止回调丢失（每 5 分钟）；
- 回收企业微信在职继承、离职继承的结果（每小时）；
- 回收企业微信群发任务的发送结果（每 30 分钟）；
- 从数据与智能专区取回群聊分析结果（每小时，开启了专区的企业）；
- 标记到期的订阅，宽限期过后停用租户（每小时）；
- 生成上个月的账单（每 6 小时检查，已收款的不变）；
- 生成排队中的数据导出、删除过期的导出文件（每 30 秒）；
- 删除注销保留期已到的租户数据（每小时）；
- 待办：发送提醒、待确认再提醒、到期提醒、逾期提醒与升级（每 15 秒）；每个工作日上班后发送
  今日待办汇总（每 5 分钟检查）；解析最近结束的人工会话，生成待确认的待办（每分钟）；
- 订单：暂欠逾期生成催收待办（每 10 分钟）；AI 采集的草稿超时生成跟进待办（每 5 分钟）；
- 向企业系统推送订单和待办的变化：分发新事件、投递到期的推送（每 10 秒），删除过期的发件箱
  记录（每小时）；
- 读取排队、坐席、发件箱、事件积压等状态类指标（每 15 秒，见 app/observability/state.py）；
- 建好本月和之后 3 个月的消息分区（每小时，见 app/db/partitions.py）；
- 收取到期的邮箱的新邮件（每 10 秒检查，每个邮箱按设置的间隔收取，见 app/modules/mail/inbox.py）。
- 个人待办：到期和逾期提醒（每分钟）、每个工作日上班后的今日汇总（每 5 分钟检查）；
- AI 助理记录的群聊：提炼知识候选（每小时，见 app/modules/assistant/extraction.py）；
- AI 唤醒：按每个企业的设置登记到期的数据巡检和知识库整理（每分钟），删除 90 天前的唤醒记录
  （每小时），见 app/modules/wake/runner.py。

每个任务的执行次数、耗时和最近一次成功的时间计入 Prometheus 指标，每次执行是一个 span。

用法：uv run python -m app.scheduler。可以运行多个实例：持有租约的实例执行任务，其他实例待命
（不导出状态类指标）。
"""

import asyncio
import logging
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from opentelemetry.trace import StatusCode

from app.context import AppContext
from app.core.config import get_settings
from app.db.partitions import ensure_partitions
from app.events.bus import wait_or_stop
from app.events.lease import Lease
from app.modules.ai.answer_cache import purge_expired as purge_expired_answers
from app.modules.ai.summaries import run_pending as run_session_summaries
from app.modules.assistant.extraction import run_group_extraction
from app.modules.billing.service import run_invoices, run_lifecycle
from app.modules.conversation.outbox import dispatch_due
from app.modules.conversation.reconcile import reconcile_all
from app.modules.finance.jobs import run_overdue_reminders as run_finance_reminders
from app.modules.formkb.learn import purge as purge_form_learning
from app.modules.integration.delivery import purge_events as purge_webhook_events
from app.modules.integration.delivery import run as run_webhooks
from app.modules.kb.extraction import run_extraction
from app.modules.kb.importer import run_imports
from app.modules.kb.metrics import run_digests
from app.modules.kb.reminders import remind_expiring
from app.modules.kb.service import expire_items
from app.modules.lifecycle.closure import run_purges
from app.modules.lifecycle.export import run_exports
from app.modules.mail.inbox import poll_due as poll_mailboxes
from app.modules.orders.jobs import run_collections as run_order_collections
from app.modules.orders.jobs import run_draft_followups as run_order_followups
from app.modules.print import delivery as print_delivery
from app.modules.products.service import embed_pending as embed_products
from app.modules.security.retention import run_retention
from app.modules.security.scanning import run_file_scan
from app.modules.sessions.engine import republish_orphans, run_session_timers
from app.modules.sessions.transfer import run_transfer_timers
from app.modules.tasks.notify import run_digest as run_task_digest
from app.modules.tasks.notify import run_timers as run_task_timers
from app.modules.todos.extract import run_pending as run_todo_extraction
from app.modules.todos.notify import run_digest as run_todo_digest
from app.modules.todos.notify import run_timers as run_todo_timers
from app.modules.usage.service import run_usage_rollup
from app.modules.wake import runner as wake
from app.modules.wecom.contacts import poll_transfers
from app.modules.wecom.kf import sync_all as kf_sync_all
from app.modules.wecom.marketing import poll_broadcasts
from app.modules.wecom.zone import pull_zone_results
from app.observability import logs, metrics, state, tracing

logger = logging.getLogger("app.scheduler")

LEASE_KEY = "edp:scheduler:lease"


@dataclass(frozen=True)
class Job:
    name: str
    interval: float
    run: Callable[[AppContext], Awaitable[object]]


async def _reconcile(ctx: AppContext) -> object:
    return await reconcile_all(ctx.db, ctx.im, bus=ctx.bus)


JOBS = (
    Job("reconcile", 60, _reconcile),
    Job("session-timers", 10, run_session_timers),
    Job("transfer-timers", 5, run_transfer_timers),
    Job("im-ops", 5, dispatch_due),
    Job("orphan-messages", 30, republish_orphans),
    Job("usage-rollup", 600, run_usage_rollup),
    Job("kb-expire", 600, expire_items),
    Job("kb-expiry-remind", 3600, remind_expiring),
    Job("kb-imports", 10, run_imports),
    Job("kb-extract", 3600, run_extraction),
    Job("kb-digest", 3600, run_digests),
    Job("wecom-kf-sync", 300, kf_sync_all),
    Job("wecom-transfers", 3600, poll_transfers),
    Job("wecom-broadcasts", 1800, poll_broadcasts),
    Job("wecom-zone", 3600, pull_zone_results),
    Job("billing-lifecycle", 3600, run_lifecycle),
    Job("billing-invoices", 6 * 3600, run_invoices),
    Job("tenant-exports", 30, run_exports),
    Job("tenant-purge", 3600, run_purges),
    Job("retention", 3600, run_retention),
    Job("file-scan", 60, run_file_scan),
    Job("ai-cache", 3600, purge_expired_answers),
    Job("session-summaries", 60, run_session_summaries),
    Job("todo-timers", 15, run_todo_timers),
    Job("todo-digest", 300, run_todo_digest),
    Job("finance-overdue-remind", 1800, run_finance_reminders),
    Job("todo-extract", 60, run_todo_extraction),
    Job("task-timers", 60, run_task_timers),
    Job("task-digest", 300, run_task_digest),
    Job("assistant-group-extract", 3600, run_group_extraction),
    Job("order-collections", 600, run_order_collections),
    Job("order-draft-followups", 300, run_order_followups),
    Job("product-embed", 60, embed_products),
    Job("webhooks", 10, run_webhooks),
    Job("print-jobs", 5, print_delivery.run),
    Job("print-confirm", 30, print_delivery.run_confirm),
    Job("printer-status", 600, print_delivery.run_status),
    Job("print-jobs-purge", 3600, print_delivery.run_purge),
    Job("webhook-events-purge", 3600, purge_webhook_events),
    Job("metrics-state", state.INTERVAL_SECONDS, state.refresh),
    Job("partitions", 3600, ensure_partitions),
    Job("form-kb-purge", 3600, purge_form_learning),
    Job("mail-poll", 10, poll_mailboxes),
    Job("wake-dispatch", 60, wake.dispatch),
    Job("wake-purge", 3600, wake.purge),
)


async def run_once(ctx: AppContext, job: Job) -> object:
    """执行一次任务，记录指标和 span。失败时抛出异常。"""
    started = time.perf_counter()
    with tracing.tracer().start_as_current_span(f"job {job.name}") as span:
        try:
            result = await job.run(ctx)
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR)
            metrics.JOB_RUNS.labels(job.name, "error").inc()
            raise
        finally:
            metrics.JOB_DURATION.labels(job.name).observe(time.perf_counter() - started)
    metrics.JOB_RUNS.labels(job.name, "ok").inc()
    metrics.JOB_LAST_SUCCESS.labels(job.name).set(time.time())
    return result


async def run_job(ctx: AppContext, job: Job, stop: asyncio.Event, lease: Lease) -> None:
    metrics.JOB_INTERVAL.labels(job.name).set(job.interval)
    while not stop.is_set():
        started = time.monotonic()
        try:
            if await lease.hold():
                result = await run_once(ctx, job)
                logger.debug("%s: %s", job.name, result)
            elif job.run is state.refresh:
                # 没有租约的实例不导出状态类指标，避免与持有租约的实例重复或过时。
                state.clear()
        except Exception:
            logger.exception("job %s failed", job.name)
        await wait_or_stop(stop, max(0.0, job.interval - (time.monotonic() - started)))


async def run(ctx: AppContext, stop: asyncio.Event) -> None:
    lease = Lease(ctx.redis, LEASE_KEY, f"{socket.gethostname()}-{os.getpid()}", ttl_ms=30_000)
    try:
        await asyncio.gather(*(run_job(ctx, job, stop, lease) for job in JOBS))
    finally:
        await lease.release()
        state.clear()


async def _main() -> None:
    settings = get_settings()
    tracing.setup(settings, "scheduler")
    metrics.start_metrics_server(settings.metrics_port)
    ctx = AppContext.create(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await run(ctx, stop)
    finally:
        await ctx.aclose()
        tracing.shutdown()


def main() -> None:
    logs.configure(get_settings())
    asyncio.run(_main())


if __name__ == "__main__":
    main()
