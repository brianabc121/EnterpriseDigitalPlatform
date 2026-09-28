"""坐席经平台发送消息：先写库再发往 IM，回调与对账按 pmid 关联，不重复入库。"""

import json
import uuid

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.conversation.reconcile import reconcile_all
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


async def serving(desk: Desk) -> tuple[Agent, Visitor, uuid.UUID]:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == alice.staff_id
    return alice, visitor, chat["id"]


async def send(
    desk: Desk, agent: Agent, session_id: uuid.UUID, text: str, client_msg_id: str | None = None
) -> httpx.Response:
    return await desk.client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=agent.headers,
        json={"client_msg_id": client_msg_id or uuid.uuid4().hex, "text": text},
    )


async def test_agent_message_is_stored_once_and_reaches_the_room(desk: Desk) -> None:
    alice, visitor, session_id = await serving(desk)

    response = await send(desk, alice, session_id, "您好，请问有什么可以帮您？")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["sender_type"], body["sender_name"], body["source"], body["send_status"]) == (
        "agent",
        "Alice",
        "api",
        "sent",
    )
    [im_msg] = [m for m in desk.im.groups[visitor.group_id].messages if m.send_id == alice.im_user]
    assert json.loads(im_msg.ex) == {"pmid": body["id"]}
    assert im_msg.sender_nickname == "Alice"
    assert body["channel_msg_id"] == im_msg.server_msg_id

    # 回调到达后关联到同一行，不重复入库。
    await desk.flush()
    rows = await desk.sql(
        "SELECT id, source, send_status FROM messages WHERE sender_type = 'agent'"
    )
    assert [(str(r["id"]), r["source"], r["send_status"]) for r in rows] == [
        (body["id"], "api", "sent")
    ]
    chat = await desk.session_of(visitor)
    assert chat["first_response_at"] is not None

    # 历史接口里也能看到坐席姓名和发送状态。
    history = await desk.client.get(
        f"/api/v1/rooms/{visitor.room_id}/messages", headers=alice.headers
    )
    newest = history.json()["items"][0]
    assert (newest["id"], newest["sender_name"], newest["session_id"]) == (
        body["id"],
        "Alice",
        str(session_id),
    )


async def test_resending_the_same_client_msg_id_is_idempotent(desk: Desk) -> None:
    alice, visitor, session_id = await serving(desk)

    first = await send(desk, alice, session_id, "稍等", client_msg_id="retry-0001")
    second = await send(desk, alice, session_id, "稍等", client_msg_id="retry-0001")

    assert first.json()["id"] == second.json()["id"]
    sent = [m for m in desk.im.groups[visitor.group_id].messages if m.send_id == alice.im_user]
    assert len(sent) == 1


async def test_failed_send_can_be_retried(desk: Desk) -> None:
    alice, _, session_id = await serving(desk)
    desk.im.down = True

    failed = await send(desk, alice, session_id, "网络不好", client_msg_id="flaky-0001")

    assert failed.status_code == 503
    [row] = await desk.sql("SELECT send_status, send_error FROM messages WHERE source = 'api'")
    assert row["send_status"] == "failed"
    assert row["send_error"]

    desk.im.down = False
    retried = await send(desk, alice, session_id, "网络不好", client_msg_id="flaky-0001")

    assert retried.status_code == 200
    assert retried.json()["send_status"] == "sent"
    assert len(await desk.sql("SELECT 1 FROM messages WHERE source = 'api'")) == 1


async def test_callback_marks_a_message_sent_even_if_the_api_call_failed(desk: Desk) -> None:
    """OpenIM 已收下消息但响应超时：回调到达后仍关联到原来的行并标为已发送。"""
    alice, visitor, session_id = await serving(desk)
    desk.im.down = True
    failed = await send(desk, alice, session_id, "你好")
    assert failed.status_code == 503
    pmid = (await desk.sql("SELECT id FROM messages WHERE source = 'api'"))[0]["id"]
    desk.im.down = False
    # 模拟：IM 里其实有这条消息。
    sent = desk.im.send_as(alice.im_user, visitor.group_id, "你好")
    sent.ex = json.dumps({"pmid": str(pmid)})
    desk.im.callbacks[-1]["ex"] = sent.ex

    await desk.flush()

    rows = await desk.sql(
        "SELECT id, send_status, channel_msg_id FROM messages WHERE sender_type = 'agent'"
    )
    assert [(r["id"], r["send_status"], r["channel_msg_id"]) for r in rows] == [
        (pmid, "sent", sent.server_msg_id)
    ]


async def test_reconcile_links_platform_messages_by_pmid(desk: Desk) -> None:
    alice, _, session_id = await serving(desk)
    await desk.flush()
    response = await send(desk, alice, session_id, "对账也不重复")
    # 回调丢失，只靠对账。
    desk.im.callbacks.clear()

    report = await reconcile_all(desk.ctx.db, desk.ctx.im)

    rows = await desk.sql(
        "SELECT id, im_seq FROM messages WHERE sender_type = 'agent' ORDER BY sent_at"
    )
    assert [str(r["id"]) for r in rows] == [response.json()["id"]]
    assert rows[0]["im_seq"] is not None
    assert report.recovered == 0


async def test_only_the_assignee_can_send(desk: Desk) -> None:
    _, _, session_id = await serving(desk)
    bob = await desk.agent("bob", online=False)

    # 看不到别人的会话：404；管理员看得到但不是接待人：403。
    assert (await send(desk, bob, session_id, "我来回复")).status_code == 404
    admin = Agent(uuid.uuid4(), "admin", desk.admin_token, "")
    assert (await send(desk, admin, session_id, "管理员")).status_code == 403


async def test_closed_session_cannot_be_replied_to(desk: Desk) -> None:
    alice, _, session_id = await serving(desk)
    await desk.client.post(f"/api/v1/sessions/{session_id}/close", headers=alice.headers)

    response = await send(desk, alice, session_id, "还在吗")

    assert response.status_code == 409
    assert response.json()["error"]["message"] == "会话已结束"
