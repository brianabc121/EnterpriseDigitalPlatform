"""知识的版本与回滚、必读确认、知识动态、员工评价、长期未命中、到期下线（设计文档 §12.5–§12.7）。"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.kb.service import expire_items
from tests.desk import Agent, Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def create(desk: Desk, **body: Any) -> dict[str, Any]:
    payload = {"title": "订单发货后多久能到？", "content": "一般 2 到 3 天送达。", "publish": True}
    response = await desk.client.post(
        "/api/v1/kb/items", headers=desk.admin, json={**payload, **body}
    )
    assert response.status_code == 201, response.text
    item: dict[str, Any] = response.json()
    return item


async def patch(desk: Desk, item: dict[str, Any], **body: Any) -> dict[str, Any]:
    response = await desk.client.patch(
        f"/api/v1/kb/items/{item['id']}", headers=desk.admin, json=body
    )
    assert response.status_code == 200, response.text
    updated: dict[str, Any] = response.json()
    return updated


async def versions(desk: Desk, item: dict[str, Any]) -> list[dict[str, Any]]:
    response = await desk.client.get(f"/api/v1/kb/items/{item['id']}/versions", headers=desk.admin)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def feed(desk: Desk, agent: Agent) -> dict[str, Any]:
    response = await desk.client.get("/api/v1/kb/feed", headers=agent.headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_every_publication_is_kept_and_can_be_restored(desk: Desk) -> None:
    item = await create(desk)
    await patch(desk, item, content="一般 1 到 2 天送达。")
    await patch(desk, item, content="一般 1 到 2 天送达。", category="物流")  # 内容没变
    await desk.client.post(f"/api/v1/kb/items/{item['id']}/archive", headers=desk.admin)
    await patch(desk, item, content="次日达。")  # 下线期间修改
    await desk.client.post(f"/api/v1/kb/items/{item['id']}/publish", headers=desk.admin)

    history = await versions(desk, item)
    assert [(v["version"], v["change"], v["content"]) for v in history] == [
        (3, "updated", "次日达。"),
        (2, "updated", "一般 1 到 2 天送达。"),
        (1, "created", "一般 2 到 3 天送达。"),
    ]
    assert history[0]["published_by_name"] == "管理员"

    restored = await desk.client.post(
        f"/api/v1/kb/items/{item['id']}/versions/1/restore", headers=desk.admin
    )
    assert restored.status_code == 200, restored.text
    assert (restored.json()["version"], restored.json()["content"]) == (4, "一般 2 到 3 天送达。")
    [latest, *_] = await versions(desk, item)
    assert (latest["change"], latest["note"]) == ("restored", "恢复到 v1")
    hits = await desk.client.get(
        "/api/v1/kb/search", headers=desk.admin, params={"q": "订单发货后多久能到"}
    )
    assert hits.json()["items"][0]["text"] == "一般 2 到 3 天送达。"
    missing = await desk.client.post(
        f"/api/v1/kb/items/{item['id']}/versions/9/restore", headers=desk.admin
    )
    assert missing.status_code == 404


async def test_must_read_knowledge_is_confirmed_per_version(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    await desk.agent("bob", online=False)
    policy = await create(
        desk, title="十一假期发货安排", content="10 月 1 日至 3 日暂停发货。", must_read=True
    )
    await create(desk, title="普通问答", content="普通答案。")

    pending = await feed(desk, alice)
    assert [e["title"] for e in pending["must_read"]] == ["十一假期发货安排"]
    assert [e["title"] for e in pending["events"]] == ["普通问答", "十一假期发货安排"]

    confirmed = await desk.client.post(
        f"/api/v1/kb/items/{policy['id']}/read", headers=alice.headers
    )
    assert confirmed.status_code == 204
    after = await feed(desk, alice)
    assert after["must_read"] == []
    assert all(e["read"] for e in after["events"])
    stats = await desk.client.get(f"/api/v1/kb/items/{policy['id']}/reads", headers=desk.admin)
    body = stats.json()
    # 需要确认的是有接待权限的员工：管理员、alice、bob。
    assert (body["version"], body["total"], body["confirmed"]) == (1, 3, 1)
    first = body["readers"][0]
    assert (first["display_name"], first["read_at"] is not None) == ("Alice", True)

    # 更新后需要重新确认。
    await patch(desk, policy, content="10 月 1 日至 5 日暂停发货。")
    again = await feed(desk, alice)
    assert [(e["title"], e["version"], e["change"]) for e in again["must_read"]] == [
        ("十一假期发货安排", 2, "updated")
    ]
    denied = await desk.client.get(f"/api/v1/kb/items/{policy['id']}/reads", headers=alice.headers)
    assert denied.status_code == 403


async def test_feed_only_shows_knowledge_the_agent_can_use(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    await create(desk, title="内部报价规则", content="仅限内部。", visibility="admin")
    archived = await create(desk, title="已下线的知识", content="旧内容。")
    await desk.client.post(f"/api/v1/kb/items/{archived['id']}/archive", headers=desk.admin)
    await create(desk, title="坐席话术", content="您好。", visibility="agent")

    body = await feed(desk, alice)

    assert [e["title"] for e in body["events"]] == ["坐席话术"]


async def test_staff_rate_knowledge_once_each(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)
    item = await create(desk)

    async def rate(agent: Agent, value: int) -> dict[str, Any]:
        response = await desk.client.post(
            f"/api/v1/kb/items/{item['id']}/feedback", headers=agent.headers, json={"value": value}
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    await rate(alice, 1)
    await rate(bob, -1)
    assert await rate(alice, -1) == {"likes": 0, "dislikes": 2, "mine": -1}
    assert await rate(alice, 0) == {"likes": 0, "dislikes": 1, "mine": 0}
    listed = await desk.client.get("/api/v1/kb/items", headers=desk.admin)
    assert (listed.json()["items"][0]["likes"], listed.json()["items"][0]["dislikes"]) == (0, 1)


async def test_stale_filter_lists_knowledge_nobody_uses(desk: Desk) -> None:
    old = await create(desk, title="冷门问题", content="很少有人问。")
    used = await create(desk, title="热门问题", content="经常被问。")
    await create(desk, title="新问题", content="刚发布。")
    long_ago = datetime.now(UTC) - timedelta(days=120)
    await desk.sql(
        "UPDATE kb_items SET published_at = $1 WHERE id = ANY($2::uuid[])",
        long_ago,
        [uuid.UUID(old["id"]), uuid.UUID(used["id"])],
    )
    await desk.sql("UPDATE kb_items SET last_hit_at = now() WHERE id = $1", uuid.UUID(used["id"]))

    stale = await desk.client.get("/api/v1/kb/items", headers=desk.admin, params={"stale": True})

    assert [i["title"] for i in stale.json()["items"]] == ["冷门问题"]


async def test_expired_knowledge_is_archived_automatically(desk: Desk) -> None:
    promo = await create(desk, title="国庆满减活动", content="满 300 减 50。")
    await desk.sql(
        "UPDATE kb_items SET valid_to = now() - interval '1 minute' WHERE id = $1",
        uuid.UUID(promo["id"]),
    )

    archived = await expire_items(desk.ctx)

    assert archived == 1
    [row] = await desk.sql(
        "SELECT status, archived_at FROM kb_items WHERE id = $1", uuid.UUID(promo["id"])
    )
    assert row["status"] == "archived" and row["archived_at"] is not None
    assert (
        await desk.sql("SELECT 1 FROM kb_chunks WHERE item_id = $1", uuid.UUID(promo["id"])) == []
    )
    [audit] = await desk.sql("SELECT actor_type FROM audit_logs WHERE action = 'kb_item.expire'")
    assert audit["actor_type"] == "system"
    assert await expire_items(desk.ctx) == 0
