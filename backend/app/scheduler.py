"""调度进程：每分钟按 seq 对账，补录回调丢失的消息。

用法：uv run python -m app.scheduler（部署时只运行一个实例；多实例重复对账也不会产生重复消息）。
"""

import asyncio
import logging
import time

from app.core.config import Settings, get_settings
from app.db.session import Database
from app.modules.conversation.deps import openim_from_settings
from app.modules.conversation.reconcile import reconcile_all

logger = logging.getLogger("app.scheduler")

RECONCILE_INTERVAL_SECONDS = 60


async def run(settings: Settings, *, interval: float = RECONCILE_INTERVAL_SECONDS) -> None:
    db = Database(settings)
    im = openim_from_settings(settings)
    try:
        while True:
            started = time.monotonic()
            try:
                report = await reconcile_all(db, im)
            except Exception:
                logger.exception("reconcile failed")
            else:
                logger.info("reconcile: %s", report)
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))
    finally:
        await im.aclose()
        await db.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run(get_settings()))


if __name__ == "__main__":
    main()
