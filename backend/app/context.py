"""进程级依赖：API、实时消费进程、调度进程和命令行共用同一套对象的创建与关闭。"""

from dataclasses import dataclass
from functools import partial
from typing import Any

import httpx
from redis.asyncio import Redis

from app.core.config import Settings
from app.db.session import Database
from app.events.bus import EventBus
from app.integrations.asr import AsrClient
from app.integrations.clamav import ClamAV
from app.integrations.llm import EmbedEndpoint, LLMClient, LLMEndpoint, RerankEndpoint
from app.integrations.openim import OpenIMClient
from app.integrations.storage import ObjectStore
from app.integrations.wecom import WeComClient
from app.modules.ai.llm_router import LlmRouter
from app.modules.ai.prompt_store import PromptStore
from app.modules.conversation.deps import openim_from_settings
from app.modules.conversation.provisioning import IMProvisioner
from app.modules.files.service import storage_config
from app.modules.security.keys import TenantKeyring
from app.modules.wecom.credentials import corp_secret

CRAWLER_AGENT = "EDP-KnowledgeBot/1.0 (+help-center import)"


@dataclass
class AppContext:
    settings: Settings
    db: Database
    redis: Redis
    im: OpenIMClient
    provisioner: IMProvisioner
    bus: EventBus
    llm: LLMClient
    storage: ObjectStore
    # 没有配置企业微信服务商（EDP_WECOM_SUITE_ID）时为空。
    wecom: WeComClient | None
    # 按租户和场景选择大模型供应商（运营后台配置的供应商、租户自带的接口密钥）；llm 是环境变量
    # 配置的供应商，没有在运营后台配置供应商时使用。
    llms: LlmRouter
    # 租户数据密钥（信封加密）：渠道凭证、自带模型密钥、客户手机号等敏感字段。
    keys: TenantKeyring
    # 提示词版本（运营后台启用的版本，没有时用内置提示词）。
    prompts: PromptStore
    # 抓取官网帮助中心等外部网页（知识导入）。不自动跟随跳转：每一跳都要检查地址。
    web: httpx.AsyncClient
    # 没有配置语音转文字（EDP_ASR_BASE_URL）时为空。
    asr: AsrClient | None = None
    # 没有配置病毒扫描（EDP_CLAMAV_HOST）时为空。
    clamav: ClamAV | None = None

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        im: OpenIMClient | None = None,
        llm: LLMClient | None = None,
        wecom_transport: httpx.AsyncBaseTransport | None = None,
        storage_transport: httpx.AsyncBaseTransport | None = None,
        asr_transport: httpx.AsyncBaseTransport | None = None,
        llm_transport: httpx.AsyncBaseTransport | None = None,
        web_transport: httpx.AsyncBaseTransport | None = None,
    ) -> "AppContext":
        redis = Redis.from_url(settings.redis_url)
        db = Database(settings)
        keys = TenantKeyring(settings, db)
        im = im or openim_from_settings(settings)
        wecom = None
        if settings.wecom_enabled:
            wecom = WeComClient(
                base_url=settings.wecom_api_url,
                suite_id=settings.wecom_suite_id,
                suite_secret=settings.wecom_suite_secret.get_secret_value(),
                redis=redis,
                corp_secret=partial(corp_secret, db, keys),
                transport=wecom_transport,
            )
        asr = None
        if settings.asr_base_url and settings.asr_model:
            asr = AsrClient(
                base_url=settings.asr_base_url,
                api_key=settings.asr_api_key.get_secret_value(),
                model=settings.asr_model,
                transport=asr_transport,
            )
        env_llm = llm or llm_from_settings(settings, transport=llm_transport)
        return cls(
            settings=settings,
            db=db,
            redis=redis,
            im=im,
            provisioner=IMProvisioner(im),
            bus=EventBus(redis),
            llm=env_llm,
            storage=ObjectStore(storage_config(settings), transport=storage_transport),
            wecom=wecom,
            llms=LlmRouter(settings, db, env_llm, keys, transport=llm_transport),
            keys=keys,
            prompts=PromptStore(db),
            web=httpx.AsyncClient(
                transport=web_transport,
                timeout=settings.kb_crawl_timeout_seconds,
                follow_redirects=False,
                headers={"User-Agent": CRAWLER_AGENT},
            ),
            asr=asr,
            clamav=(
                ClamAV(
                    settings.clamav_host,
                    settings.clamav_port,
                    timeout=settings.clamav_timeout_seconds,
                )
                if settings.clamav_host
                else None
            ),
        )

    async def aclose(self) -> None:
        if self.asr is not None:
            await self.asr.aclose()
        if self.wecom is not None:
            await self.wecom.aclose()
        await self.storage.aclose()
        await self.web.aclose()
        await self.llms.aclose()
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
            base_url=base_url,
            api_key=key,
            chat_model=chat,
            fast_model=fast,
            name=name,
            price_input=settings.llm_price_input,
            price_output=settings.llm_price_output,
            supports_tools=settings.llm_supports_tools,
        )

    embed_base = settings.llm_embed_base_url or settings.llm_base_url
    embed_key = (
        settings.llm_embed_api_key.get_secret_value() or settings.llm_api_key.get_secret_value()
    )
    embed = None
    if settings.llm_embed_model and embed_base:
        embed = EmbedEndpoint(
            base_url=embed_base,
            api_key=embed_key,
            model=settings.llm_embed_model,
            dim=settings.llm_embed_dim,
            send_dimensions=settings.llm_embed_send_dimensions,
            price=settings.llm_embed_price,
        )
    rerank = None
    rerank_base = settings.llm_rerank_base_url or embed_base
    if settings.llm_rerank_model and rerank_base:
        rerank = RerankEndpoint(
            base_url=rerank_base,
            api_key=settings.llm_rerank_api_key.get_secret_value() or embed_key,
            model=settings.llm_rerank_model,
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
        rerank=rerank,
        timeout=settings.llm_timeout_seconds,
        **kwargs,
    )
