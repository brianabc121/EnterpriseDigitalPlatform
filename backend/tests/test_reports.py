"""运营报表与首页实时数据：指标口径、团队数据范围、权限。"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from tests.desk import Agent, Desk, Visitor
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


async def get(desk: Desk, path: str, headers: dict[str, str], **params: Any) -> dict[str, Any]:
    response = await desk.client.get(f"/api/v1/reports/{path}", headers=headers, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def send(desk: Desk, agent: Agent, session_id: uuid.UUID, text: str) -> None:
    response = await desk.client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=agent.headers,
        json={"client_msg_id": uuid.uuid4().hex, "text": text},
    )
    assert response.status_code == 200, response.text
    await desk.flush()


async def served_and_rated(desk: Desk, agent: Agent, score: int) -> Visitor:
    """访客咨询 → 分配给 agent → 回复 → 结束 → 评价。"""
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == agent.staff_id
    await send(desk, agent, chat["id"], "您好，请讲")
    await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=agent.headers)
    await desk.flush()
    rated = await desk.client.post(
        "/api/v1/visitor/csat",
        headers={"X-Visitor-Token": visitor.visitor_token},
        json={"session_id": str(chat["id"]), "score": score},
    )
    assert rated.status_code == 204, rated.text
    return visitor


async def test_overview_measures_from_the_first_queue_and_assignment(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await served_and_rated(desk, alice, 4)
    chat = await desk.session_of(visitor)
    # 固定时间线：排队 → 30 秒后分配 → 再 60 秒首次回复 → 分配后 10 分钟结束。
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    await desk.sql(
        "UPDATE session_events SET created_at = $2 WHERE session_id = $1 AND type = 'queued'",
        chat["id"],
        t0,
    )
    await desk.sql(
        "UPDATE session_events SET created_at = $2 WHERE session_id = $1 AND type = 'assigned'",
        chat["id"],
        t0 + timedelta(seconds=30),
    )
    await desk.sql(
        "UPDATE sessions SET created_at = $2, first_response_at = $3, closed_at = $4,"
        " assigned_at = NULL WHERE id = $1",
        chat["id"],
        t0,
        t0 + timedelta(seconds=90),
        t0 + timedelta(seconds=630),
    )
    # 第二位访客还在接待中、还没回复；第三位访客留言。
    waiting = await desk.visitor()
    await desk.say(waiting, "在吗")
    leaving = await desk.visitor()
    await desk.client.post(
        "/api/v1/visitor/tickets",
        headers={"X-Visitor-Token": leaving.visitor_token},
        json={"content": "请回电"},
    )

    body = await get(desk, "overview", desk.admin)

    totals = body["totals"]
    assert {k: totals[k] for k in ("sessions", "human_sessions", "closed_sessions")} == {
        "sessions": 2,
        "human_sessions": 2,
        "closed_sessions": 1,
    }
    # 会话上的 assigned_at 被清空（模拟转接后刷新），时长仍按会话事件计算。
    assert (
        totals["avg_wait_seconds"],
        totals["avg_first_response_seconds"],
        totals["avg_handle_seconds"],
    ) == (15.0, 60.0, 600.0)
    assert (totals["csat_avg"], totals["csat_count"], totals["satisfied_rate"]) == (4.0, 1, 1.0)
    assert (totals["messages_in"], totals["agent_messages"], totals["tickets"]) == (2, 1, 1)
    assert len(body["days"]) == 7
    assert sum(day["sessions"] for day in body["days"]) == 2


async def test_team_scope_and_agent_report(desk: Desk) -> None:
    lead = await desk.agent("lead", roles=["supervisor"], online=False)
    alice = await desk.agent("alice")
    await served_and_rated(desk, alice, 5)
    await desk.set_status(alice, "away")
    bob = await desk.agent("bob")
    await served_and_rated(desk, bob, 2)
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

    everyone = await get(desk, "overview", desk.admin)
    team = await get(desk, "overview", lead.headers)
    admin_agents = await get(desk, "agents", desk.admin)
    team_agents = await get(desk, "agents", lead.headers)

    assert (everyone["totals"]["sessions"], everyone["totals"]["csat_avg"]) == (2, 3.5)
    assert (team["totals"]["sessions"], team["totals"]["csat_avg"]) == (1, 5.0)
    by_name = {a["display_name"]: a for a in admin_agents["items"]}
    assert (by_name["Alice"]["sessions"], by_name["Alice"]["messages"]) == (1, 1)
    assert (by_name["Bob"]["csat_avg"], by_name["Bob"]["status"]) == (2.0, "online")
    # 主管只看到所带团队；从没上线接待过的员工（Lead）不算坐席。
    assert {a["display_name"] for a in team_agents["items"]} == {"Alice"}


async def test_transfers_are_counted_for_the_agent_who_hands_over(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    bob = await desk.agent("bob")
    chat = await desk.session_of(visitor)
    response = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/transfer",
        headers=desk.admin,
        json={"to_staff_id": str(bob.staff_id), "force": True},
    )
    assert response.status_code == 200, response.text
    await desk.flush()

    overview = await get(desk, "overview", desk.admin)
    agents = {a["display_name"]: a for a in (await get(desk, "agents", desk.admin))["items"]}

    assert overview["totals"]["transfers"] == 1
    assert (agents["Alice"]["transfers_out"], agents["Bob"]["sessions"]) == (1, 1)
    assert alice.staff_id != bob.staff_id


async def test_realtime_numbers_by_role(desk: Desk) -> None:
    alice = await desk.agent("alice")
    serving = await desk.visitor()
    await desk.say(serving, "你好")
    await desk.set_status(alice, "away")
    queued = await desk.visitor()
    await desk.say(queued, "有人吗")
    await desk.sql(
        "UPDATE sessions SET queued_at = now() - interval '2 minutes' WHERE room_id = $1",
        queued.room_id,
    )

    admin = await get(desk, "realtime", desk.admin)
    mine = await get(desk, "realtime", alice.headers)

    assert (admin["queued"], admin["serving"], admin["agents_away"]) == (1, 1, 1)
    assert 115 <= admin["longest_wait_seconds"] <= 180
    assert (admin["today_sessions"], admin["my_serving"]) == (2, 0)
    # 坐席看到排队总数和自己的接待，看不到其他坐席的状态。
    assert (mine["queued"], mine["my_serving"], mine["my_today_sessions"]) == (1, 1, 1)
    assert (mine["serving"], mine["today_sessions"], mine["agents_away"]) == (1, 1, 0)


async def test_reports_need_permission_and_valid_ranges(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)

    forbidden = await desk.client.get("/api/v1/reports/overview", headers=alice.headers)
    agents = await desk.client.get("/api/v1/reports/agents", headers=alice.headers)
    bad_tz = await desk.client.get(
        "/api/v1/reports/overview", headers=desk.admin, params={"tz": "Mars/Base"}
    )
    backwards = await desk.client.get(
        "/api/v1/reports/overview",
        headers=desk.admin,
        params={"start": "2026-09-28", "end": "2026-09-01"},
    )
    utc = await desk.client.get(
        "/api/v1/reports/overview",
        headers=desk.admin,
        params={"start": "2026-09-01", "end": "2026-09-30", "tz": "UTC"},
    )

    assert (forbidden.status_code, agents.status_code) == (403, 403)
    assert (bad_tz.status_code, backwards.status_code) == (422, 422)
    assert (utc.json()["timezone"], len(utc.json()["days"])) == ("UTC", 30)
