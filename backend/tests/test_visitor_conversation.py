"""访客端查询：消息历史与会话状态（排队位置、接待坐席）。"""

import uuid

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from tests.desk import Desk, Visitor
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


def headers(visitor: Visitor) -> dict[str, str]:
    return {"X-Visitor-Token": visitor.visitor_token}


async def test_visitor_sees_the_conversation_history(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages",
        headers=alice.headers,
        json={"client_msg_id": uuid.uuid4().hex, "text": "您好，我是 Alice"},
    )
    await desk.flush()

    response = await desk.client.get("/api/v1/visitor/messages", headers=headers(visitor))

    assert response.status_code == 200, response.text
    items = list(reversed(response.json()["items"]))
    assert [(m["sender_type"], m["sender_name"], m["text"]) for m in items] == [
        ("customer", None, "你好"),
        ("system", None, "客服 Alice 为您服务。"),
        ("agent", "Alice", "您好，我是 Alice"),
    ]
    im_ids = {m.server_msg_id for m in desk.im.groups[visitor.group_id].messages}
    assert all(m["server_msg_id"] in im_ids for m in items)


async def test_visitor_session_state_and_queue_position(desk: Desk) -> None:
    first, second = await desk.visitor(), await desk.visitor()

    assert (await desk.client.get("/api/v1/visitor/session", headers=headers(first))).json()[
        "status"
    ] == "none"

    await desk.say(first, "我先来的")
    await desk.say(second, "我后来的")

    state = (await desk.client.get("/api/v1/visitor/session", headers=headers(second))).json()
    assert (state["status"], state["queue_position"]) == ("queued", 2)

    await desk.agent("alice")
    state = (await desk.client.get("/api/v1/visitor/session", headers=headers(first))).json()
    assert (state["status"], state["assignee_name"], state["queue_position"]) == (
        "human_serving",
        "Alice",
        None,
    )
    state = (await desk.client.get("/api/v1/visitor/session", headers=headers(second))).json()
    # Alice 还有名额（默认并发 5），第二位也分到了。
    assert state["status"] == "human_serving"


async def test_visitor_endpoints_need_a_valid_token(desk: Desk) -> None:
    for path in ("/api/v1/visitor/messages", "/api/v1/visitor/session"):
        assert (await desk.client.get(path)).status_code == 401
        bad = await desk.client.get(path, headers={"X-Visitor-Token": "nope"})
        assert bad.status_code == 401
