"""会话协作：交还 AI、客户取消排队、主管转人工、旁听与邀请协助（设计文档 §8.2、§14.1）。"""

import uuid

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from tests.desk import Agent, Desk, Visitor
from tests.factories import create_knowledge_role
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_ai_reception import ANSWER, bot_texts, enable_ai


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


def _visitor_headers(visitor: Visitor) -> dict[str, str]:
    return {"X-Visitor-Token": visitor.visitor_token}


async def _serving(desk: Desk, agent: Agent) -> tuple[Visitor, dict[str, object]]:
    visitor = await desk.visitor()
    await desk.say(visitor, "转人工")
    chat = dict(await desk.session_of(visitor))
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", agent.staff_id)
    return visitor, chat


async def test_agent_returns_the_session_to_ai(desk: Desk) -> None:
    await enable_ai(desk)
    alice = await desk.agent("alice")
    visitor, chat = await _serving(desk, alice)

    returned = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/return-to-ai", headers=alice.headers
    )
    assert returned.status_code == 204, returned.text
    await desk.flush()
    after = await desk.session_of(visitor)
    assert (after["status"], after["assignee_id"]) == ("ai_serving", None)
    assert alice.im_user not in desk.members(visitor)
    assert desk.notices(visitor)[-1].startswith("已为您转回智能客服")
    assert {"type": "session.revoked", "session_id": str(chat["id"]),
            "room_id": str(visitor.room_id)} in desk.im.signals_to(alice.im_user)  # fmt: skip
    assert "returned_to_ai" in await desk.events_of(after["id"])

    # AI 继续接待，只回答交还之后的新问题。
    await desk.say(visitor, "快递几天能到")
    assert bot_texts(desk, visitor) == [ANSWER]
    again = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/return-to-ai", headers=alice.headers
    )
    assert again.status_code in (403, 404, 409)


async def test_return_to_ai_needs_ai(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    response = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/return-to-ai", headers=alice.headers
    )
    assert response.status_code == 409
    assert "AI 接待暂时不可用" in response.json()["error"]["message"]


async def test_visitor_cancels_the_queue(desk: Desk) -> None:
    # 没有启用 AI：取消后会话结束。
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    assert (await desk.session_of(visitor))["status"] == "queued"
    cancelled = await desk.client.post(
        "/api/v1/visitor/cancel-queue", headers=_visitor_headers(visitor)
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "closed"
    await desk.flush()
    chat = await desk.session_of(visitor)
    assert chat["close_reason"] == "visitor_cancel"
    assert desk.notices(visitor)[-1] == "已取消排队。如需帮助，请随时留言。"
    not_queued = await desk.client.post(
        "/api/v1/visitor/cancel-queue", headers=_visitor_headers(visitor)
    )
    assert not_queued.status_code == 409

    # 启用 AI：取消后回到 AI 接待。
    await enable_ai(desk)
    other = await desk.visitor()
    await desk.say(other, "转人工")
    assert (await desk.session_of(other))["status"] == "queued"
    back = await desk.client.post("/api/v1/visitor/cancel-queue", headers=_visitor_headers(other))
    assert back.json()["status"] == "ai_serving"
    await desk.flush()
    assert desk.notices(other)[-1] == "已取消排队，智能客服继续为您服务。"
    await desk.say(other, "快递几天能到")
    assert bot_texts(desk, other) == [ANSWER]


async def test_supervisor_hands_an_ai_session_to_humans(desk: Desk) -> None:
    await enable_ai(desk)
    supervisor = await desk.agent("susan", roles=["supervisor"], online=False)
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "快递几天能到")
    chat = await desk.session_of(visitor)
    assert chat["status"] == "ai_serving"
    # 工作台"进行中"：AI 或人工接待中的会话，不含排队。
    await desk.set_status(alice, "away")
    queued = await desk.visitor()
    await desk.say(queued, "转人工")
    assert (await desk.session_of(queued))["status"] == "queued"
    serving = await desk.client.get("/api/v1/sessions?status=serving", headers=desk.admin)
    assert [s["id"] for s in serving.json()["items"]] == [str(chat["id"])]
    await desk.set_status(alice, "online")

    denied = await desk.client.post(f"/api/v1/sessions/{chat['id']}/handoff", headers=alice.headers)
    assert denied.status_code == 403
    handed = await desk.client.post(f"/api/v1/sessions/{chat['id']}/handoff", headers=desk.admin)
    assert handed.status_code == 200, handed.text
    await desk.flush()
    after = await desk.session_of(visitor)
    assert (after["status"], after["handoff_reason"], after["assignee_id"]) == (
        "human_serving",
        "supervisor",
        alice.staff_id,
    )
    # 主管看不到不在自己组里的会话。
    hidden = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/monitor", headers=supervisor.headers
    )
    assert hidden.status_code == 404


async def test_monitor_and_assist(desk: Desk) -> None:
    alice = await desk.agent("alice")
    bob = await desk.agent("bob", online=False)
    supervisor = await desk.agent("susan", roles=["supervisor"], online=False)
    group = await desk.client.post(
        "/api/v1/skill-groups",
        headers=desk.admin,
        json={
            "name": "一线",
            "members": [
                {"staff_id": str(alice.staff_id)},
                {"staff_id": str(supervisor.staff_id), "is_lead": True},
            ],
        },
    )
    assert group.status_code == 201, group.text
    visitor, chat = await _serving(desk, alice)
    notices_before = list(desk.notices(visitor))

    # 主管旁听：进群，客户看不到提示；主管只能看不能发。
    watched = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/monitor", headers=supervisor.headers
    )
    assert watched.status_code == 200, watched.text
    await desk.flush()
    assert watched.json()["my_role"] == "monitor"
    assert [w["role"] for w in watched.json()["watchers"]] == ["monitor"]
    assert supervisor.im_user in desk.members(visitor)
    assert desk.notices(visitor) == notices_before
    mute = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages",
        headers=supervisor.headers,
        json={"client_msg_id": uuid.uuid4().hex, "text": "我来说两句"},
    )
    assert mute.status_code == 403
    watching = await desk.client.get("/api/v1/sessions?watching=true", headers=supervisor.headers)
    assert [(s["id"], s["my_role"]) for s in watching.json()["items"]] == [
        (str(chat["id"]), "monitor")
    ]
    # 坐席没有旁听权限。
    agent_monitor = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/monitor", headers=bob.headers
    )
    assert agent_monitor.status_code == 403

    # 邀请协助：Bob 进群，客户看到提示，Bob 可以看到会话和客户并发言。
    invited = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/assists",
        headers=alice.headers,
        json={"staff_id": str(bob.staff_id)},
    )
    assert invited.status_code == 200, invited.text
    await desk.flush()
    assert bob.im_user in desk.members(visitor)
    assert desk.notices(visitor)[-1] == "客服 Bob 加入了会话。"
    assert any(s["type"] == "session.assist" for s in desk.im.signals_to(bob.im_user))
    detail = await desk.client.get(f"/api/v1/sessions/{chat['id']}", headers=bob.headers)
    assert detail.json()["my_role"] == "assist"
    customer = await desk.client.get(
        f"/api/v1/customers/{chat['customer_id']}", headers=bob.headers
    )
    assert customer.status_code == 200
    sent = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages",
        headers=bob.headers,
        json={"client_msg_id": uuid.uuid4().hex, "text": "我是 Bob，帮您查一下"},
    )
    assert sent.status_code == 200, sent.text
    knowledge = await create_knowledge_role(desk.client, desk.admin_token)
    manager = await desk.agent("kate", roles=[knowledge], online=False)
    no_workbench = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/assists",
        headers=alice.headers,
        json={"staff_id": str(manager.staff_id)},
    )
    assert no_workbench.status_code == 422

    # Bob 退出协助；坐席结束会话后旁听的主管也退出。
    left = await desk.client.delete(
        f"/api/v1/sessions/{chat['id']}/watchers/{bob.staff_id}", headers=bob.headers
    )
    assert left.status_code == 204
    await desk.flush()
    assert bob.im_user not in desk.members(visitor)
    gone = await desk.client.get(f"/api/v1/sessions/{chat['id']}", headers=bob.headers)
    assert gone.status_code == 404
    closed = await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=alice.headers)
    assert closed.status_code == 200
    await desk.flush()
    assert supervisor.im_user not in desk.members(visitor)
    rows = await desk.sql("SELECT staff_id, left_at FROM session_watchers ORDER BY joined_at")
    assert all(r["left_at"] is not None for r in rows)
