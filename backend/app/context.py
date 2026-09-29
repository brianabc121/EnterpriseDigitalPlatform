"""进程级依赖：API、实时消费进程、调度进程和命令行共用同一套对象的创建与关闭。"""

from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from app.core.config import Settings
from app.db.session import Database
from app.events.bus import EventBus
from app.integrations.llm import EmbedEndpoint, LLMClient, LLMEndpoint
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
    llm: LLMClient

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        im: OpenIMClient | None = None,
        llm: LLMClient | None = None,
    ) -> "AppContext":
        redis = Redis.from_url(settings.redis_url)
        im = im or openim_from_settings(settings)
        return cls(
            settings=settings,
            db=Database(settings),
            redis=redis,
            im=im,
            provisioner=IMProvisioner(im),
            bus=EventBus(redis),
            llm=llm or llm_from_settings(settings),
        )

    async def aclose(self) -> None:
        await self.llm.aclose()
        await self.im.aclose()
        await self.redis.aclose()
        await self.db.dispose()


def llm_from_settings(settings: Settings, **kwargs: Any) -> LLMClient:
    """按配置创建大模型客户端；没有配置时返回一个未启用的客户端。"""

    def endpoint(
        name: str, base_url: str, key: str, chat: str, fast: str = ""
    ) -> LLMEndpoint | None:
        if not base_url or not chat:
            return None
        return LLMEndpoint(
            base_url=base_url, api_key=key, chat_model=chat, fast_model=fast, name=name
        )

    embed = None
    if settings.llm_embed_model and (settings.llm_embed_base_url or settings.llm_base_url):
        embed = EmbedEndpoint(
            base_url=settings.llm_embed_base_url or settings.llm_base_url,
            api_key=settings.llm_embed_api_key.get_secret_value()
            or settings.llm_api_key.get_secret_value(),
            model=settings.llm_embed_model,
            dim=settings.llm_embed_dim,
            send_dimensions=settings.llm_embed_send_dimensions,
        )
    return LLMClient(
        endpoint(
            settings.llm_provider,
            settings.llm_base_url,
            settings.llm_api_key.get_secret_value(),
            settings.llm_chat_model,
            settings.llm_fast_model,
        ),
        fallback=endpoint(
            settings.llm_fallback_provider,
            settings.llm_fallback_base_url,
            settings.llm_fallback_api_key.get_secret_value(),
            settings.llm_fallback_chat_model,
        ),
        embed=embed,
        timeout=settings.llm_timeout_seconds,
        **kwargs,
    )
