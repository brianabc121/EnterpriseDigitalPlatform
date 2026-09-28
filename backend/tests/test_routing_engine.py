"""会话引擎：消息归入会话、排队与分配、结束、超时与断线（设计文档 §8.2、§11.3）。"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.conversation.outbox import dispatch_due
from app.modules.sessions.engine import republish_orphans, run_session_timers
from tests.desk import Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_openim_hooks import deliver


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def test_first_visitor_message_queues_a_session_and_reports_the_position(
    desk: Desk,
) -> None:
    visitor = await desk.visitor()

    await desk.say(visitor, "你好，想咨询价格")

    chat = await desk.session_of(visitor)
    assert chat["status"] == "queued"
    assert chat["queued_at"] is not None
    assert await desk.events_of(chat["id"]) == ["created", "queued"]
    assert desk.notices(visitor) == ["正在为您转接人工客服，您前面还有 0 位，请稍候。"]
    # 访客消息和系统提示都归入这个会话。
    messages = await desk.sql("SELECT sender_type, session_id FROM messages ORDER BY sent_at")
    assert [(m["sender_type"], m["session_id"]) for m in messages] == [
        ("customer", chat["id"]),
        ("system", chat["id"]),
    ]

    # 排队期间的后续消息归入同一个会话，不再重复提示。
    await desk.say(visitor, "在吗？")
    assert (await desk.session_of(visitor))["id"] == chat["id"]
    assert len(desk.notices(visitor)) == 1


async def test_online_agent_is_assigned_and_joins_the_room(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()

    await desk.say(visitor, "你好")

    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", alice.staff_id)
    assert await desk.events_of(chat["id"]) == ["created", "queued", "assigned"]
    assert alice.im_user in desk.members(visitor)
    assert desk.notices(visitor) == ["客服 Alice 为您服务。"]
    [signal] = desk.im.signals_to(alice.im_user)
    assert signal == {
        "type": "session.assigned",
        "session_id": str(chat["id"]),
        "room_id": str(visitor.room_id),
    }

    # 坐席能看到分配给自己的会话和其中的客户。
    response = await desk.client.get("/api/v1/sessions?mine=true", headers=alice.headers)
    [item] = response.json()["items"]
    assert (item["id"], item["status"], item["assignee_display_name"]) == (
        str(chat["id"]),
        "human_serving",
        "Alice",
    )
    response = await desk.client.get(
        f"/api/v1/customers/{chat['customer_id']}", headers=alice.headers
    )
    assert response.status_code == 200


async def test_queued_session_is_assigned_when_an_agent_comes_online(desk: Desk) -> None:
    visitor = await desk.visitor()
    await desk.say(visitor, "有人吗")
    alice = await desk.agent("alice", online=False)

    state = await desk.set_status(alice, "online")

    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", alice.staff_id)
    assert state == {
        "status": "online",
        "max_concurrency": 5,
        "active_sessions": 1,
        "last_seen_at": state["last_seen_at"],
    }


async def test_owner_first_then_least_loaded_and_capacity(desk: Desk) -> None:
    alice = await desk.agent("alice")
    bob = await desk.agent("bob")
    for agent in (alice, bob):
        response = await desk.client.patch(
            f"/api/v1/agents/{agent.staff_id}", headers=desk.admin, json={"max_concurrency": 1}
        )
        assert response.status_code == 204, response.text
    owned, other, third = await desk.visitor(), await desk.visitor(), await desk.visitor()
    [row] = await desk.sql("SELECT customer_id FROM rooms WHERE id = $1", owned.room_id)
    await desk.sql(
        "UPDATE customers SET owner_id = $1 WHERE id = $2", bob.staff_id, row["customer_id"]
    )

    # 归属 Bob 的客户分给 Bob（尽管两人负载相同）。
    await desk.say(owned, "我找 Bob")
    assert (await desk.session_of(owned))["assignee_id"] == bob.staff_id
    # 其他客户分给还有名额的 Alice。
    await desk.say(other, "你好")
    assert (await desk.session_of(other))["assignee_id"] == alice.staff_id
    # 两人都满了：排队。
    await desk.say(third, "你好")
    assert (await desk.session_of(third))["status"] == "queued"
    assert desk.notices(third) == ["正在为您转接人工客服，您前面还有 0 位，请稍候。"]

    # Bob 结束会话后，空出的名额立即分给排队的客户。
    chat = await desk.session_of(owned)
    response = await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=bob.headers)
    assert response.status_code == 200, response.text
    await desk.flush()
    assert (await desk.session_of(third))["assignee_id"] == bob.staff_id


async def test_skill_group_limits_who_gets_the_session(desk: Desk) -> None:
    alice = await desk.agent("alice")
    await desk.agent("bob")
    response = await desk.client.post(
        "/api/v1/skill-groups",
        headers=desk.admin,
        json={"name": "售后", "members": [{"staff_id": str(alice.staff_id)}]},
    )
    group_id = response.json()["id"]
    [policy] = (await desk.client.get("/api/v1/routing-policies", headers=desk.admin)).json()[
        "items"
    ]
    response = await desk.client.patch(
        f"/api/v1/routing-policies/{policy['id']}",
        headers=desk.admin,
        json={"default_skill_group_id": group_id},
    )
    assert response.status_code == 200, response.text

    first = await desk.visitor()
    await desk.say(first, "售后问题")
    assert (await desk.session_of(first))["assignee_id"] == alice.staff_id

    # Alice 离开后，组外的 Bob 虽然在线也不会分到。
    await desk.set_status(alice, "away")
    second = await desk.visitor()
    await desk.say(second, "售后问题 2")
    chat = await desk.session_of(second)
    assert chat["status"] == "queued"
    assert str(chat["skill_group_id"]) == group_id


async def test_agent_reply_and_close(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")

    await desk.reply(alice, visitor, "您好，请问有什么可以帮您？")

    chat = await desk.session_of(visitor)
    assert chat["first_response_at"] is not None
    assert chat["last_agent_message_at"] == chat["first_response_at"]

    response = await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=alice.headers)
    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["close_reason"]) == ("closed", "agent")
    await desk.flush()
    assert alice.im_user not in desk.members(visitor)
    assert desk.notices(visitor)[-1] == "本次会话已结束，感谢您的咨询。"
    assert [s["type"] for s in desk.im.signals_to(alice.im_user)] == [
        "session.assigned",
        "session.closed",
    ]
    assert await desk.events_of(chat["id"]) == ["created", "queued", "assigned", "closed"]
    # 结束后的系统提示仍归入这个会话；再次关闭是幂等的。
    [last] = await desk.sql("SELECT session_id FROM messages ORDER BY sent_at DESC LIMIT 1")
    assert last["session_id"] == chat["id"]
    response = await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=alice.headers)
    assert response.status_code == 200

    # 客户不归属 Alice：会话结束后 Alice 看不到这个客户。
    response = await desk.client.get(
        f"/api/v1/customers/{chat['customer_id']}", headers=alice.headers
    )
    assert response.status_code == 404

    # 客户再发消息时开始新的会话。
    await desk.say(visitor, "我还有一个问题")
    assert (await desk.session_of(visitor))["id"] != chat["id"]


async def test_agent_cannot_close_someone_elses_session(desk: Desk) -> None:
    alice = await desk.agent("alice")
    bob = await desk.agent("bob", online=False)
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == alice.staff_id

    response = await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=bob.headers)

    # 看不到别人的会话：与不存在一样返回 404。
    assert response.status_code == 404
    response = await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=desk.admin)
    assert response.status_code == 200


async def test_queue_timeout_turns_into_a_ticket(desk: Desk) -> None:
    [policy] = (await desk.client.get("/api/v1/routing-policies", headers=desk.admin)).json()[
        "items"
    ]
    await desk.client.patch(
        f"/api/v1/routing-policies/{policy['id']}",
        headers=desk.admin,
        json={"max_wait_seconds": 60},
    )
    visitor = await desk.visitor()
    await desk.say(visitor, "请问能开发票吗")
    await desk.say(visitor, "急用")
    chat = await desk.session_of(visitor)

    report = await run_session_timers(desk.ctx, now=chat["queued_at"] + timedelta(seconds=30))
    assert report.queue_timeouts == 0

    report = await run_session_timers(desk.ctx, now=chat["queued_at"] + timedelta(seconds=61))
    await desk.flush()

    assert report.queue_timeouts == 1
    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["close_reason"]) == ("closed", "leave_message")
    assert desk.notices(visitor)[-1] == "当前咨询较多，您的问题已登记为留言，我们会尽快联系您。"
    response = await desk.client.get("/api/v1/tickets?status=open", headers=desk.admin)
    [ticket] = response.json()["items"]
    assert (ticket["source"], ticket["content"], ticket["session_id"]) == (
        "queue_timeout",
        "请问能开发票吗\n急用",
        str(chat["id"]),
    )

    response = await desk.client.post(f"/api/v1/tickets/{ticket['id']}/done", headers=desk.admin)
    assert response.json()["status"] == "done"


async def test_idle_session_is_closed(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    await desk.reply(alice, visitor, "您好")
    chat = await desk.session_of(visitor)
    # 心跳保持在线，只验证空闲结束。
    await desk.sql("UPDATE agent_states SET last_seen_at = now() + interval '1 day'")

    report = await run_session_timers(
        desk.ctx, now=chat["last_agent_message_at"] + timedelta(minutes=31)
    )
    await desk.flush()

    assert report.idle_closed == 1
    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["close_reason"]) == ("closed", "idle_timeout")
    assert alice.im_user not in desk.members(visitor)
    assert desk.notices(visitor)[-1].startswith("由于长时间没有新消息")


async def test_disconnected_agent_goes_offline_and_unanswered_sessions_are_requeued(
    desk: Desk,
) -> None:
    alice = await desk.agent("alice")
    answered, unanswered = await desk.visitor(), await desk.visitor()
    await desk.say(answered, "第一位")
    await desk.say(unanswered, "第二位")
    await desk.reply(alice, answered, "您好")

    report = await run_session_timers(desk.ctx, now=datetime.now(UTC) + timedelta(minutes=2))
    await desk.flush()

    assert (report.agents_offline, report.requeued_rooms) == (1, 1)
    [state] = await desk.sql("SELECT status FROM agent_states")
    assert state["status"] == "offline"
    # 已回复的会话保留给 Alice；没回复过的退回队列，优先级提高，Alice 被移出服务群。
    assert (await desk.session_of(answered))["status"] == "human_serving"
    chat = await desk.session_of(unanswered)
    assert (chat["status"], chat["assignee_id"], chat["priority"]) == ("queued", None, 1)
    assert alice.im_user not in desk.members(unanswered)
    assert [s["type"] for s in desk.im.signals_to(alice.im_user)][-1] == "session.revoked"


async def test_heartbeat_keeps_the_agent_online(desk: Desk) -> None:
    alice = await desk.agent("alice")
    response = await desk.client.post("/api/v1/agent/heartbeat", headers=alice.headers)
    assert response.status_code == 200
    seen = datetime.fromisoformat(response.json()["last_seen_at"])

    report = await run_session_timers(desk.ctx, now=seen + timedelta(seconds=60))

    assert report.agents_offline == 0


async def test_off_hours_messages_become_one_ticket(desk: Desk) -> None:
    [policy] = (await desk.client.get("/api/v1/routing-policies", headers=desk.admin)).json()[
        "items"
    ]
    response = await desk.client.patch(
        f"/api/v1/routing-policies/{policy['id']}",
        headers=desk.admin,
        json={"business_hours": {"tz": "Asia/Shanghai", "days": {}}},
    )
    assert response.status_code == 200, response.text
    await desk.agent("alice")
    visitor = await desk.visitor()

    await desk.say(visitor, "晚上好")
    await desk.say(visitor, "明天联系我")

    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["close_reason"], chat["assignee_id"]) == (
        "closed",
        "leave_message",
        None,
    )
    assert desk.notices(visitor) == [
        "您好，现在是非工作时间。您的留言已记录，我们会在工作时间尽快联系您。"
    ]
    [ticket] = await desk.sql("SELECT source, content, session_id FROM tickets")
    assert (ticket["source"], ticket["content"], ticket["session_id"]) == (
        "off_hours",
        "晚上好\n明天联系我",
        chat["id"],
    )


async def test_im_operations_are_retried_in_order_when_openim_is_down(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    desk.im.send_as(visitor.user_id, visitor.group_id, "你好")
    desk.im.down = True

    await desk.flush()

    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", alice.staff_id)
    ops = await desk.sql("SELECT op, status, attempts FROM im_ops ORDER BY id")
    # 第一条失败后，后面的操作等它成功后再执行。
    assert [(o["op"], o["status"], o["attempts"]) for o in ops] == [
        ("invite", "pending", 1),
        ("notice", "pending", 0),
        ("signal", "pending", 0),
    ]

    desk.im.down = False
    done = await dispatch_due(desk.ctx, now=datetime.now(UTC) + timedelta(seconds=5))

    assert done == 3
    assert alice.im_user in desk.members(visitor)
    assert desk.notices(visitor) == ["客服 Alice 为您服务。"]
    assert len(desk.im.signals_to(alice.im_user)) == 1


async def test_messages_without_a_session_are_republished(desk: Desk) -> None:
    visitor = await desk.visitor()
    desk.im.send_as(visitor.user_id, visitor.group_id, "事件丢了")
    # 模拟事件发布失败：回调已入库，但事件流被清空。
    callbacks = list(desk.im.callbacks)
    desk.im.callbacks.clear()
    await deliver(desk.client, desk.settings, callbacks)
    await desk.ctx.redis.flushdb()
    [message] = await desk.sql("SELECT id, session_id, created_at FROM messages")
    assert message["session_id"] is None

    republished = await republish_orphans(
        desk.ctx, now=message["created_at"] + timedelta(seconds=31)
    )
    await desk.flush()

    assert republished == 1
    chat = await desk.session_of(visitor)
    [message] = await desk.sql("SELECT session_id FROM messages WHERE id = $1", message["id"])
    assert message["session_id"] == chat["id"]
