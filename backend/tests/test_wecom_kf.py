"""微信客服（设计文档 §10.3）：入站同步与镜像、坐席和 AI 回复的投递、48 小时 5 条的回复窗口。"""

import json
import time
import uuid
from datetime import timedelta
from typing import Any

from app.modules.conversation.outbox import dispatch_due
from app.modules.wecom.kf import AI_LABEL, WINDOW_EXPIRED, WINDOW_USED_UP, sync_all
from tests.desk import Agent
from tests.test_ai_reception import ANSWER, enable_ai
from tests.wecom_desk import WecomDesk

CUSTOMER = "wmcust0001"
KF = "wkfake0001"


async def ready(wdesk: WecomDesk) -> Agent:
    """授权企业微信，准备一位在线坐席，客户有昵称。"""
    wdesk.wecom.add_kf_customer(CUSTOMER, "王小明", unionid="union-1")
    await wdesk.authorize()
    return await wdesk.agent("amy")


def mirrored(wdesk: WecomDesk, room_id: uuid.UUID) -> list[tuple[str, str]]:
    """服务群里的文本消息：(发送者类别, 内容)。"""
    group = wdesk.im.groups[f"{wdesk.code}_r_{room_id.hex}"]
    kinds = {"c": "customer", "s": "agent", "bot": "bot", "sys": "system"}
    result = []
    for m in group.messages:
        if m.content_type != 101:
            continue
        rest = m.send_id.split("_", 1)[1]
        result.append((kinds[rest.split("_")[0]], json.loads(m.content)["content"]))
    return result


async def send(wdesk: WecomDesk, agent: Agent, session_id: Any, text: str) -> Any:
    return await wdesk.client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=agent.headers,
        json={"client_msg_id": uuid.uuid4().hex, "type": "text", "text": text},
    )


async def window(wdesk: WecomDesk, agent: Agent, session_id: Any) -> dict[str, Any]:
    response = await wdesk.client.get(
        f"/api/v1/sessions/{session_id}/reply-window", headers=agent.headers
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_customer_message_becomes_a_session_and_agent_replies(wdesk: WecomDesk) -> None:
    agent = await ready(wdesk)
    await wdesk.customer_says("你好，想问下发货时间")

    [customer] = await wdesk.sql("SELECT * FROM customers WHERE source_channel = 'wecom_kf'")
    assert customer["display_name"] == "王小明"
    [identity] = await wdesk.sql(
        "SELECT * FROM customer_identities WHERE external_id = $1", CUSTOMER
    )
    assert json.loads(identity["profile"])["unionid"] == "union-1"
    chat = await wdesk.kf_session()
    assert chat["status"] == "human_serving"
    assert chat["assignee_id"] == agent.staff_id
    # 入站消息按 msgid 入库，以客户身份镜像到服务群，不重复入库。
    [message] = await wdesk.sql("SELECT * FROM messages WHERE sender_type = 'customer'")
    assert message["ext_msg_id"].startswith("kfmsg")
    assert message["source"] == "channel"
    assert message["channel_msg_id"] is not None
    room_id = await wdesk.kf_room()
    assert ("customer", "你好，想问下发货时间") in mirrored(wdesk, room_id)
    # 新会话置为由智能助手接待（1），不转为人工接待（3）。
    assert wdesk.wecom.corp.kf_states[(KF, CUSTOMER)] == 1

    response = await send(wdesk, agent, chat["id"], "一般 2 到 3 天送达")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["send_status"] == "sent"
    await wdesk.flush()
    # 分配提示（系统消息）也投递给客户，占用额度。
    assert wdesk.wecom.sent_texts() == ["客服 Amy 为您服务。", "一般 2 到 3 天送达"]
    assert wdesk.wecom.sent[-1]["open_kfid"] == KF
    assert ("agent", "一般 2 到 3 天送达") in mirrored(wdesk, room_id)
    rows = await wdesk.sql(
        "SELECT send_status, ext_msg_id FROM messages WHERE sender_type = 'agent'"
    )
    assert [(r["send_status"], r["ext_msg_id"] is not None) for r in rows] == [("sent", True)]


async def test_reply_window_limits_five_messages_until_customer_replies(wdesk: WecomDesk) -> None:
    agent = await ready(wdesk)
    await wdesk.customer_says("在吗")
    chat = await wdesk.kf_session()
    before = await window(wdesk, agent, chat["id"])
    assert before["limited"] is True and before["limit"] == 5
    # 分配提示已经用掉 1 条。
    assert before["remaining"] == 4
    for i in range(4):
        response = await send(wdesk, agent, chat["id"], f"第 {i + 1} 条")
        assert response.status_code == 200, response.text
    assert (await window(wdesk, agent, chat["id"]))["open"] is False
    response = await send(wdesk, agent, chat["id"], "第 5 条")
    assert response.status_code == 409
    assert response.json()["error"]["message"] == WINDOW_USED_UP

    # 客户再发消息（时间精确到秒，这里放在下一秒）后额度重置。
    wdesk.wecom.kf_message(
        KF, CUSTOMER, "text", {"content": "好的，还有问题"}, send_time=int(time.time()) + 1
    )
    await wdesk.wecom.notify_kf(KF)
    await wdesk.flush()
    after = await window(wdesk, agent, chat["id"])
    assert after["open"] is True and after["remaining"] == 5
    assert (await send(wdesk, agent, chat["id"], "请讲")).status_code == 200


async def test_reply_window_expires_after_48_hours(wdesk: WecomDesk) -> None:
    agent = await ready(wdesk)
    await wdesk.customer_says("在吗")
    chat = await wdesk.kf_session()
    await wdesk.sql(
        "UPDATE messages SET sent_at = sent_at - $1::interval WHERE sender_type = 'customer'",
        timedelta(hours=49),
    )
    result = await window(wdesk, agent, chat["id"])
    assert result["open"] is False
    assert result["reason"] == WINDOW_EXPIRED
    response = await send(wdesk, agent, chat["id"], "抱歉回复晚了")
    assert response.status_code == 409
    assert response.json()["error"]["message"] == WINDOW_EXPIRED


async def test_web_sessions_have_no_reply_window(wdesk: WecomDesk) -> None:
    agent = await wdesk.agent("amy")
    visitor = await wdesk.visitor()
    await wdesk.say(visitor, "你好")
    chat = await wdesk.session_of(visitor)
    assert (await window(wdesk, agent, chat["id"]))["limited"] is False


async def test_ai_replies_to_wechat_customers_with_a_label(wdesk: WecomDesk) -> None:
    await ready(wdesk)
    await enable_ai(wdesk)
    await wdesk.customer_says("快递几天能到")
    chat = await wdesk.kf_session()
    assert chat["status"] == "ai_serving"
    assert wdesk.wecom.sent_texts() == [f"{AI_LABEL}{ANSWER}"]
    [bot] = await wdesk.sql("SELECT * FROM messages WHERE sender_type = 'bot'")
    assert bot["send_status"] == "sent"
    assert bot["text_plain"] == ANSWER
    assert ("bot", ANSWER) in mirrored(wdesk, await wdesk.kf_room())


async def test_welcome_message_on_enter_session(wdesk: WecomDesk) -> None:
    agent = await ready(wdesk)
    channel = await wdesk.kf_channel()
    response = await wdesk.client.patch(
        f"/api/v1/channels/{channel['id']}",
        headers=wdesk.admin,
        json={"kf": {"welcome_message": "您好，我是官方客服，请问有什么可以帮您？"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["kf"]["welcome_message"].startswith("您好")

    await wdesk.wecom.customer_enters()
    await wdesk.flush()
    [reply] = wdesk.wecom.event_replies
    assert reply["text"]["content"] == "您好，我是官方客服，请问有什么可以帮您？"
    [identity] = await wdesk.sql("SELECT profile FROM customer_identities")
    profile = json.loads(identity["profile"])
    assert profile["entry"]["scene"] == "website"
    # 客户进入会话时就取到了昵称（还没发消息）。
    assert profile["nickname"] == "王小明"
    [welcome] = await wdesk.sql("SELECT * FROM messages WHERE sender_type = 'system'")
    assert json.loads(welcome["content"])["welcome"] is True

    # 欢迎语不占 5 条的额度。
    await wdesk.customer_says("你好")
    chat = await wdesk.kf_session()
    assert (await window(wdesk, agent, chat["id"]))["remaining"] == 4


async def test_duplicate_callbacks_and_history_are_not_imported_twice(wdesk: WecomDesk) -> None:
    # 授权前的历史消息不导入。
    wdesk.wecom.kf_message(KF, CUSTOMER, "text", {"content": "很早以前"}, send_time=1_600_000_000)
    await ready(wdesk)
    await wdesk.customer_says("第一条")
    await wdesk.wecom.notify_kf(KF)
    await wdesk.wecom.notify_kf(KF)
    await wdesk.flush()
    rows = await wdesk.sql("SELECT text_plain FROM messages WHERE sender_type = 'customer'")
    assert [r["text_plain"] for r in rows] == ["第一条"]
    [account] = await wdesk.sql("SELECT cursor FROM wecom_kf_accounts")
    assert account["cursor"] == "2"


async def test_fallback_sync_picks_up_messages_without_callback(wdesk: WecomDesk) -> None:
    await ready(wdesk)
    wdesk.wecom.kf_message(KF, CUSTOMER, "text", {"content": "回调丢了"})
    assert await sync_all(wdesk.ctx) == 1
    await wdesk.flush()
    assert (await wdesk.kf_session())["status"] == "human_serving"


async def test_media_is_stored_and_agent_images_are_uploaded(wdesk: WecomDesk) -> None:
    agent = await ready(wdesk)
    media_id = wdesk.wecom.add_media(b"\x89PNG-fake", "image/png", "photo.png")
    wdesk.wecom.kf_message(KF, CUSTOMER, "image", {"media_id": media_id})
    await wdesk.wecom.notify_kf(KF)
    await wdesk.flush()
    [image] = await wdesk.sql("SELECT * FROM messages WHERE sender_type = 'customer'")
    assert image["content_type"] == "image"
    content = json.loads(image["content"])
    assert content["url"].startswith("http://testserver/api/v1/files/acme/wecom/")
    assert content["mime"] == "image/png"
    assert any(data == b"\x89PNG-fake" for data, _ in wdesk.storage.objects.values())

    chat = await wdesk.kf_session()
    response = await wdesk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages",
        headers=agent.headers,
        json={
            "client_msg_id": uuid.uuid4().hex,
            "type": "image",
            "attachment": {
                "url": content["url"],
                "name": "photo.png",
                "size": 9,
                "content_type": "image/png",
            },
        },
    )
    assert response.status_code == 200, response.text
    sent = wdesk.wecom.sent[-1]
    assert sent["msgtype"] == "image"
    assert wdesk.wecom.uploaded[sent["image"]["media_id"]][0].count(b"\x89PNG-fake") == 1


async def test_customers_still_get_replies_when_openim_is_down(wdesk: WecomDesk) -> None:
    wdesk.wecom.add_kf_customer(CUSTOMER, "王小明")
    await wdesk.authorize()
    await enable_ai(wdesk)
    wdesk.im.down = True
    await wdesk.customer_says("快递几天能到")
    # 入站消息照常入库，AI 回复照常送达；镜像等 OpenIM 恢复后补上。
    rows = await wdesk.sql("SELECT text_plain FROM messages WHERE sender_type = 'customer'")
    assert [r["text_plain"] for r in rows] == ["快递几天能到"]
    assert wdesk.wecom.sent_texts() == [f"{AI_LABEL}{ANSWER}"]
    room_id = await wdesk.kf_room()
    assert f"{wdesk.code}_r_{room_id.hex}" not in wdesk.im.groups

    wdesk.im.down = False
    await wdesk.sql("UPDATE im_ops SET next_attempt_at = now() WHERE status = 'pending'")
    await dispatch_due(wdesk.ctx)
    await wdesk.flush()
    assert [text for _, text in mirrored(wdesk, room_id)] == ["快递几天能到", ANSWER]
    unlinked = await wdesk.sql("SELECT 1 FROM messages WHERE channel_msg_id IS NULL")
    assert unlinked == []
    # AI 回复只投递了一次。
    assert wdesk.wecom.sent_texts() == [f"{AI_LABEL}{ANSWER}"]


async def test_send_failure_event_marks_the_message(wdesk: WecomDesk) -> None:
    agent = await ready(wdesk)
    await wdesk.customer_says("在吗")
    chat = await wdesk.kf_session()
    body = (await send(wdesk, agent, chat["id"], "在的")).json()
    [row] = await wdesk.sql("SELECT ext_msg_id FROM messages WHERE id = $1", uuid.UUID(body["id"]))
    message = wdesk.wecom.kf_message(KF, CUSTOMER, "event", {}, origin=4)
    message["event"] = {
        "event_type": "msg_send_fail",
        "fail_msgid": row["ext_msg_id"],
        "fail_type": 10,
    }
    await wdesk.wecom.notify_kf(KF)
    await wdesk.flush()
    response = await wdesk.client.get(
        f"/api/v1/sessions/{chat['id']}/messages", headers=agent.headers
    )
    failed = [m for m in response.json()["items"] if m["id"] == body["id"]]
    assert failed[0]["send_status"] == "failed"
    assert "客户拒收" in failed[0]["send_error"]


async def test_wechat_customer_merges_with_external_contact(wdesk: WecomDesk) -> None:
    wdesk.wecom.add_contact(CUSTOMER, "王小明", remark="王总")
    await ready(wdesk)
    await wdesk.customer_says("你好")
    customers = await wdesk.sql("SELECT id, display_name FROM customers WHERE display_name <> ''")
    identities = await wdesk.sql("SELECT DISTINCT customer_id FROM customer_identities")
    assert len(identities) == 1
    assert [c["display_name"] for c in customers] == ["王总"]
