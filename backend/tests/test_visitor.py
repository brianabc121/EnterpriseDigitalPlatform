import uuid
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI

from app.core.ratelimit import VISITOR_INIT_PER_IP
from tests.factories import bearer, login, provision
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls


async def channel_key(client: httpx.AsyncClient, tenant_code: str) -> str:
    token = await login(client, tenant_code)
    response = await client.get("/api/v1/channels", headers=bearer(token))
    key: str = response.json()["items"][0]["public_key"]
    return key


async def init(client: httpx.AsyncClient, key: str, **extra: Any) -> httpx.Response:
    return await client.post("/api/v1/visitor/init", json={"channel_key": key, **extra})


async def fetch(database_urls: DatabaseUrls, query: str, *args: Any) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(database_urls.platform_dsn)
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


async def test_new_visitor_gets_a_customer_room_and_im_login(
    app: FastAPI, client: httpx.AsyncClient, fake_im: FakeOpenIM, database_urls: DatabaseUrls
) -> None:
    await provision(app, "acme")
    key = await channel_key(client, "acme")

    response = await init(client, key, page_url="https://acme.example/pricing")

    assert response.status_code == 200, response.text
    body = response.json()
    im = body["im"]
    room_id = uuid.UUID(body["room_id"])
    assert im["group_id"] == f"acme_r_{room_id.hex}"
    assert im["conversation_id"] == f"sg_acme_r_{room_id.hex}"
    assert im["user_id"].startswith("acme_c_")
    assert (im["api_url"], im["ws_url"], im["platform_id"]) == (
        "http://localhost:10002",
        "ws://localhost:10001",
        5,
    )
    assert im["token"] in fake_im.tokens
    assert body["visitor_token"]

    group = fake_im.groups[im["group_id"]]
    assert group.owner == "acme_sys"
    assert group.members == {"acme_sys", "acme_bot", im["user_id"]}
    assert group.name == "ACME"

    [row] = await fetch(
        database_urls,
        "SELECT c.display_name, c.source_channel, c.owner_id, i.im_user_id, i.im_registered_at, "
        "i.profile, r.im_ready_at FROM customers c "
        "JOIN customer_identities i ON i.customer_id = c.id JOIN rooms r ON r.identity_id = i.id",
    )
    assert row["display_name"].startswith("访客 ")
    assert (row["source_channel"], row["owner_id"]) == ("web", None)
    assert row["im_user_id"] == im["user_id"]
    assert row["im_registered_at"] is not None
    assert row["im_ready_at"] is not None
    assert '"first_page": "https://acme.example/pricing"' in row["profile"]


async def test_returning_visitor_keeps_the_same_identity_and_room(
    app: FastAPI, client: httpx.AsyncClient, fake_im: FakeOpenIM, database_urls: DatabaseUrls
) -> None:
    await provision(app, "acme")
    key = await channel_key(client, "acme")
    first = (await init(client, key)).json()
    calls_before = list(fake_im.calls)

    second = (await init(client, key, visitor_token=first["visitor_token"])).json()

    assert second["room_id"] == first["room_id"]
    assert second["im"]["user_id"] == first["im"]["user_id"]
    # 已开通的身份和服务群不再重复开通，只换新的 IM token。
    new_calls = fake_im.calls[len(calls_before) :]
    assert "/group/create_group" not in new_calls
    assert "/user/user_register" not in new_calls
    assert "/auth/get_user_token" in new_calls
    assert len(await fetch(database_urls, "SELECT 1 FROM customers")) == 1


async def test_invalid_or_foreign_visitor_token_starts_a_new_visitor(
    app: FastAPI, client: httpx.AsyncClient, database_urls: DatabaseUrls
) -> None:
    await provision(app, "acme")
    await provision(app, "globex")
    acme_key = await channel_key(client, "acme")
    globex_key = await channel_key(client, "globex")
    globex_visitor = (await init(client, globex_key)).json()

    garbage = await init(client, acme_key, visitor_token="not-a-token")
    foreign = await init(client, acme_key, visitor_token=globex_visitor["visitor_token"])

    assert garbage.status_code == foreign.status_code == 200
    assert garbage.json()["room_id"] != foreign.json()["room_id"]
    assert foreign.json()["room_id"] != globex_visitor["room_id"]
    rows = await fetch(
        database_urls,
        "SELECT t.code, count(*) AS n FROM customers c JOIN tenants t ON t.id = c.tenant_id "
        "GROUP BY t.code ORDER BY t.code",
    )
    assert [(r["code"], r["n"]) for r in rows] == [("acme", 2), ("globex", 1)]


async def test_unknown_channel_is_not_found(app: FastAPI, client: httpx.AsyncClient) -> None:
    await provision(app, "acme")
    key = await channel_key(client, "acme")

    no_dot = "acme-without-a-dot"
    for bad in ("acme.00000000000000000000000000000000", "nobody." + key.split(".")[1], no_dot):
        response = await init(client, bad)
        assert response.status_code == 404, bad
        assert response.json()["error"]["code"] == "not_found"


async def test_disabled_channel_or_suspended_tenant_is_refused(
    app: FastAPI, client: httpx.AsyncClient, database_urls: DatabaseUrls
) -> None:
    await provision(app, "acme")
    key = await channel_key(client, "acme")

    await fetch(database_urls, "UPDATE channel_accounts SET status = 'disabled'")
    assert (await init(client, key)).status_code == 403

    await fetch(database_urls, "UPDATE channel_accounts SET status = 'active'")
    await fetch(database_urls, "UPDATE tenants SET status = 'suspended'")
    assert (await init(client, key)).status_code == 403


async def test_openim_outage_returns_503_without_leaving_orphans(
    app: FastAPI, client: httpx.AsyncClient, fake_im: FakeOpenIM, database_urls: DatabaseUrls
) -> None:
    await provision(app, "acme")
    key = await channel_key(client, "acme")
    fake_im.down = True

    response = await init(client, key)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert await fetch(database_urls, "SELECT 1 FROM customers") == []

    fake_im.down = False
    assert (await init(client, key)).status_code == 200
    assert len(await fetch(database_urls, "SELECT 1 FROM customers")) == 1


async def test_visitor_init_is_rate_limited_per_ip(app: FastAPI, client: httpx.AsyncClient) -> None:
    await provision(app, "acme")
    key = await channel_key(client, "acme")
    first = (await init(client, key)).json()

    statuses = [
        (await init(client, key, visitor_token=first["visitor_token"])).status_code
        for _ in range(VISITOR_INIT_PER_IP.limit)
    ]

    assert statuses[:-1] == [200] * (VISITOR_INIT_PER_IP.limit - 1)
    assert statuses[-1] == 429
    limited = await init(client, key)
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) >= 1
