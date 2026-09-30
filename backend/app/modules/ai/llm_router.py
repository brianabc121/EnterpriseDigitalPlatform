"""大模型路由（设计文档 D8、§11.5）：每次调用按租户和场景选择供应商。

对话模型的选择顺序：
1. 租户自带的接口密钥（AI 设置里的 byo_llm）：只用它，失败时转人工，不改用平台的供应商；
2. 平台运营给租户指定的供应商（ai_settings.llm_provider_id），平台默认供应商作为备用；
3. 按场景的路由（平台设置 llm_routes，例如知识提炼用便宜的模型），平台默认供应商作为备用；
4. 平台默认供应商（llm_providers.is_default）；
5. 环境变量配置的供应商（EDP_LLM_*，没有在运营后台配置供应商时使用）。

向量模型只用平台级的配置（默认供应商配置了向量模型时用它，否则用环境变量），保证同一个知识库的
向量来自同一个模型；更换向量模型后需要执行 kb-reindex。重排序模型同样只用平台级的配置。

每个供应商登记价格（估算费用）和能力标签（是否支持工具调用等），租户自带的接口不计费用。

供应商和路由在进程内缓存几秒；客户端按配置复用，配置变化后换新的客户端。
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import Settings
from app.core.crypto import DecryptError, unseal
from app.db.session import Database
from app.integrations.llm import EmbedEndpoint, LLMClient, LLMEndpoint, RerankEndpoint
from app.modules.security.keys import TenantKeyring

logger = logging.getLogger(__name__)

ROUTES_KEY = "llm_routes"
SCENES = {
    "reply": "AI 接待回复",
    "test": "AI 设置里的试一试",
    "rewrite": "问题改写",
    "suggest": "坐席助手建议回复",
    "summary": "转人工摘要",
    "session_summary": "会话小结",
    "copilot": "坐席实时提醒",
    "extract": "知识提炼",
    "phrase": "优秀话术挖掘",
    "todo_extract": "待办解析",
    "order_extract": "订单解析",
    "evaluate": "AI 评测",
}
CACHE_SECONDS = 5.0


class LlmRoutes(BaseModel):
    """按场景指定供应商（平台设置 llm_routes）。没有列出的场景用默认供应商。"""

    routes: dict[str, uuid.UUID] = Field(default_factory=dict)


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    name: str
    base_url: str
    api_key: str
    chat_model: str
    fast_model: str
    embed_model: str
    embed_dim: int
    send_dimensions: bool
    rerank_model: str = ""
    price_input: float = 0.0
    price_output: float = 0.0
    supports_tools: bool = False

    def endpoint(self) -> LLMEndpoint:
        return LLMEndpoint(
            base_url=self.base_url,
            api_key=self.api_key,
            chat_model=self.chat_model,
            fast_model=self.fast_model,
            name=self.name,
            price_input=self.price_input,
            price_output=self.price_output,
            supports_tools=self.supports_tools,
        )

    def embedding(self) -> EmbedEndpoint | None:
        if not self.embed_model:
            return None
        return EmbedEndpoint(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.embed_model,
            dim=self.embed_dim,
            send_dimensions=self.send_dimensions,
            price=self.price_input,
        )

    def reranking(self) -> RerankEndpoint | None:
        if not self.rerank_model:
            return None
        return RerankEndpoint(base_url=self.base_url, api_key=self.api_key, model=self.rerank_model)


@dataclass(frozen=True)
class _Platform:
    providers: dict[str, ProviderConfig]
    default: ProviderConfig | None
    routes: dict[str, str]


class LlmRouter:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        env: LLMClient,
        keys: TenantKeyring,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._keys = keys
        self.env = env
        self._transport = transport
        self._platform: _Platform | None = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        self._clients: dict[tuple[Any, ...], LLMClient] = {}
        self._any: tuple[float, bool] | None = None

    def invalidate(self) -> None:
        """供应商、路由或租户自带的密钥变化后调用。"""
        self._platform = None
        self._any = None

    async def _load(self) -> _Platform:
        if self._platform is not None and time.monotonic() - self._loaded_at < CACHE_SECONDS:
            return self._platform
        async with self._lock:
            if self._platform is not None and time.monotonic() - self._loaded_at < CACHE_SECONDS:
                return self._platform
            from app.modules.platform import settings as platform_settings
            from app.modules.platform.models import LlmProvider

            async with self._db.platform_sessionmaker() as session:
                rows = (
                    await session.scalars(select(LlmProvider).where(LlmProvider.enabled.is_(True)))
                ).all()
                routes = await platform_settings.read(session, ROUTES_KEY, LlmRoutes)
            providers: dict[str, ProviderConfig] = {}
            default = None
            for row in rows:
                config = self._provider(row)
                if config is None:
                    continue
                providers[config.id] = config
                if row.is_default:
                    default = config
            self._platform = _Platform(
                providers=providers,
                default=default,
                routes={scene: str(pid) for scene, pid in routes.routes.items()},
            )
            self._loaded_at = time.monotonic()
            return self._platform

    def _provider(self, row: Any) -> ProviderConfig | None:
        try:
            key = unseal(self._settings, row.api_key_enc) if row.api_key_enc else ""
        except DecryptError:
            logger.error("cannot decrypt the api key of llm provider %s", row.id)
            return None
        prices = row.prices or {}
        return ProviderConfig(
            id=str(row.id),
            name=row.name,
            base_url=row.base_url,
            api_key=key,
            chat_model=row.chat_model,
            fast_model=row.fast_model,
            embed_model=row.embed_model,
            embed_dim=row.embed_dim,
            send_dimensions=row.send_dimensions,
            rerank_model=row.rerank_model or "",
            price_input=float(prices.get("input") or 0),
            price_output=float(prices.get("output") or 0),
            supports_tools=bool((row.capabilities or {}).get("tools")),
        )

    def _client(
        self,
        primary: LLMEndpoint | None,
        fallback: LLMEndpoint | None = None,
        embed: EmbedEndpoint | None = None,
        rerank: RerankEndpoint | None = None,
    ) -> LLMClient:
        key = (primary, fallback, embed, rerank)
        client = self._clients.get(key)
        if client is None:
            client = LLMClient(
                primary,
                fallback=fallback,
                embed=embed,
                rerank=rerank,
                timeout=self._settings.llm_timeout_seconds,
                retries=self.env.retries,
                transport=self._transport,
            )
            self._clients[key] = client
        return client

    def new_client(
        self, primary: LLMEndpoint | None, *, embed: EmbedEndpoint | None = None
    ) -> LLMClient:
        """一次性的客户端（检查配置用，调用方负责关闭），不重试。"""
        return LLMClient(
            primary,
            embed=embed,
            timeout=self._settings.llm_timeout_seconds,
            retries=0,
            transport=self._transport,
        )

    async def _tenant(self, tenant_id: uuid.UUID) -> tuple[str | None, dict[str, Any] | None]:
        from app.modules.ai.models import AiSettings

        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    select(AiSettings.llm_provider_id, AiSettings.byo_llm).where(
                        AiSettings.tenant_id == tenant_id
                    )
                )
            ).first()
        if row is None:
            return None, None
        provider_id, byo = row
        return (str(provider_id) if provider_id else None), byo

    async def byo_endpoint(
        self, tenant_id: uuid.UUID, byo: dict[str, Any] | None
    ) -> LLMEndpoint | None:
        if not byo or not byo.get("enabled", True) or not byo.get("base_url"):
            return None
        try:
            sealed = byo.get("api_key_enc")
            key = await self._keys.unseal(tenant_id, sealed) if sealed else ""
        except DecryptError:
            logger.error("cannot decrypt the own llm key of tenant %s", tenant_id)
            return None
        return LLMEndpoint(
            base_url=str(byo["base_url"]),
            api_key=key,
            chat_model=str(byo.get("chat_model") or ""),
            fast_model=str(byo.get("fast_model") or ""),
            name="tenant",
            supports_tools=bool(byo.get("supports_tools")),
        )

    async def chat_client(self, tenant_id: uuid.UUID, scene: str = "reply") -> LLMClient:
        """这个租户、这个场景使用的对话客户端（可能没有启用）。"""
        provider_id, byo = await self._tenant(tenant_id)
        own = await self.byo_endpoint(tenant_id, byo)
        if own is not None:
            return self._client(own)
        platform = await self._load()
        default = platform.default.endpoint() if platform.default else None
        chosen = platform.providers.get(provider_id or "") or platform.providers.get(
            platform.routes.get(scene, "")
        )
        if chosen is not None:
            fallback = default if default != chosen.endpoint() else None
            return self._client(chosen.endpoint(), fallback)
        if default is not None:
            return self._client(default)
        return self.env

    async def embed_client(self) -> LLMClient:
        platform = await self._load()
        embedding = platform.default.embedding() if platform.default else None
        if embedding is not None:
            return self._client(None, embed=embedding)
        return self.env

    async def rerank_client(self) -> LLMClient:
        platform = await self._load()
        reranking = platform.default.reranking() if platform.default else None
        if reranking is not None:
            return self._client(None, rerank=reranking)
        return self.env

    async def rerank_enabled(self) -> bool:
        return (await self.rerank_client()).can_rerank

    async def chat_enabled(self, tenant_id: uuid.UUID, scene: str = "reply") -> bool:
        return (await self.chat_client(tenant_id, scene)).enabled

    async def tools_supported(self, tenant_id: uuid.UUID, scene: str = "reply") -> bool:
        """这个租户、这个场景使用的模型是否支持工具调用（供应商的能力标签）。"""
        return (await self.chat_client(tenant_id, scene)).supports_tools

    async def embed_enabled(self) -> bool:
        return (await self.embed_client()).can_embed

    async def any_enabled(self) -> bool:
        """平台上是否可能有租户用得上大模型（实时消费进程据此决定是否轮询 AI 待回复）。"""
        if self.env.enabled:
            return True
        platform = await self._load()
        if platform.providers:
            return True
        if self._any is not None and time.monotonic() - self._any[0] < CACHE_SECONDS:
            return self._any[1]
        from app.modules.ai.models import AiSettings

        async with self._db.platform_sessionmaker() as session:
            found = await session.scalar(
                select(AiSettings.tenant_id).where(AiSettings.byo_llm.is_not(None)).limit(1)
            )
        self._any = (time.monotonic(), found is not None)
        return found is not None

    async def describe(self, tenant_id: uuid.UUID) -> tuple[str, str | None]:
        """（来源，供应商名称）：来源为 tenant、provider、default、env 或 none。"""
        provider_id, byo = await self._tenant(tenant_id)
        if await self.byo_endpoint(tenant_id, byo) is not None:
            return "tenant", "自带接口密钥"
        platform = await self._load()
        if provider_id and provider_id in platform.providers:
            return "provider", platform.providers[provider_id].name
        if platform.default is not None:
            return "default", platform.default.name
        if self.env.enabled and self.env.primary is not None:
            return "env", self.env.primary.provider
        return "none", None

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
