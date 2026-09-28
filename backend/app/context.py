"""进程级依赖：API、实时消费进程、调度进程和命令行共用同一套对象的创建与关闭。"""

from dataclasses import dataclass

from redis.asyncio import Redis

from app.core.config import Settings
from app.db.session import Database
from app.events.bus import EventBus
from app.integrations.openim import OpenIMClient
from app.modules.conversation.deps import openim_from_settings
from app.modules.conversation.provisioning import IMProvisioner


@dataclass
class AppContext:
    settings: Settings
    db: Database
    redis: Redis
    im: OpenIMClient
    provisioner: IMProvisioner
    bus: EventBus

    @classmethod
    def create(cls, settings: Settings, *, im: OpenIMClient | None = None) -> "AppContext":
        redis = Redis.from_url(settings.redis_url)
        im = im or openim_from_settings(settings)
        return cls(
            settings=settings,
            db=Database(settings),
            redis=redis,
            im=im,
            provisioner=IMProvisioner(im),
            bus=EventBus(redis),
        )

    async def aclose(self) -> None:
        await self.im.aclose()
        await self.redis.aclose()
        await self.db.dispose()
