import uuid
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI

from app.core.config import Settings
from tests.factories import STAFF_PASSWORD, bearer, create_staff, login, provision
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_openim_hooks import deliver, new_visitor


async def chat(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    texts: list[str],
) -> Any:
    """新访客发送几条消息，回调全部投递。"""
    visitor = await new_visitor(app, client)
    for text in texts:
        fake_im.send_as(visitor["im"]["user_id"], visitor["im"]["group_id"], text)
    await deliver(client, settings, fake_im.callbacks)
    return visitor


async def test_admin_sees_rooms_and_pages_through_messages(
    app: FastAPI, client: httpx.AsyncClient, fake_im: FakeOpenIM, settings: Settings
) -> None:
    visitor = await chat(app, client, fake_im, settings, ["一", "二", "三"])
    admin = bearer(await login(client, "acme"))

    rooms = (await client.get("/api/v1/rooms", headers=admin)).json()
    assert rooms["total"] == 1
    [room] = rooms["items"]
    assert room["id"] == visitor["room_id"]
    assert room["customer_display_name"].startswith("访客 ")
    assert room["last_message_at"] is not None

    url = f"/api/v1/rooms/{room['id']}/messages"
    first = (await client.get(url, headers=admin, params={"limit": 2})).json()
    assert [m["text_plain"] for m in first["items"]] == ["三", "二"]
    assert first["has_more"] is True
    assert first["items"][0]["direction"] == "in"
    assert first["items"][0]["content"] == {"text": "三"}

    before = first["items"][-1]["id"]
    second = (await client.get(url, headers=admin, params={"limit": 2, "before": before})).json()
    assert [m["text_plain"] for m in second["items"]] == ["一"]
    assert second["has_more"] is False

    missing = await client.get(url, headers=admin, params={"before": str(uuid.uuid4())})
    assert missing.status_code == 404


async def test_agents_only_see_rooms_of_their_own_customers(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> None:
    visitor = await chat(app, client, fake_im, settings, ["你好"])
    admin = await login(client, "acme")
    alice_id = await create_staff(client, admin, "alice")
    alice = bearer(await login(client, "acme", "alice", STAFF_PASSWORD))
    url = f"/api/v1/rooms/{visitor['room_id']}/messages"

    # 访客没有归属坐席，坐席看不到。
    assert (await client.get("/api/v1/rooms", headers=alice)).json()["total"] == 0
    assert (await client.get(url, headers=alice)).status_code == 404

    conn = await asyncpg.connect(database_urls.platform_dsn)
    try:
        await conn.execute("UPDATE customers SET owner_id = $1", uuid.UUID(alice_id))
    finally:
        await conn.close()

    assert (await client.get("/api/v1/rooms", headers=alice)).json()["total"] == 1
    messages = (await client.get(url, headers=alice)).json()["items"]
    assert [m["text_plain"] for m in messages] == ["你好"]


async def test_rooms_are_invisible_to_other_tenants(
    app: FastAPI, client: httpx.AsyncClient, fake_im: FakeOpenIM, settings: Settings
) -> None:
    visitor = await chat(app, client, fake_im, settings, ["你好"])
    await provision(app, "globex")
    other = bearer(await login(client, "globex"))

    assert (await client.get("/api/v1/rooms", headers=other)).json()["total"] == 0
    response = await client.get(f"/api/v1/rooms/{visitor['room_id']}/messages", headers=other)
    assert response.status_code == 404


async def test_rooms_can_be_filtered_by_customer(
    app: FastAPI, client: httpx.AsyncClient, fake_im: FakeOpenIM, settings: Settings
) -> None:
    await chat(app, client, fake_im, settings, ["你好"])
    admin = bearer(await login(client, "acme"))
    [room] = (await client.get("/api/v1/rooms", headers=admin)).json()["items"]

    mine = await client.get(
        "/api/v1/rooms", headers=admin, params={"customer_id": room["customer_id"]}
    )
    other = await client.get(
        "/api/v1/rooms", headers=admin, params={"customer_id": str(uuid.uuid4())}
    )

    assert mine.json()["total"] == 1
    assert other.json()["total"] == 0
