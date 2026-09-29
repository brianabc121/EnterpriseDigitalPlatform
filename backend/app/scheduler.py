"""调度进程：周期任务。

- 按 seq 对账，补录回调丢失的消息（每分钟）；
- 会话定时处理：断线坐席下线、排队超时转留言、空闲会话结束、分配排队会话（每 10 秒）；
- 超时未接受的会话转接退回原坐席（每 5 秒）；
- 执行到期的 IM 发件箱操作（每 5 秒）；
- 重新发布没有归入会话的消息事件（每 30 秒）；
- 汇总当天和前一天的用量（每 10 分钟）；
- 有效期已过的知识自动下线（每 10 分钟）；
- 从最近结束的会话提炼知识候选（每小时）；
- 每周一生成上一周的知识周报（每小时检查）。

用法：uv run python -m app.scheduler。可以运行多个实例：持有租约的实例执行任务，其他实例待命。
"""

import asyncio
import logging
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.context import AppContext
from app.core.config import get_settings
from app.events.bus import wait_or_stop
from app.events.lease import Lease
from app.modules.conversation.outbox import dispatch_due
from app.modules.conversation.reconcile import reconcile_all
from app.modules.kb.extraction import run_extraction
from app.modules.kb.metrics import run_digests
from app.modules.kb.service import expire_items
from app.modules.sessions.engine import republish_orphans, run_session_timers
from app.modules.sessions.transfer import run_transfer_timers
from app.modules.usage.service import run_usage_rollup

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
    Job("kb-extract", 3600, run_extraction),
    Job("kb-digest", 3600, run_digests),
)


async def run_job(ctx: AppContext, job: Job, stop: asyncio.Event, lease: Lease) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            if await lease.hold():
                result = await job.run(ctx)
                logger.debug("%s: %s", job.name, result)
        except Exception:
            logger.exception("job %s failed", job.name)
        await wait_or_stop(stop, max(0.0, job.interval - (time.monotonic() - started)))


async def run(ctx: AppContext, stop: asyncio.Event) -> None:
    lease = Lease(ctx.redis, LEASE_KEY, f"{socket.gethostname()}-{os.getpid()}", ttl_ms=30_000)
    try:
        await asyncio.gather(*(run_job(ctx, job, stop, lease) for job in JOBS))
    finally:
        await lease.release()


async def _main() -> None:
    ctx = AppContext.create(get_settings())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await run(ctx, stop)
    finally:
        await ctx.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # 每个 HTTP 请求一行的日志太多，只保留警告。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
