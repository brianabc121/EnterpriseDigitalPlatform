"""OpenAI 兼容协议的大模型客户端（设计文档 §11.5）。

DeepSeek、通义千问（阿里云百炼）、智谱 GLM、豆包（火山方舟）、Kimi、自部署的 vLLM 等都提供 OpenAI
兼容接口，用同一个客户端接入：

- chat：对话补全，可要求输出 JSON（response_format=json_object）。
- embed：文本向量（/embeddings），用于知识库的语义检索。

超时、限流（429）和服务端错误先在同一家重试，仍失败时换备用供应商；全部失败抛出 LLMUnavailable，
由调用方降级（AI 接待转人工）。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}
_EMBED_BATCH = 10


class LLMError(Exception):
    """一次调用失败（单个供应商）。"""


class LLMUnavailable(LLMError):
    """所有供应商都失败，或者没有配置大模型。"""


@dataclass(frozen=True)
class LLMEndpoint:
    """一家供应商：base_url 形如 https://api.deepseek.com/v1。"""

    base_url: str
    api_key: str
    chat_model: str
    fast_model: str = ""
    name: str = ""

    @property
    def provider(self) -> str:
        return self.name or urlsplit(self.base_url).netloc or "llm"


@dataclass(frozen=True)
class EmbedEndpoint:
    base_url: str
    api_key: str
    model: str
    dim: int
    send_dimensions: bool = False


@dataclass(frozen=True)
class ChatResult:
    content: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    provider: str
    model: str
    prompt_tokens: int
    latency_ms: int


class LLMClient:
    def __init__(
        self,
        primary: LLMEndpoint | None,
        *,
        fallback: LLMEndpoint | None = None,
        embed: EmbedEndpoint | None = None,
        timeout: float = 30.0,
        retries: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.embedding = embed
        self.retries = retries
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def enabled(self) -> bool:
        return self.primary is not None

    @property
    def can_embed(self) -> bool:
        return self.embedding is not None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        fast: bool = False,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int = 800,
    ) -> ChatResult:
        endpoints = [e for e in (self.primary, self.fallback) if e is not None]
        if not endpoints:
            raise LLMUnavailable("没有配置大模型")
        errors: list[str] = []
        for endpoint in endpoints:
            model = (endpoint.fast_model or endpoint.chat_model) if fast else endpoint.chat_model
            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            try:
                data, latency = await self._post(endpoint, "/chat/completions", body)
                choice = (data.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content") or ""
                usage = data.get("usage") or {}
                return ChatResult(
                    content=content,
                    provider=endpoint.provider,
                    model=str(data.get("model") or model),
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    latency_ms=latency,
                )
            except LLMError as exc:
                logger.warning("chat via %s failed: %s", endpoint.provider, exc)
                errors.append(f"{endpoint.provider}: {exc}")
        raise LLMUnavailable("; ".join(errors))

    async def embed(self, texts: list[str]) -> EmbedResult:
        endpoint = self.embedding
        if endpoint is None:
            raise LLMUnavailable("没有配置向量模型")
        vectors: list[list[float]] = []
        tokens = 0
        latency = 0
        for start in range(0, len(texts), _EMBED_BATCH):
            batch = texts[start : start + _EMBED_BATCH]
            body: dict[str, Any] = {"model": endpoint.model, "input": batch}
            if endpoint.send_dimensions:
                body["dimensions"] = endpoint.dim
            try:
                data, spent = await self._post(endpoint, "/embeddings", body)
            except LLMError as exc:
                raise LLMUnavailable(str(exc)) from exc
            latency += spent
            items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
            if len(items) != len(batch):
                raise LLMUnavailable("向量接口返回的条数与请求不一致")
            for item in items:
                vector = [float(x) for x in item.get("embedding") or []]
                if len(vector) != endpoint.dim:
                    raise LLMUnavailable(
                        f"向量维度为 {len(vector)}，与配置的 {endpoint.dim} 不一致"
                    )
                vectors.append(vector)
            tokens += int((data.get("usage") or {}).get("prompt_tokens") or 0)
        return EmbedResult(
            vectors=vectors,
            provider=urlsplit(endpoint.base_url).netloc or "llm",
            model=endpoint.model,
            prompt_tokens=tokens,
            latency_ms=latency,
        )

    async def _post(
        self, endpoint: LLMEndpoint | EmbedEndpoint, path: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        url = endpoint.base_url.rstrip("/") + path
        headers = {"authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
        last = "unknown error"
        for attempt in range(self.retries + 1):
            if attempt:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
            started = time.monotonic()
            try:
                response = await self._http.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            latency = int((time.monotonic() - started) * 1000)
            if response.status_code == 200:
                try:
                    data = response.json()
                except json.JSONDecodeError as exc:
                    raise LLMError("响应不是 JSON") from exc
                if not isinstance(data, dict):
                    raise LLMError("响应格式不正确")
                return data, latency
            last = f"HTTP {response.status_code}: {response.text[:200]}"
            if response.status_code not in _RETRYABLE:
                break
        raise LLMError(last)

    async def aclose(self) -> None:
        await self._http.aclose()
