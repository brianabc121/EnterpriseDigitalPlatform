"""技能组、路由策略、坐席看板、渠道绑定与团队数据范围。"""

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from tests.desk import Desk
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


async def test_skill_group_crud(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)

    response = await desk.client.post(
        "/api/v1/skill-groups",
        headers=desk.admin,
        json={
            "name": "售前",
            "members": [
                {"staff_id": str(alice.staff_id), "is_lead": True},
                {"staff_id": str(bob.staff_id)},
            ],
        },
    )
    assert response.status_code == 201, response.text
    group = response.json()
    assert [(m["display_name"], m["is_lead"]) for m in group["members"]] == [
        ("Alice", True),
        ("Bob", False),
    ]

    duplicate = await desk.client.post(
        "/api/v1/skill-groups", headers=desk.admin, json={"name": "售前"}
    )
    assert duplicate.status_code == 409

    response = await desk.client.patch(
        f"/api/v1/skill-groups/{group['id']}",
        headers=desk.admin,
        json={"name": "售前咨询", "members": [{"staff_id": str(bob.staff_id)}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "售前咨询"
    assert [m["display_name"] for m in response.json()["members"]] == ["Bob"]

    bad = await desk.client.patch(
        f"/api/v1/skill-groups/{group['id']}",
        headers=desk.admin,
        json={"members": [{"staff_id": "00000000-0000-0000-0000-000000000000"}]},
    )
    assert bad.status_code == 422

    response = await desk.client.delete(f"/api/v1/skill-groups/{group['id']}", headers=desk.admin)
    assert response.status_code == 204
    listed = await desk.client.get("/api/v1/skill-groups", headers=desk.admin)
    assert listed.json()["items"] == []

    # 坐席没有 routing:manage。
    forbidden = await desk.client.get("/api/v1/skill-groups", headers=alice.headers)
    assert forbidden.status_code == 403


async def test_routing_policies_and_channel_binding(desk: Desk) -> None:
    [default] = (await desk.client.get("/api/v1/routing-policies", headers=desk.admin)).json()[
        "items"
    ]
    assert (default["name"], default["is_default"], default["mode"]) == (
        "默认策略",
        True,
        "human_first",
    )

    invalid = await desk.client.post(
        "/api/v1/routing-policies",
        headers=desk.admin,
        json={"name": "夜间", "business_hours": {"tz": "Mars/Base", "days": {}}},
    )
    assert invalid.status_code == 422
    invalid = await desk.client.post(
        "/api/v1/routing-policies",
        headers=desk.admin,
        json={"name": "夜间", "business_hours": {"days": {"1": [["18:00", "09:00"]]}}},
    )
    assert invalid.status_code == 422

    response = await desk.client.post(
        "/api/v1/routing-policies",
        headers=desk.admin,
        json={
            "name": "VIP",
            "max_wait_seconds": 120,
            "business_hours": {"days": {"1": [["09:00", "18:00"]], "7": []}},
        },
    )
    assert response.status_code == 201, response.text
    vip = response.json()
    assert vip["business_hours"] == {
        "tz": "Asia/Shanghai",
        "days": {"1": [["09:00", "18:00"]], "7": []},
    }

    [channel] = (await desk.client.get("/api/v1/channels", headers=desk.admin)).json()["items"]
    assert channel["routing_policy_id"] is None
    response = await desk.client.patch(
        f"/api/v1/channels/{channel['id']}",
        headers=desk.admin,
        json={"routing_policy_id": vip["id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["routing_policy_id"] == vip["id"]
    policies = (await desk.client.get("/api/v1/routing-policies", headers=desk.admin)).json()
    assert {p["name"]: p["channel_ids"] for p in policies["items"]} == {
        "默认策略": [],
        "VIP": [channel["id"]],
    }

    # 换默认策略；默认策略不能删除；删除后绑定的渠道改用默认策略。
    response = await desk.client.patch(
        f"/api/v1/routing-policies/{vip['id']}", headers=desk.admin, json={"is_default": True}
    )
    assert response.json()["is_default"] is True
    policies = (await desk.client.get("/api/v1/routing-policies", headers=desk.admin)).json()
    assert [p["name"] for p in policies["items"] if p["is_default"]] == ["VIP"]
    assert (
        await desk.client.delete(f"/api/v1/routing-policies/{vip['id']}", headers=desk.admin)
    ).status_code == 409
    await desk.client.patch(
        f"/api/v1/routing-policies/{default['id']}", headers=desk.admin, json={"is_default": True}
    )
    assert (
        await desk.client.delete(f"/api/v1/routing-policies/{vip['id']}", headers=desk.admin)
    ).status_code == 204
    [channel] = (await desk.client.get("/api/v1/channels", headers=desk.admin)).json()["items"]
    assert channel["routing_policy_id"] is None


async def test_agent_board_and_team_scope(desk: Desk) -> None:
    lead = await desk.agent("lead", roles=["supervisor"], online=False)
    alice = await desk.agent("alice")
    outsider = await desk.agent("carol", online=False)
    await desk.client.post(
        "/api/v1/skill-groups",
        headers=desk.admin,
        json={
            "name": "一组",
            "members": [
                {"staff_id": str(lead.staff_id), "is_lead": True},
                {"staff_id": str(alice.staff_id)},
            ],
        },
    )
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == alice.staff_id

    board = await desk.client.get("/api/v1/agents", headers=desk.admin)
    assert {(a["username"], a["status"], a["active_sessions"]) for a in board.json()["items"]} == {
        ("admin", "offline", 0),
        ("lead", "offline", 0),
        ("alice", "online", 1),
        ("carol", "offline", 0),
    }
    team_board = await desk.client.get("/api/v1/agents", headers=lead.headers)
    assert sorted(a["username"] for a in team_board.json()["items"]) == ["alice", "lead"]
    assert (await desk.client.get("/api/v1/agents", headers=alice.headers)).status_code == 403

    # 组长能看到组员接待的会话和客户；组外坐席看不到。
    for who, visible in ((lead, True), (outsider, False)):
        sessions = await desk.client.get("/api/v1/sessions?status=open", headers=who.headers)
        assert [s["id"] for s in sessions.json()["items"]] == ([str(chat["id"])] if visible else [])
        customer = await desk.client.get(
            f"/api/v1/customers/{chat['customer_id']}", headers=who.headers
        )
        assert customer.status_code == (200 if visible else 404)

    detail = await desk.client.get(f"/api/v1/sessions/{chat['id']}", headers=lead.headers)
    assert [e["type"] for e in detail.json()["events"]] == ["created", "queued", "assigned"]
    # 主管有 session:transfer_any，可以结束组员的会话。
    response = await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=lead.headers)
    assert response.status_code == 200


async def test_agent_im_token_provisions_the_staff_user(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)

    response = await desk.client.post("/api/v1/agent/im-token", headers=alice.headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_id"] == alice.im_user
    assert body["system_user_id"] == "acme_sys"
    assert (body["api_url"], body["ws_url"], body["platform_id"]) == (
        "http://localhost:10002",
        "ws://localhost:10001",
        5,
    )
    assert desk.im.tokens[body["token"]] == alice.im_user
    assert frozenset(("acme_sys", alice.im_user)) in desk.im.friends
    [state] = await desk.sql("SELECT im_ready_at FROM agent_states")
    assert state["im_ready_at"] is not None

    desk.im.down = True
    response = await desk.client.post("/api/v1/agent/im-token", headers=alice.headers)
    assert response.status_code == 503


async def test_going_offline_requeues_unanswered_sessions(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")

    state = await desk.set_status(alice, "offline")

    assert state["status"] == "offline"
    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("queued", None)
    assert alice.im_user not in desk.members(visitor)


async def test_leave_message_visibility(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)
    [policy] = (await desk.client.get("/api/v1/routing-policies", headers=desk.admin)).json()[
        "items"
    ]
    await desk.client.patch(
        f"/api/v1/routing-policies/{policy['id']}",
        headers=desk.admin,
        json={"business_hours": {"days": {}}},
    )
    visitor = await desk.visitor()
    [row] = await desk.sql("SELECT customer_id FROM rooms WHERE id = $1", visitor.room_id)
    await desk.sql(
        "UPDATE customers SET owner_id = $1 WHERE id = $2", alice.staff_id, row["customer_id"]
    )

    await desk.say(visitor, "留言")

    # 留言指派给归属坐席 Alice；Bob 看不到。
    for who, count in ((alice, 1), (bob, 0), (None, 1)):
        headers = who.headers if who else desk.admin
        todos = await desk.client.get("/api/v1/todos", headers=headers)
        assert todos.json()["total"] == count
    [todo] = (await desk.client.get("/api/v1/todos", headers=alice.headers)).json()["items"]
    assert (todo["type_code"], todo["assignee_id"]) == ("leave_message", str(alice.staff_id))
