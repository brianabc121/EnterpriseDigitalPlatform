"""大模型客户端：OpenAI 兼容协议、重试、备用供应商、向量批量与维度校验；中文词项与切片。"""

import json
from collections.abc import Callable

import httpx
import pytest

from app.integrations.llm import (
    EmbedEndpoint,
    LLMClient,
    LLMEndpoint,
    LLMUnavailable,
    RerankEndpoint,
)
from app.modules.kb.text import similarity, split_passages, terms
from tests.fake_llm import DIM, FakeLLM, embed_text

PRIMARY = LLMEndpoint(
    base_url="https://primary.example/v1", api_key="k1", chat_model="big", fast_model="small"
)
BACKUP = LLMEndpoint(
    base_url="https://backup.example/v1", api_key="k2", chat_model="other", name="backup"
)


def ok(content: str = "你好") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "big",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        },
    )


def client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: object) -> LLMClient:
    return LLMClient(PRIMARY, transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]


async def test_chat_sends_openai_compatible_requests() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok('{"reply": "您好"}')

    llm = client(handler)
    result = await llm.chat([{"role": "user", "content": "在吗"}], json_mode=True, fast=True)

    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://primary.example/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer k1"
    assert (body["model"], body["response_format"], body["stream"]) == (
        "small",
        {"type": "json_object"},
        False,
    )
    assert (result.content, result.provider, result.prompt_tokens, result.completion_tokens) == (
        '{"reply": "您好"}',
        "primary.example",
        12,
        3,
    )
    await llm.aclose()


async def test_retries_then_falls_back_to_the_backup_provider() -> None:
    calls: list[str] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "primary.example" and calls.count("primary.example") == 1:
            return httpx.Response(429, text="slow down")
        return ok()

    llm = client(flaky, retries=1)
    await llm.chat([{"role": "user", "content": "x"}])
    assert calls == ["primary.example", "primary.example"]

    calls.clear()

    def primary_down(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "primary.example":
            return httpx.Response(503, text="down")
        return ok("备用")

    llm = LLMClient(
        PRIMARY, fallback=BACKUP, retries=1, transport=httpx.MockTransport(primary_down)
    )
    result = await llm.chat([{"role": "user", "content": "x"}])
    assert (result.content, result.provider) == ("备用", "backup")
    assert calls == ["primary.example", "primary.example", "backup.example"]


async def test_non_retryable_errors_skip_retries_and_all_failures_raise() -> None:
    calls: list[str] = []

    def bad(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(400, text="model not found")

    llm = LLMClient(PRIMARY, fallback=BACKUP, retries=2, transport=httpx.MockTransport(bad))
    with pytest.raises(LLMUnavailable, match="model not found"):
        await llm.chat([{"role": "user", "content": "x"}])
    assert calls == ["primary.example", "backup.example"]

    unconfigured = LLMClient(None)
    assert not unconfigured.enabled
    with pytest.raises(LLMUnavailable):
        await unconfigured.chat([{"role": "user", "content": "x"}])


async def test_embeddings_are_batched_and_checked() -> None:
    fake = FakeLLM()
    endpoint = EmbedEndpoint(base_url="https://embed.example/v1", api_key="", model="m", dim=DIM)
    llm = LLMClient(None, embed=endpoint, transport=fake.transport())

    result = await llm.embed([f"问题 {i}" for i in range(23)])

    assert len(result.vectors) == 23
    assert [len(r["input"]) for r in fake.requests] == [10, 10, 3]
    assert "dimensions" not in fake.requests[0]
    wrong = LLMClient(
        None,
        embed=EmbedEndpoint(
            base_url="https://embed.example/v1",
            api_key="",
            model="m",
            dim=768,
            send_dimensions=True,
        ),
        transport=fake.transport(),
    )
    with pytest.raises(LLMUnavailable, match="维度"):
        await wrong.embed(["x"])
    assert fake.requests[-1]["dimensions"] == 768


@pytest.mark.parametrize(
    ("usage", "tokens"),
    [
        ({"usage": {"total_tokens": 120}}, 120),  # Jina、Voyage
        ({"meta": {"tokens": {"input_tokens": 80, "output_tokens": 0}}}, 80),  # Cohere 风格
        ({"tokens": {"input_tokens": 50}}, 50),
        ({}, 0),  # 没有返回用量
    ],
)
async def test_rerank_records_the_tokens_the_provider_reports(
    usage: dict[str, object], tokens: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "score": 0.2}]}
        return httpx.Response(200, json={**body, **usage})

    endpoint = RerankEndpoint(base_url="https://rerank.example/v1", api_key="", model="r", price=2)
    llm = LLMClient(None, rerank=endpoint, transport=httpx.MockTransport(handler))

    result = await llm.rerank("快递几天到", ["开发票", "快递三天到"])

    assert result.scores == [0.2, 0.9]
    assert (result.prompt_tokens, result.cost) == (tokens, tokens * 2 / 1000)


def test_chinese_terms_similarity_and_passages() -> None:
    assert terms("您好，请问订单多久能到？") == ["久能", "单多", "多久", "能到", "订单"]
    assert terms("iPhone 16 保修") == ["16", "iphone", "保修"]
    assert similarity("订单多久能到", "订单大概多久能到货") > 0.4
    assert similarity("怎么开发票", "订单多久能到") == 0

    near = sum(
        a * b for a, b in zip(embed_text("订单多久能到"), embed_text("订单多久能送到"), strict=True)
    )
    far = sum(
        a * b for a, b in zip(embed_text("订单多久能到"), embed_text("怎么开发票"), strict=True)
    )
    assert near > 0.5 > far

    text = "第一段。" * 10 + "\n\n" + "很长的第二段句子。" * 120
    passages = split_passages(text, size=200, overlap=20)
    assert all(len(p) <= 200 for p in passages)
    assert passages[0].startswith("第一段")
    assert len(passages) > 5
