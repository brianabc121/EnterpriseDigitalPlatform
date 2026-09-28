"""实时消费进程：消费事件流（消息归入会话、路由分配等）。

用法：uv run python -m app.worker。可以运行多个实例：每个事件分区同一时刻只由一个实例消费。
"""

import asyncio
import logging
import os
import signal
import socket

from app.context import AppContext
from app.core.config import get_settings
from app.events.bus import EventProcessor
from app.modules.sessions.handlers import event_handlers

logger = logging.getLogger("app.worker")


async def run(ctx: AppContext, stop: asyncio.Event) -> None:
    await ctx.bus.ensure_groups()
    consumer = f"{socket.gethostname()}-{os.getpid()}"
    processor = EventProcessor(ctx.bus, event_handlers(ctx), consumer=consumer)
    logger.info("worker %s consuming %s partitions", consumer, ctx.bus.partitions)
    await asyncio.gather(*(processor.run_partition(p, stop) for p in range(ctx.bus.partitions)))


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
