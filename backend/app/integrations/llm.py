"""OpenAI 兼容协议的大模型客户端（设计文档 §11.5）。

DeepSeek、通义千问（阿里云百炼）、智谱 GLM、豆包（火山方舟）、Kimi、自部署的 vLLM 等都提供 OpenAI
兼容接口，用同一个客户端接入：

- chat：对话补全，可要求输出 JSON（response_format=json_object），可带工具（function calling）。
- embed：文本向量（/embeddings），用于知识库的语义检索。
- rerank：重排序（/rerank，bge-reranker 等常见的 {query, documents} 协议），可选。

每次调用按供应商的价格（每千 tokens 多少分）估算费用。

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

from app.core.errors import Unprocessable
from app.core.urls import resolve_outbound

logger = logging.getLogger(__name__)

_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}
_EMBED_BATCH = 10


class LLMError(Exception):
    """一次调用失败（单个供应商）。"""


class LLMUnavailable(LLMError):
    """所有供应商都失败，或者没有配置大模型。"""


@dataclass(frozen=True)
class LLMEndpoint:
    """一家供应商：base_url 形如 https://api.deepseek.com/v1。价格为每千 tokens 多少分。"""

    base_url: str
    api_key: str
    chat_model: str
    fast_model: str = ""
    name: str = ""
    price_input: float = 0.0
    price_output: float = 0.0
    # 模型支持函数调用（tools）；不支持时调用方给的工具被忽略。
    supports_tools: bool = False
    # 租户自带的接口（生产环境）：每次调用前重新检查地址并固定解析到的 IP（见 core/urls.py）。
    pinned: bool = False

    @property
    def provider(self) -> str:
        return self.name or urlsplit(self.base_url).netloc or "llm"

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.price_input + completion_tokens * self.price_output) / 1000


@dataclass(frozen=True)
class EmbedEndpoint:
    base_url: str
    api_key: str
    model: str
    dim: int
    send_dimensions: bool = False
    price: float = 0.0


@dataclass(frozen=True)
class RerankEndpoint:
    base_url: str
    api_key: str
    model: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatResult:
    content: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    cost: float = 0.0
    tool_calls: tuple[ToolCall, ...] = ()
    # 模型要调用工具时的原始回复，回传工具结果时要原样放回对话里。
    message: dict[str, Any] | None = None


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    provider: str
    model: str
    prompt_tokens: int
    latency_ms: int
    cost: float = 0.0


@dataclass(frozen=True)
class RerankResult:
    scores: list[float]  # 与 documents 一一对应，0 到 1
    provider: str
    model: str
    latency_ms: int


class LLMClient:
    def __init__(
        self,
        primary: LLMEndpoint | None,
        *,
        fallback: LLMEndpoint | None = None,
        embed: EmbedEndpoint | None = None,
        rerank: RerankEndpoint | None = None,
        timeout: float = 30.0,
        retries: int = 1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.embedding = embed
        self.reranker = rerank
        self.retries = retries
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def enabled(self) -> bool:
        return self.primary is not None

    @property
    def can_embed(self) -> bool:
        return self.embedding is not None

    @property
    def can_rerank(self) -> bool:
        return self.reranker is not None

    @property
    def supports_tools(self) -> bool:
        return self.primary is not None and self.primary.supports_tools

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        fast: bool = False,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int = 800,
        tools: list[dict[str, Any]] | None = None,
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
            with_tools = bool(tools) and endpoint.supports_tools
            if with_tools:
                # 工具与 response_format 同时使用时部分供应商会报错：输出格式由提示词约束。
                body["tools"] = tools
            elif json_mode:
                body["response_format"] = {"type": "json_object"}
            try:
                data, latency = await self._post(endpoint, "/chat/completions", body)
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                content = message.get("content") or ""
                usage = data.get("usage") or {}
                prompt = int(usage.get("prompt_tokens") or 0)
                completion = int(usage.get("completion_tokens") or 0)
                calls = _tool_calls(message) if with_tools else ()
                return ChatResult(
                    content=content,
                    provider=endpoint.provider,
                    model=str(data.get("model") or model),
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    latency_ms=latency,
                    cost=endpoint.cost(prompt, completion),
                    tool_calls=calls,
                    message=_assistant(message, calls) if calls else None,
                )
            except LLMError as exc:
                logger.warning("chat via %s failed: %s", endpoint.provider, exc)
                errors.append(f"{endpoint.provider}: {exc}")
        raise LLMUnavailable("; ".join(errors))

    async def rerank(self, query: str, documents: list[str]) -> RerankResult:
        endpoint = self.reranker
        if endpoint is None:
            raise LLMUnavailable("没有配置重排序模型")
        body = {"model": endpoint.model, "query": query, "documents": documents}
        try:
            data, latency = await self._post(endpoint, "/rerank", body)
        except LLMError as exc:
            raise LLMUnavailable(str(exc)) from exc
        scores = [0.0] * len(documents)
        for item in data.get("results") or data.get("data") or []:
            index = item.get("index")
            if isinstance(index, int) and 0 <= index < len(documents):
                raw = item.get("relevance_score", item.get("score", 0.0))
                scores[index] = max(0.0, min(1.0, float(raw or 0.0)))
        return RerankResult(
            scores=scores,
            provider=urlsplit(endpoint.base_url).netloc or "llm",
            model=endpoint.model,
            latency_ms=latency,
        )

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
            cost=tokens * endpoint.price / 1000,
        )

    async def _post(
        self,
        endpoint: LLMEndpoint | EmbedEndpoint | RerankEndpoint,
        path: str,
        body: dict[str, Any],
    ) -> tuple[dict[str, Any], int]:
        url = endpoint.base_url.rstrip("/") + path
        headers = {"authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
        pinned = isinstance(endpoint, LLMEndpoint) and endpoint.pinned
        last = "unknown error"
        for attempt in range(self.retries + 1):
            if attempt:
                await asyncio.sleep(0.5 * 2 ** (attempt - 1))
            started = time.monotonic()
            try:
                target = await resolve_outbound(url, allow_private=not pinned)
                response = await self._http.post(
                    target.request_url,
                    json=body,
                    headers={**headers, **target.headers},
                    extensions=target.extensions,
                )
            except Unprocessable as exc:
                raise LLMError(f"接口地址不可用：{exc.message}") from exc
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


def _tool_calls(message: dict[str, Any]) -> tuple[ToolCall, ...]:
    calls = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = (raw or {}).get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False)
        calls.append(
            ToolCall(id=str(raw.get("id") or f"call_{index}"), name=name, arguments=arguments)
        )
    return tuple(calls)


def _assistant(message: dict[str, Any], calls: tuple[ToolCall, ...]) -> dict[str, Any]:
    """回传工具结果前，放回对话里的模型回复（只保留协议需要的字段）。"""
    return {
        "role": "assistant",
        "content": message.get("content") or None,
        "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in calls
        ],
    }
