"""知识空间与分类树、负责人与到期提醒、按技能组推送、站内信（设计文档 §12.1、§12.5、§12.6）。"""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.kb.reminders import remind_expiring
from tests.desk import Agent, Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_kb_versions import create, feed, patch


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def _post(desk: Desk, path: str, body: dict[str, Any], expect: int = 201) -> Any:
    response = await desk.client.post(path, headers=desk.admin, json=body)
    assert response.status_code == expect, response.text
    return response.json()


async def _spaces(desk: Desk, headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = await desk.client.get("/api/v1/kb/spaces", headers=headers or desk.admin)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _titles(desk: Desk, **params: Any) -> list[str]:
    response = await desk.client.get("/api/v1/kb/items", headers=desk.admin, params=params)
    assert response.status_code == 200, response.text
    return sorted(i["title"] for i in response.json()["items"])


async def test_spaces_and_category_tree(desk: Desk) -> None:
    presale = await _post(desk, "/api/v1/kb/spaces", {"name": "售前", "description": "产品咨询"})
    aftersale = await _post(desk, "/api/v1/kb/spaces", {"name": "售后"})
    await _post(desk, "/api/v1/kb/spaces", {"name": "售后"}, expect=409)
    assert (presale["sort"], aftersale["sort"]) == (1, 2)

    logistics = await _post(
        desk, "/api/v1/kb/categories", {"space_id": aftersale["id"], "name": "物流"}
    )
    express = await _post(
        desk,
        "/api/v1/kb/categories",
        {"space_id": aftersale["id"], "parent_id": logistics["id"], "name": "快递"},
    )
    sf = await _post(
        desk,
        "/api/v1/kb/categories",
        {"space_id": aftersale["id"], "parent_id": express["id"], "name": "顺丰"},
    )
    # 最多三级；上级分类必须在同一空间；同一级不能重名。
    await _post(
        desk,
        "/api/v1/kb/categories",
        {"space_id": aftersale["id"], "parent_id": sf["id"], "name": "第四级"},
        expect=422,
    )
    await _post(
        desk,
        "/api/v1/kb/categories",
        {"space_id": presale["id"], "parent_id": logistics["id"], "name": "跨空间"},
        expect=422,
    )
    await _post(
        desk,
        "/api/v1/kb/categories",
        {"space_id": aftersale["id"], "parent_id": logistics["id"], "name": "快递"},
        expect=409,
    )
    returns = await _post(
        desk, "/api/v1/kb/categories", {"space_id": aftersale["id"], "name": "退换货"}
    )
    # 不能移到自己的下级下面；移动后超过三级也不行。
    loop = await desk.client.patch(
        f"/api/v1/kb/categories/{logistics['id']}",
        headers=desk.admin,
        json={"parent_id": sf["id"]},
    )
    assert loop.status_code == 422
    too_deep = await desk.client.patch(
        f"/api/v1/kb/categories/{logistics['id']}",
        headers=desk.admin,
        json={"parent_id": returns["id"]},
    )
    assert too_deep.status_code == 422
    moved = await desk.client.patch(
        f"/api/v1/kb/categories/{sf['id']}",
        headers=desk.admin,
        json={"parent_id": None, "name": "顺丰速运"},
    )
    assert moved.status_code == 200, moved.text
    assert (moved.json()["parent_id"], moved.json()["name"]) == (None, "顺丰速运")

    # 知识归入分类（只给分类时取分类所在的空间）；按分类筛选时包括下级分类。
    await create(desk, title="快递几天到", category_id=express["id"])
    await create(desk, title="物流停发通知", category_id=logistics["id"])
    await create(desk, title="怎么退货", space_id=aftersale["id"], category_id=returns["id"])
    await create(desk, title="产品有哪些型号", space_id=presale["id"])
    await create(desk, title="公司地址")
    mismatch = await desk.client.post(
        "/api/v1/kb/items",
        headers=desk.admin,
        json={
            "title": "x",
            "content": "y",
            "space_id": presale["id"],
            "category_id": express["id"],
        },
    )
    assert mismatch.status_code == 422

    assert await _titles(desk, space_id=aftersale["id"]) == [
        "快递几天到",
        "怎么退货",
        "物流停发通知",
    ]
    assert await _titles(desk, category_id=logistics["id"]) == ["快递几天到", "物流停发通知"]
    assert await _titles(desk, unassigned=True) == ["公司地址"]
    listed = await _spaces(desk)
    assert [(s["name"], s["items"]) for s in listed["items"]] == [("售前", 1), ("售后", 3)]
    assert listed["unassigned"] == 1
    tree = {c["name"]: c for c in listed["items"][1]["categories"]}
    assert tree["快递"]["parent_id"] == logistics["id"]
    assert tree["快递"]["items"] == 1

    # 只改空间：原分类不在新空间里就去掉。
    [item] = (
        await desk.client.get("/api/v1/kb/items", headers=desk.admin, params={"q": "快递几天到"})
    ).json()["items"]
    changed = await patch(desk, item, space_id=presale["id"])
    assert (changed["space_id"], changed["category_id"]) == (presale["id"], None)

    # 删除分类：下级分类一并删除，知识保留在空间里。
    deleted = await desk.client.delete(
        f"/api/v1/kb/categories/{logistics['id']}", headers=desk.admin
    )
    assert deleted.status_code == 204
    names = [c["name"] for c in (await _spaces(desk))["items"][1]["categories"]]
    assert sorted(names) == ["退换货", "顺丰速运"]
    assert await _titles(desk, space_id=aftersale["id"]) == ["怎么退货", "物流停发通知"]

    # 删除空间：知识移出空间，渠道不再限定这个空间。
    [channel] = (await desk.client.get("/api/v1/channels", headers=desk.admin)).json()["items"]
    limited = await desk.client.patch(
        f"/api/v1/channels/{channel['id']}",
        headers=desk.admin,
        json={"kb_space_ids": [aftersale["id"], presale["id"]]},
    )
    assert limited.status_code == 200, limited.text
    removed = await desk.client.delete(f"/api/v1/kb/spaces/{aftersale['id']}", headers=desk.admin)
    assert removed.status_code == 204
    assert await _titles(desk, unassigned=True) == ["公司地址", "怎么退货", "物流停发通知"]
    [channel] = (await desk.client.get("/api/v1/channels", headers=desk.admin)).json()["items"]
    assert channel["kb_space_ids"] == [presale["id"]]
    assert [s["name"] for s in (await _spaces(desk))["items"]] == ["售前"]


async def test_agents_browse_spaces_but_cannot_manage_them(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    space = await _post(desk, "/api/v1/kb/spaces", {"name": "售后"})
    await create(desk, title="怎么退货", space_id=space["id"])
    await create(desk, title="快递几天到")
    assert [s["name"] for s in (await _spaces(desk, alice.headers))["items"]] == ["售后"]
    denied = await desk.client.post(
        "/api/v1/kb/spaces", headers=alice.headers, json={"name": "售前"}
    )
    assert denied.status_code == 403
    # 在空间里检索。
    response = await desk.client.get(
        "/api/v1/kb/search",
        headers=alice.headers,
        params={"q": "快递几天到", "space_id": space["id"]},
    )
    assert response.status_code == 200, response.text
    assert all(h["title"] != "快递几天到" for h in response.json()["items"])


async def _notifications(desk: Desk, agent: Agent, **params: Any) -> dict[str, Any]:
    response = await desk.client.get("/api/v1/notifications", headers=agent.headers, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_owners_are_reminded_before_knowledge_expires(desk: Desk) -> None:
    bob = await desk.agent("bob", online=False)
    alice = await desk.agent("alice", online=False)
    now = datetime.now(UTC)
    soon = await create(
        desk,
        title="国庆活动规则",
        owner_id=str(bob.staff_id),
        valid_to=(now + timedelta(days=3)).isoformat(),
    )
    await create(desk, title="长期有效", valid_to=(now + timedelta(days=30)).isoformat())
    unknown = await desk.client.post(
        "/api/v1/kb/items",
        headers=desk.admin,
        json={"title": "x", "content": "y", "owner_id": soon["id"]},
    )
    assert unknown.status_code == 422

    assert await remind_expiring(desk.ctx) == 1
    assert await remind_expiring(desk.ctx) == 0
    inbox = await _notifications(desk, bob)
    assert inbox["unread"] == 1
    [notice] = inbox["items"]
    assert notice["kind"] == "kb_expiring"
    assert notice["title"] == "知识即将到期：国庆活动规则"
    assert notice["link"] == f"/knowledge?item={soon['id']}"
    assert (await _notifications(desk, alice))["items"] == []
    mine = await desk.client.get("/api/v1/kb/items", headers=bob.headers, params={"mine": True})
    assert [i["title"] for i in mine.json()["items"]] == ["国庆活动规则"]

    # 延期后到期前再提醒一次。
    await patch(desk, soon, valid_to=(now + timedelta(days=5)).isoformat())
    assert await remind_expiring(desk.ctx) == 1
    assert (await _notifications(desk, bob))["unread"] == 2

    # 只能处理自己的站内信。
    stranger = await desk.client.post(
        f"/api/v1/notifications/{notice['id']}/read", headers=alice.headers
    )
    assert stranger.status_code == 404
    read = await desk.client.post(f"/api/v1/notifications/{notice['id']}/read", headers=bob.headers)
    assert read.status_code == 204
    assert (await _notifications(desk, bob))["unread"] == 1
    unread = await _notifications(desk, bob, unread=True)
    assert len(unread["items"]) == 1
    await desk.client.post("/api/v1/notifications/read-all", headers=bob.headers)
    assert (await _notifications(desk, bob))["unread"] == 0


async def test_knowledge_without_active_owner_reminds_managers(desk: Desk) -> None:
    now = datetime.now(UTC)
    await create(desk, title="限时优惠", valid_to=(now + timedelta(days=2)).isoformat())
    # 创建者（管理员）在职：提醒创建者。
    assert await remind_expiring(desk.ctx) == 1
    [row] = await desk.sql("SELECT kind, staff_id FROM staff_notifications")
    [admin] = await desk.sql("SELECT id FROM staff WHERE username = 'admin'")
    assert (row["kind"], row["staff_id"]) == ("kb_expiring", admin["id"])


async def test_knowledge_is_pushed_to_selected_skill_groups(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)
    group = await _post(
        desk,
        "/api/v1/skill-groups",
        {"name": "售后组", "members": [{"staff_id": str(alice.staff_id)}]},
    )
    await create(desk, title="售后新规", must_read=True, audience_group_ids=[group["id"]])
    await create(desk, title="全员必读", must_read=True)
    bad = await desk.client.post(
        "/api/v1/kb/items",
        headers=desk.admin,
        json={"title": "x", "content": "y", "audience_group_ids": [str(bob.staff_id)]},
    )
    assert bad.status_code == 422

    alice_feed = await feed(desk, alice)
    bob_feed = await feed(desk, bob)
    assert sorted(e["title"] for e in alice_feed["must_read"]) == ["全员必读", "售后新规"]
    assert [e["title"] for e in bob_feed["must_read"]] == ["全员必读"]
    assert "售后新规" not in [e["title"] for e in bob_feed["events"]]
    # 推送给技能组的知识仍然可以检索和查看，只是不出现在其他人的动态里。
    items = await desk.client.get("/api/v1/kb/items", headers=bob.headers)
    assert "售后新规" in [i["title"] for i in items.json()["items"]]

    [targeted] = [
        i
        for i in (await desk.client.get("/api/v1/kb/items", headers=desk.admin)).json()["items"]
        if i["title"] == "售后新规"
    ]
    stats = await desk.client.get(f"/api/v1/kb/items/{targeted['id']}/reads", headers=desk.admin)
    assert stats.status_code == 200, stats.text
    assert stats.json()["total"] == 1
    assert [r["display_name"] for r in stats.json()["readers"]] == ["Alice"]
