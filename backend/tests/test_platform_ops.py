"""运营后台（G2）：二次验证、全局敏感词、大模型供应商与路由、租户自带密钥、系统健康、审计、渠道。"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core import totp
from app.core.config import Settings
from app.core.errors import Unprocessable
from app.core.urls import check_outbound_url
from app.integrations.openim import OpenIMClient
from app.main import create_app
from app.modules.ai.models import LlmCall
from tests.conftest import fake_llm_client
from tests.desk import Desk
from tests.factories import (
    PLATFORM_PASSWORD,
    bearer,
    create_platform_admin,
    login,
    platform_login,
    provision,
)
from tests.fake_llm import FakeLLM
from tests.fake_openim import SECRET as FAKE_OPENIM_SECRET
from tests.fake_openim import FakeOpenIM
from tests.fake_storage import FakeStorage
from tests.support import DatabaseUrls
from tests.test_agent_messages import send, serving
from tests.test_ai_reception import enable_ai

PROVIDERS = "/platform/v1/llm-providers"
FAKE_URL = "http://fake-llm/v1"


async def _ops(app: FastAPI, client: httpx.AsyncClient) -> dict[str, str]:
    await create_platform_admin(app)
    return bearer(await platform_login(client))


def _code(secret: str, offset: int = 0) -> str:
    return totp.code_at(secret, totp.counter_at() + offset)


# ---- 二次验证 ----


async def test_totp_setup_and_login(app: FastAPI, client: httpx.AsyncClient) -> None:
    ops = await _ops(app, client)
    me = (await client.get("/platform/v1/me", headers=ops)).json()
    assert (me["mfa_enabled"], me["mfa_required"]) == (False, False)

    setup = (await client.post("/platform/v1/auth/mfa/setup", headers=ops)).json()
    secret = setup["secret"]
    assert (
        setup["otpauth_uri"].startswith("otpauth://totp/")
        and f"secret={secret}" in setup["otpauth_uri"]
    )
    wrong = await client.post("/platform/v1/auth/mfa/enable", headers=ops, json={"code": "000000"})
    assert wrong.status_code == 422
    enabled = await client.post(
        "/platform/v1/auth/mfa/enable", headers=ops, json={"code": _code(secret)}
    )
    assert enabled.status_code == 204

    body = {"username": "ops", "password": PLATFORM_PASSWORD}
    need = await client.post("/platform/v1/auth/login", json=body)
    assert (need.status_code, need.json()["error"]["code"]) == (401, "mfa_required")
    bad = await client.post("/platform/v1/auth/login", json={**body, "otp": "123456"})
    assert (bad.status_code, bad.json()["error"]["message"]) == (401, "验证码错误")
    # 启用时用过的验证码不能再用来登录；下一个时间步的可以。
    replay = await client.post("/platform/v1/auth/login", json={**body, "otp": _code(secret)})
    assert replay.status_code == 401
    ok = await client.post("/platform/v1/auth/login", json={**body, "otp": _code(secret, 1)})
    assert ok.status_code == 200, ok.text
    token = bearer(ok.json()["access_token"])
    assert (await client.get("/platform/v1/me", headers=token)).json()["mfa_enabled"] is True

    refused = await client.post(
        "/platform/v1/auth/mfa/disable",
        headers=token,
        json={"password": "wrong-pass", "code": _code(secret)},
    )
    assert refused.status_code == 422
    disabled = await client.post(
        "/platform/v1/auth/mfa/disable",
        headers=token,
        json={"password": PLATFORM_PASSWORD, "code": _code(secret)},
    )
    assert disabled.status_code == 204
    plain = await client.post("/platform/v1/auth/login", json=body)
    assert plain.status_code == 200


@pytest.fixture
async def mfa_client(
    settings: Settings, fake_im: FakeOpenIM, fake_llm: FakeLLM, fake_storage: FakeStorage
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    im = OpenIMClient(
        settings.openim_api_url, secret=FAKE_OPENIM_SECRET, transport=fake_im.transport()
    )
    application = create_app(
        settings.model_copy(update={"platform_mfa_required": True}),
        im=im,
        llm=fake_llm_client(fake_llm),
        storage_transport=fake_storage.transport(),
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield application, c
    await application.state.ctx.aclose()


async def test_enforced_mfa_limits_new_accounts_to_setup(
    mfa_client: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    app, client = mfa_client
    await create_platform_admin(app)
    login_response = await client.post(
        "/platform/v1/auth/login", json={"username": "ops", "password": PLATFORM_PASSWORD}
    )
    assert login_response.json()["mfa_setup_required"] is True
    token = bearer(login_response.json()["access_token"])

    blocked = await client.get("/platform/v1/tenants", headers=token)
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (403, "mfa_setup_required")
    assert (await client.get("/platform/v1/me", headers=token)).json()["mfa_required"] is True
    secret = (await client.post("/platform/v1/auth/mfa/setup", headers=token)).json()["secret"]
    await client.post("/platform/v1/auth/mfa/enable", headers=token, json={"code": _code(secret)})

    assert (await client.get("/platform/v1/tenants", headers=token)).status_code == 200


def test_totp_matches_rfc_6238_vectors() -> None:
    import base64

    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.code_at(secret, 1111111109 // 30) == "081804"
    assert totp.match(secret, "081804", now=1111111109 + 30) == 1111111109 // 30
    assert totp.match(secret, "081804", now=1111111109 + 90) is None


# ---- 全局敏感词 ----


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def test_platform_words_block_agents_and_hand_ai_over(
    desk: Desk, app: FastAPI, client: httpx.AsyncClient
) -> None:
    ops = await _ops(app, client)
    policy = await client.put(
        "/platform/v1/settings/content-policy",
        headers=ops,
        json={"words": ["刷单", " 刷单 ", "赌博"], "apply_to_ai": True},
    )
    assert policy.json() == {
        "words": ["刷单", "赌博"],
        "apply_to_ai": True,
        "block_agent_messages": True,
    }
    alice, _, session_id = await serving(desk)

    blocked = await send(desk, alice, session_id, "可以帮你刷单")
    assert (blocked.status_code, blocked.json()["error"]["message"]) == (
        422,
        "消息包含平台禁止发送的内容：刷单",
    )
    assert (await send(desk, alice, session_id, "您好")).status_code == 200

    await enable_ai(desk)
    visitor = await desk.visitor()
    await desk.say(visitor, "你们这里能赌博吗")
    chat = await desk.session_of(visitor)
    assert chat["handoff_reason"] == "sensitive"


# ---- 大模型供应商 ----


def _provider(name: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "base_url": FAKE_URL,
        "api_key": f"sk-{name}-secret-1234",
        "chat_model": f"{name}-chat",
        **extra,
    }


async def _ai_test(desk: Desk) -> None:
    response = await desk.client.post(
        "/api/v1/ai/test", headers=desk.admin, json={"question": "快递几天能到"}
    )
    assert response.status_code == 200, response.text


async def _last_provider(app: FastAPI) -> str:
    async with app.state.db.platform_sessionmaker() as session:
        call = await session.scalar(select(LlmCall).order_by(LlmCall.created_at.desc()).limit(1))
    assert call is not None
    return call.provider


async def test_provider_registry_routes_tenants(
    desk: Desk, app: FastAPI, client: httpx.AsyncClient, fake_llm: FakeLLM
) -> None:
    await enable_ai(desk)
    ops = await _ops(app, client)
    bad_dim = await client.post(
        PROVIDERS, headers=ops, json=_provider("x", embed_model="e", embed_dim=768)
    )
    assert bad_dim.status_code == 422

    main = await client.post(
        PROVIDERS,
        headers=ops,
        json=_provider("main", is_default=True, embed_model="fake-embed", embed_dim=1024),
    )
    assert main.status_code == 201, main.text
    assert (main.json()["api_key_set"], main.json()["api_key_hint"]) == (True, "1234")
    assert "sk-main" not in main.text
    tested = (await client.post(f"{PROVIDERS}/{main.json()['id']}/test", headers=ops)).json()
    assert tested["chat"]["ok"] and tested["embed"]["ok"]

    # 默认供应商取代环境变量里的配置。
    await _ai_test(desk)
    assert await _last_provider(app) == "main"
    assert fake_llm.requests[-1]["model"] == "main-chat"

    vip = (await client.post(PROVIDERS, headers=ops, json=_provider("vip"))).json()
    assigned = await client.put(
        f"/platform/v1/tenants/{desk.tenant_id}/llm", headers=ops, json={"provider_id": vip["id"]}
    )
    assert assigned.json() == {
        "provider_id": vip["id"],
        "source": "provider",
        "provider_name": "vip",
    }
    await _ai_test(desk)
    assert await _last_provider(app) == "vip"
    listed = (await client.get(PROVIDERS, headers=ops)).json()["items"]
    assert {p["name"]: p["tenants"] for p in listed} == {"main": 0, "vip": 1}

    # 按场景路由：没有单独指定供应商的租户，知识提炼用便宜的模型。
    cheap = (await client.post(PROVIDERS, headers=ops, json=_provider("cheap"))).json()
    routes = await client.put(
        "/platform/v1/settings/llm-routes", headers=ops, json={"routes": {"test": cheap["id"]}}
    )
    assert routes.json()["routes"] == {"test": cheap["id"]}
    unknown = await client.put(
        "/platform/v1/settings/llm-routes", headers=ops, json={"routes": {"nope": cheap["id"]}}
    )
    assert unknown.status_code == 422
    await client.put(
        f"/platform/v1/tenants/{desk.tenant_id}/llm", headers=ops, json={"provider_id": None}
    )
    await _ai_test(desk)
    assert await _last_provider(app) == "cheap"

    # 删除供应商后路由里去掉它，改用默认供应商。
    assert (await client.delete(f"{PROVIDERS}/{cheap['id']}", headers=ops)).status_code == 204
    assert (await client.get("/platform/v1/settings/llm-routes", headers=ops)).json()[
        "routes"
    ] == {}
    await _ai_test(desk)
    assert await _last_provider(app) == "main"

    # 修改：换默认、换密钥。
    patched = await client.patch(
        f"{PROVIDERS}/{vip['id']}", headers=ops, json={"is_default": True, "api_key": "sk-new-9876"}
    )
    assert (patched.json()["is_default"], patched.json()["api_key_hint"]) == (True, "9876")
    names = {
        p["name"]: p["is_default"]
        for p in (await client.get(PROVIDERS, headers=ops)).json()["items"]
    }
    assert names == {"main": False, "vip": True}


async def test_tenant_can_bring_its_own_key(
    desk: Desk, app: FastAPI, client: httpx.AsyncClient, fake_llm: FakeLLM
) -> None:
    await enable_ai(desk)
    before = (await client.get("/api/v1/ai/llm", headers=desk.admin)).json()
    assert (before["source"], before["own"]) == ("env", None)

    saved = await client.put(
        "/api/v1/ai/llm",
        headers=desk.admin,
        json={"base_url": FAKE_URL + "/", "api_key": "sk-own", "chat_model": "own-chat"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["source"] == "tenant"
    assert saved.json()["own"] == {
        "base_url": FAKE_URL,
        "chat_model": "own-chat",
        "fast_model": "",
        "enabled": True,
        "api_key_set": True,
    }
    assert (await client.post("/api/v1/ai/llm/test", headers=desk.admin)).json()["chat"]["ok"]
    await _ai_test(desk)
    assert await _last_provider(app) == "tenant"
    assert fake_llm.requests[-1]["model"] == "own-chat"

    removed = await client.delete("/api/v1/ai/llm", headers=desk.admin)
    assert (removed.json()["source"], removed.json()["own"]) == ("env", None)
    ops = await _ops(app, client)
    assert (await client.get(f"/platform/v1/tenants/{desk.tenant_id}/llm", headers=ops)).json()[
        "source"
    ] == "env"


async def test_outbound_urls_must_be_public_in_prod() -> None:
    for url in ("http://api.example.com/v1", "https://localhost/v1", "https://127.0.0.1/v1"):
        with pytest.raises(Unprocessable):
            await check_outbound_url(url, allow_private=False)
    assert await check_outbound_url("http://127.0.0.1:8900/v1/", allow_private=True) == (
        "http://127.0.0.1:8900/v1"
    )


# ---- 系统健康、审计、渠道 ----


async def test_health_reports_components(app: FastAPI, client: httpx.AsyncClient) -> None:
    ops = await _ops(app, client)
    await provision(app, "acme")

    report = (await client.get("/platform/v1/health", headers=ops)).json()

    status = {c["key"]: c["status"] for c in report["components"]}
    assert status == {
        "database": "ok",
        "redis": "ok",
        "openim": "ok",
        "storage": "ok",
        "llm": "ok",
        "outbox": "ok",
        "wecom": "disabled",
        "clamav": "disabled",
        # 测试里没有运行实时消费进程和调度进程。
        "worker": "down",
        "scheduler": "down",
    }
    assert report["status"] == "degraded"
    assert report["metrics"]["active_tenants"] == 1


async def test_audit_log_viewer_filters_and_pages(app: FastAPI, client: httpx.AsyncClient) -> None:
    ops = await _ops(app, client)
    acme = await provision(app, "acme")
    await provision(app, "beta")
    await login(client, "acme")
    await client.patch(f"/platform/v1/tenants/{acme}", headers=ops, json={"name": "ACME 新"})

    everything = (await client.get("/platform/v1/audit-logs", headers=ops)).json()["items"]
    assert {"tenant.provision", "tenant.update", "auth.login"} <= {a["action"] for a in everything}
    only = (
        await client.get(
            "/platform/v1/audit-logs",
            headers=ops,
            params={"tenant_id": str(acme), "action": "tenant"},
        )
    ).json()["items"]
    assert [a["action"] for a in only] == ["tenant.update", "tenant.provision"]
    assert only[0]["actor_name"] == "运营" and only[0]["tenant_code"] == "acme"
    staff = (
        await client.get("/platform/v1/audit-logs", headers=ops, params={"actor_type": "staff"})
    ).json()["items"]
    assert [(a["action"], a["actor_name"]) for a in staff] == [("auth.login", "管理员")]

    first = (await client.get("/platform/v1/audit-logs", headers=ops, params={"limit": 1})).json()
    assert len(first["items"]) == 1 and first["next_before"] is not None
    second = (
        await client.get(
            "/platform/v1/audit-logs",
            headers=ops,
            params={"limit": 1, "before": first["next_before"]},
        )
    ).json()
    assert second["items"][0]["id"] != first["items"][0]["id"]


async def test_channel_overview_lists_tenants(app: FastAPI, client: httpx.AsyncClient) -> None:
    ops = await _ops(app, client)
    await provision(app, "acme")

    overview = (await client.get("/platform/v1/channels", headers=ops)).json()

    assert overview["wecom_configured"] is False
    [tenant] = overview["items"]
    assert (tenant["code"], tenant["channels"], tenant["wecom"]) == ("acme", {"web": 1}, None)


async def test_ops_endpoints_require_platform_login(client: httpx.AsyncClient) -> None:
    for path in (
        "/platform/v1/health",
        "/platform/v1/audit-logs",
        "/platform/v1/channels",
        PROVIDERS,
        "/platform/v1/settings/content-policy",
        "/platform/v1/settings/llm-routes",
        f"/platform/v1/tenants/{uuid.uuid4()}/llm",
    ):
        assert (await client.get(path)).status_code == 401, path


async def test_replay_record_belongs_to_one_secret(app: FastAPI, client: httpx.AsyncClient) -> None:
    ops = await _ops(app, client)
    body = {"username": "ops", "password": PLATFORM_PASSWORD}
    first = (await client.post("/platform/v1/auth/mfa/setup", headers=ops)).json()["secret"]
    await client.post("/platform/v1/auth/mfa/enable", headers=ops, json={"code": _code(first)})
    login = await client.post("/platform/v1/auth/login", json={**body, "otp": _code(first, 1)})
    assert login.status_code == 200
    await client.post(
        "/platform/v1/auth/mfa/disable",
        headers=ops,
        json={"password": PLATFORM_PASSWORD, "code": _code(first)},
    )

    # 换了新密钥后，旧密钥用过的时间步不影响新密钥。
    second = (await client.post("/platform/v1/auth/mfa/setup", headers=ops)).json()["secret"]
    await client.post("/platform/v1/auth/mfa/enable", headers=ops, json={"code": _code(second)})
    again = await client.post("/platform/v1/auth/login", json={**body, "otp": _code(second, 1)})
    assert again.status_code == 200, again.text
