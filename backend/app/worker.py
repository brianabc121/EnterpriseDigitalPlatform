"""实时消费进程：消费事件流（消息归入会话、路由分配等），处理 AI 接待的待回复会话，判断客户的
意图（设计文档 §32），判断每次提交的表单要不要更新表单知识（设计文档 §25.18），以及执行排队的
AI 唤醒（数据巡检和知识库整理，设计文档 §33）。

用法：uv run python -m app.worker。可以运行多个实例：每个事件分区同一时刻只由一个实例消费；
AI 待回复会话按行领取（SKIP LOCKED），多个实例不会重复回复。
"""

import asyncio
import logging
import os
import signal
import socket

from app.context import AppContext
from app.core.config import get_settings
from app.events.bus import EventProcessor, wait_or_stop
from app.modules.ai import intent
from app.modules.ai.responder import run_due
from app.modules.formkb import learn as form_learning
from app.modules.sessions.handlers import event_handlers
from app.modules.wake import runner as wake
from app.observability import logs, metrics, tracing

logger = logging.getLogger("app.worker")

AI_POLL_SECONDS = 0.5
INTENT_POLL_SECONDS = 0.5
FORM_KB_POLL_SECONDS = 1.0
WAKE_POLL_SECONDS = 5.0


async def ai_loop(ctx: AppContext, stop: asyncio.Event) -> None:
    """AI 接待：定期处理到期的待回复会话（大模型没有配置时空转）。"""
    while not stop.is_set():
        handled = 0
        if await ctx.llms.any_enabled():
            try:
                handled = await run_due(ctx)
            except Exception:
                logger.exception("AI responder failed")
        if not handled:
            await wait_or_stop(stop, AI_POLL_SECONDS)


async def intent_loop(ctx: AppContext, stop: asyncio.Event) -> None:
    """意图判断：定期判断到期的会话（没有登记时空转，只是一条按索引的查询）。"""
    while not stop.is_set():
        handled = 0
        try:
            handled = await intent.run_due(ctx)
        except Exception:
            logger.exception("intent judgment failed")
        if not handled:
            await wait_or_stop(stop, INTENT_POLL_SECONDS)


async def form_kb_loop(ctx: AppContext, stop: asyncio.Event) -> None:
    """表单知识：每秒领取待判断的学习记录（每次提交的表单），判断要不要更新知识库。"""
    while not stop.is_set():
        handled = 0
        try:
            handled = await form_learning.run_due(ctx)
        except Exception:
            logger.exception("form knowledge learning failed")
        if not handled:
            await wait_or_stop(stop, FORM_KB_POLL_SECONDS)


async def wake_loop(ctx: AppContext, stop: asyncio.Event) -> None:
    """AI 唤醒：每 5 秒领取排队的唤醒（按行领取，多个实例不会重复执行）。"""
    while not stop.is_set():
        handled = 0
        try:
            handled = await wake.run_due(ctx)
        except Exception:
            logger.exception("AI wake-up failed")
        if not handled:
            await wait_or_stop(stop, WAKE_POLL_SECONDS)


async def run(ctx: AppContext, stop: asyncio.Event) -> None:
    await ctx.bus.ensure_groups()
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    processor = EventProcessor(ctx.bus, event_handlers(ctx), consumer=consumer)
    logger.info("worker %s consuming %s partitions", consumer, ctx.bus.partitions)
    await asyncio.gather(
        *(processor.run_partition(p, stop) for p in range(ctx.bus.partitions)),
        ai_loop(ctx, stop),
        intent_loop(ctx, stop),
        form_kb_loop(ctx, stop),
        wake_loop(ctx, stop),
    )


async def _main() -> None:
    settings = get_settings()
    tracing.setup(settings, "worker")
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
