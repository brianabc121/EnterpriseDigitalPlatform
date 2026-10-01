"""外部地址的检查与固定（core/urls.py）：解析一次就连这个地址，防止域名重绑定到内网。"""

import httpx
import pytest

from app.core import urls
from app.core.errors import Unprocessable
from app.core.urls import resolve_outbound
from app.integrations.llm import LLMClient, LLMEndpoint, LLMError


def resolver(*addresses: str) -> object:
    async def fake(host: str, port: int) -> list[str]:
        return list(addresses)

    return fake


async def test_dev_and_test_environments_do_not_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urls, "_resolve", resolver("10.0.0.8"))

    target = await resolve_outbound("http://10.0.0.8:8900/v1/", allow_private=True)

    assert (target.request_url, target.headers, target.extensions) == (
        "http://10.0.0.8:8900/v1/",
        {},
        {},
    )


async def test_public_https_hosts_are_pinned_to_the_resolved_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urls, "_resolve", resolver("93.184.216.34", "93.184.216.35"))

    target = await resolve_outbound("https://hooks.example.com:8443/in?x=1", allow_private=False)

    assert target.url == "https://hooks.example.com:8443/in?x=1"
    assert target.request_url == "https://93.184.216.34:8443/in?x=1"
    assert target.headers == {"Host": "hooks.example.com:8443"}
    assert target.extensions == {"sni_hostname": "hooks.example.com"}

    monkeypatch.setattr(urls, "_resolve", resolver("2606:2800:220:1:248:1893:25c8:1946"))
    v6 = await resolve_outbound("https://hooks.example.com/in", allow_private=False)
    assert v6.request_url == "https://[2606:2800:220:1:248:1893:25c8:1946]/in"
    assert v6.headers == {"Host": "hooks.example.com"}


async def test_private_or_plain_http_addresses_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urls, "_resolve", resolver("93.184.216.34", "192.168.1.9"))
    with pytest.raises(Unprocessable, match="内网"):
        await resolve_outbound("https://hooks.example.com/in", allow_private=False)
    with pytest.raises(Unprocessable, match="https"):
        await resolve_outbound("http://hooks.example.com/in", allow_private=False)
    with pytest.raises(Unprocessable, match="内网"):
        await resolve_outbound("https://db.internal/in", allow_private=False)
    with pytest.raises(Unprocessable, match="格式"):
        await resolve_outbound("ftp://hooks.example.com/in", allow_private=False)


async def test_httpx_connects_to_the_pinned_address_with_the_original_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urls, "_resolve", resolver("93.184.216.34"))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    target = await resolve_outbound("https://hooks.example.com/in", allow_private=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await client.post(
            target.request_url,
            headers={**target.headers, "X-Test": "1"},
            extensions=target.extensions,
            content=b"{}",
        )

    assert response.status_code == 204
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "hooks.example.com"
    assert seen[0].extensions["sni_hostname"] == "hooks.example.com"


async def test_tenant_llm_endpoints_are_pinned_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urls, "_resolve", resolver("93.184.216.34"))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "big",
                "choices": [{"message": {"role": "assistant", "content": "你好"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    endpoint = LLMEndpoint(
        base_url="https://llm.example.com/v1", api_key="k", chat_model="big", pinned=True
    )
    llm = LLMClient(endpoint, transport=httpx.MockTransport(handler), retries=0)
    await llm.chat([{"role": "user", "content": "在吗"}])
    assert str(seen[0].url) == "https://93.184.216.34/v1/chat/completions"
    assert seen[0].headers["host"] == "llm.example.com"
    assert seen[0].headers["authorization"] == "Bearer k"

    # 域名改成解析到内网地址：这次调用被拒绝，不会连接。
    monkeypatch.setattr(urls, "_resolve", resolver("10.1.2.3"))
    with pytest.raises(LLMError, match="内网"):
        await llm.chat([{"role": "user", "content": "在吗"}])
    assert len(seen) == 1
    await llm.aclose()
