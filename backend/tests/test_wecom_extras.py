"""企业微信补充能力：离职继承与客户群继承（设计 §14.3）、客户群活码与群发任务（§10.4）、
微信客服菜单消息与语音（§10.3、§9.3）、侧边栏改标签与一键建群（§10.5）、专区结果（§10.6）。"""

import json
from typing import Any

import httpx
import pytest

from app.integrations.asr import AsrClient
from app.modules.kb.extraction import run_extraction
from app.modules.wecom import kf
from app.modules.wecom.contacts import poll_transfers
from app.modules.wecom.kf import AI_LABEL
from app.modules.wecom.marketing import poll_broadcasts
from app.modules.wecom.menus import CSAT_PROMPT, CSAT_THANKS, MENU_HANDOFF
from app.modules.wecom.zone import pull_zone_results
from tests.test_ai_reception import ANSWER, enable_ai
from tests.test_wecom_contacts import customer, sync, with_agents
from tests.test_wecom_kf import ready
from tests.wecom_desk import API, WecomDesk

GROUP = "wrgroup1"


async def with_group(wdesk: WecomDesk) -> None:
    """张三是客户群的群主，客户甲在群里。"""
    wdesk.wecom.add_group(GROUP, "甲总的服务群", owner="zhangsan", externals=["wmA"])
    await sync(wdesk, "contacts", "groups")


def summary(**changes: int) -> dict[str, int]:
    base = {
        "requested": 0,
        "skipped": 0,
        "failed": 0,
        "resigned": 0,
        "groups_transferred": 0,
        "groups_failed": 0,
    }
    return {**base, **changes}


# ---- 离职继承、客户群继承 ----


async def test_resigned_member_customers_are_assigned(wdesk: WecomDesk) -> None:
    _, bob = await with_agents(wdesk)
    await with_group(wdesk)
    await wdesk.wecom.member_leaves("zhangsan")
    await wdesk.flush()
    [member] = await wdesk.sql("SELECT status FROM wecom_members WHERE userid = 'zhangsan'")
    assert member["status"] == "left"

    response = await wdesk.client.get(f"{API}/unassigned", headers=wdesk.admin)
    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    assert (item["handover_userid"], item["external_userid"]) == ("zhangsan", "wmA")
    assert (item["customer_name"], item["owner_name"]) == ("甲总", "Amy")

    response = await wdesk.client.post(
        f"{API}/unassigned/assign",
        headers=wdesk.admin,
        json={"handover_userid": "zhangsan", "to_owner_id": str(bob.staff_id)},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "transferred": 1,
        "wecom": summary(requested=1, resigned=1, groups_transferred=1),
    }
    assert (await customer(wdesk, "wmA"))["owner_id"] == bob.staff_id
    assert list(wdesk.wecom.corp.resigned_transfers) == [("wmA", "zhangsan", "lisi")]
    assert wdesk.wecom.corp.group_transfers == [
        {"chat_id": GROUP, "from": "zhangsan", "to": "lisi", "resigned": True}
    ]
    [group] = await wdesk.sql("SELECT owner_userid FROM wecom_group_chats")
    assert group["owner_userid"] == "lisi"
    [transfer] = await wdesk.sql("SELECT kind, status FROM wecom_transfers")
    assert (transfer["kind"], transfer["status"]) == ("resigned", "waiting")

    # 企业微信接替后回收结果：客户在企业微信里改由李四添加。
    wdesk.wecom.complete_transfers()
    assert await poll_transfers(wdesk.ctx) == 1
    [transfer] = await wdesk.sql("SELECT status FROM wecom_transfers")
    assert transfer["status"] == "success"
    info = (
        await wdesk.client.get(
            f"/api/v1/customers/{(await customer(wdesk, 'wmA'))['id']}/wecom",
            headers=bob.headers,
        )
    ).json()
    assert [f["userid"] for f in info["follows"] if not f["deleted"]] == ["lisi"]
    assert (await wdesk.client.get(f"{API}/unassigned", headers=wdesk.admin)).json() == {
        "items": []
    }
    records = await wdesk.client.get(f"{API}/group-transfers", headers=wdesk.admin)
    [record] = records.json()["items"]
    assert (record["group_name"], record["kind"], record["status"]) == (
        "甲总的服务群",
        "resigned",
        "success",
    )

    # 已经分配过的客户不能再分配。
    again = await wdesk.client.post(
        f"{API}/unassigned/assign",
        headers=wdesk.admin,
        json={
            "handover_userid": "zhangsan",
            "external_userids": ["wmA"],
            "to_owner_id": str(bob.staff_id),
        },
    )
    assert again.status_code == 409, again.text


async def test_handover_transfers_group_chats(wdesk: WecomDesk) -> None:
    amy, bob = await with_agents(wdesk)
    await with_group(wdesk)
    response = await wdesk.client.post(
        f"/api/v1/customers/handover/{amy.staff_id}",
        headers=wdesk.admin,
        json={"to_owner_id": str(bob.staff_id), "sync_wecom": True, "transfer_groups": True},
    )
    assert response.status_code == 200, response.text
    # 张三在职：客户在职继承，客户群转给李四（在职群主转移）。
    assert response.json()["wecom"] == summary(requested=1, groups_transferred=1)
    assert list(wdesk.wecom.corp.transfers) == [("wmA", "zhangsan", "lisi")]
    assert wdesk.wecom.corp.group_transfers == [
        {"chat_id": GROUP, "from": "zhangsan", "to": "lisi", "resigned": False}
    ]


async def test_handover_of_resigned_member_uses_resigned_inheritance(wdesk: WecomDesk) -> None:
    amy, bob = await with_agents(wdesk)
    await with_group(wdesk)
    await wdesk.wecom.member_leaves("zhangsan")
    await wdesk.flush()
    response = await wdesk.client.post(
        f"/api/v1/customers/handover/{amy.staff_id}",
        headers=wdesk.admin,
        json={"to_owner_id": str(bob.staff_id), "sync_wecom": True, "transfer_groups": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["wecom"] == summary(requested=1, resigned=1, groups_transferred=1)
    assert wdesk.wecom.corp.transfers == {}
    assert list(wdesk.wecom.corp.resigned_transfers) == [("wmA", "zhangsan", "lisi")]
    assert wdesk.wecom.corp.group_transfers[0]["resigned"] is True


# ---- 客户群活码 ----


async def test_join_way_qr_code(wdesk: WecomDesk) -> None:
    amy, _ = await with_agents(wdesk)
    await with_group(wdesk)
    missing_name = await wdesk.client.post(
        f"{API}/join-ways",
        headers=wdesk.admin,
        json={"name": "活动", "chat_ids": [GROUP], "auto_create_room": True},
    )
    assert missing_name.status_code == 422
    response = await wdesk.client.post(
        f"{API}/join-ways",
        headers=wdesk.admin,
        json={
            "name": "国庆活动",
            "chat_ids": [GROUP],
            "auto_create_room": True,
            "room_base_name": "VIP 客户群",
            "room_base_id": 2,
        },
    )
    assert response.status_code == 201, response.text
    way = response.json()
    assert way["qr_code"].startswith("https://")
    assert way["state"].startswith("edp")
    [fake] = wdesk.wecom.corp.join_ways.values()
    assert (fake["scene"], fake["auto_create_room"], fake["room_base_id"]) == (2, 1, 2)

    # 客户扫码进群后，按 state 统计进群人数。
    wdesk.wecom.join_by_qr(way["config_id"], "wmNew")
    await wdesk.wecom.group_changed(GROUP)
    await wdesk.flush()
    [listed] = (await wdesk.client.get(f"{API}/join-ways", headers=wdesk.admin)).json()["items"]
    assert (listed["joined"], listed["group_names"]) == (1, ["甲总的服务群"])

    forbidden = await wdesk.client.get(f"{API}/join-ways", headers=amy.headers)
    assert forbidden.status_code == 403
    deleted = await wdesk.client.delete(f"{API}/join-ways/{way['id']}", headers=wdesk.admin)
    assert deleted.status_code == 204
    assert wdesk.wecom.corp.join_ways == {}


# ---- 群发任务 ----


async def test_broadcast_to_customers(wdesk: WecomDesk) -> None:
    amy, _ = await with_agents(wdesk)
    await sync(wdesk, "contacts")
    empty = await wdesk.client.post(
        "/api/v1/wecom/broadcasts",
        headers=wdesk.admin,
        json={"kind": "single", "title": "空", "content": "hi"},
    )
    assert empty.status_code == 422
    response = await wdesk.client.post(
        "/api/v1/wecom/broadcasts",
        headers=wdesk.admin,
        json={
            "kind": "single",
            "title": "国庆活动",
            "content": "国庆期间全场九折。",
            "link": {"title": "活动详情", "url": "https://example.com/sale"},
            "audience": {"tags": ["VIP"]},
        },
    )
    assert response.status_code == 201, response.text
    broadcast = response.json()
    assert (broadcast["status"], broadcast["target_count"]) == ("created", 1)
    [template] = wdesk.wecom.corp.templates.values()
    # 客户甲由归属坐席（张三）发送，附带活动链接。
    assert (template["sender"], list(template["results"])) == ("zhangsan", ["wmA"])
    assert template["attachments"][0]["link"]["url"] == "https://example.com/sale"

    # 员工确认发送后回收结果。
    wdesk.wecom.confirm_broadcast(template["msgid"])
    assert await poll_broadcasts(wdesk.ctx) == 1
    detail = (
        await wdesk.client.get(f"/api/v1/wecom/broadcasts/{broadcast['id']}", headers=wdesk.admin)
    ).json()
    assert detail["stats"]["sent"] == 1
    [member] = detail["members"]
    assert (member["userid"], member["name"], member["confirmed"], member["sent"]) == (
        "zhangsan",
        "张三",
        True,
        1,
    )

    # 发给两位客户：按归属坐席分别创建任务；停止后不能再发送。
    a, b = await customer(wdesk, "wmA"), await customer(wdesk, "wmB")
    second = await wdesk.client.post(
        "/api/v1/wecom/broadcasts",
        headers=wdesk.admin,
        json={
            "kind": "single",
            "title": "回访",
            "content": "最近使用还顺利吗？",
            "audience": {"customer_ids": [str(a["id"]), str(b["id"])]},
        },
    )
    assert second.json()["target_count"] == 2
    senders = sorted(t["sender"] for t in list(wdesk.wecom.corp.templates.values())[1:])
    assert senders == ["lisi", "zhangsan"]
    remind = await wdesk.client.post(
        f"/api/v1/wecom/broadcasts/{second.json()['id']}/remind", headers=wdesk.admin
    )
    assert remind.status_code == 204
    cancelled = await wdesk.client.post(
        f"/api/v1/wecom/broadcasts/{second.json()['id']}/cancel", headers=wdesk.admin
    )
    assert cancelled.json()["status"] == "cancelled"
    assert all(t["cancelled"] for t in list(wdesk.wecom.corp.templates.values())[1:])

    # 坐席没有群发权限。
    forbidden = await wdesk.client.get("/api/v1/wecom/broadcasts", headers=amy.headers)
    assert forbidden.status_code == 403
    listed = await wdesk.client.get("/api/v1/wecom/broadcasts", headers=wdesk.admin)
    assert [b["title"] for b in listed.json()["items"]] == ["回访", "国庆活动"]


async def test_broadcast_to_group_chats(wdesk: WecomDesk) -> None:
    await with_agents(wdesk)
    await with_group(wdesk)
    options = await wdesk.client.get("/api/v1/wecom/broadcast-options", headers=wdesk.admin)
    assert options.status_code == 200, options.text
    assert "VIP" in options.json()["tags"]
    assert {o["name"] for o in options.json()["owners"]} >= {"Amy", "Bob"}
    assert [g["chat_id"] for g in options.json()["group_chats"]] == [GROUP]
    response = await wdesk.client.post(
        "/api/v1/wecom/broadcasts",
        headers=wdesk.admin,
        json={
            "kind": "group",
            "title": "群公告",
            "content": "本群将于周五开展答疑活动。",
            "audience": {"chat_ids": [GROUP]},
        },
    )
    assert response.status_code == 201, response.text
    [template] = wdesk.wecom.corp.templates.values()
    assert (template["chat_type"], template["sender"]) == ("group", "zhangsan")
    wdesk.wecom.confirm_broadcast(template["msgid"])
    refreshed = await wdesk.client.post(
        f"/api/v1/wecom/broadcasts/{response.json()['id']}/refresh", headers=wdesk.admin
    )
    assert refreshed.json()["stats"]["sent"] == 1
    [row] = await wdesk.sql("SELECT chat_id, status FROM wecom_broadcast_results")
    assert (row["chat_id"], row["status"]) == (GROUP, 1)


# ---- 微信客服：菜单消息、语音 ----


async def test_ai_reply_offers_a_handoff_button(wdesk: WecomDesk) -> None:
    agent = await ready(wdesk)
    await enable_ai(wdesk)
    await wdesk.customer_says("快递几天能到")
    assert wdesk.wecom.sent_texts() == [f"{AI_LABEL}{ANSWER}"]
    assert wdesk.wecom.sent_menus() == [[MENU_HANDOFF]]
    # 客户点「转人工」：会话转入人工接待。
    await wdesk.wecom.customer_clicks(MENU_HANDOFF)
    await wdesk.flush()
    chat = await wdesk.kf_session()
    assert (chat["status"], chat["handoff_reason"]) == ("human_serving", "customer_request")
    assert chat["assignee_id"] == agent.staff_id
    [clicked] = await wdesk.sql(
        "SELECT content FROM messages WHERE content->>'menu_id' = $1", MENU_HANDOFF
    )
    assert json.loads(clicked["content"])["text"] == "转人工"

    # 关闭按钮后 AI 的回复是普通文字消息。
    await wdesk.client.put(f"{API}/settings", headers=wdesk.admin, json={"kf_handoff_menu": False})
    await wdesk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=agent.headers)
    await wdesk.flush()
    await wdesk.customer_says("快递几天能到")
    assert wdesk.wecom.sent[-1]["msgtype"] == "text"


async def test_csat_buttons_after_human_service(wdesk: WecomDesk) -> None:
    agent = await ready(wdesk)
    await wdesk.customer_says("你好，想问下发票")
    chat = await wdesk.kf_session()
    assert chat["status"] == "human_serving"
    response = await wdesk.client.post(
        f"/api/v1/sessions/{chat['id']}/close", headers=agent.headers
    )
    assert response.status_code == 200, response.text
    await wdesk.flush()
    assert wdesk.wecom.sent_texts()[-1].endswith(CSAT_PROMPT)
    assert wdesk.wecom.sent_menus()[-1] == [f"edp_csat_{n}" for n in (5, 4, 3, 2, 1)]

    await wdesk.wecom.customer_clicks("edp_csat_4")
    await wdesk.flush()
    sessions = await wdesk.sql(
        "SELECT id, csat FROM sessions WHERE room_id = $1", await wdesk.kf_room()
    )
    # 评价记在刚结束的会话上，不开始新的会话。
    assert [(s["id"], s["csat"]) for s in sessions] == [(chat["id"], 4)]
    assert wdesk.wecom.sent_texts()[-1] == CSAT_THANKS
    events = await wdesk.events_of(chat["id"])
    assert "csat" in events


async def test_voice_is_transcribed_and_converted(
    wdesk: WecomDesk, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_mp3(data: bytes, ffmpeg: str = "ffmpeg") -> bytes:
        return b"MP3:" + data

    monkeypatch.setattr(kf, "to_mp3", fake_mp3)
    requests: list[httpx.Request] = []

    def asr(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": "我想查一下订单到哪了"})

    wdesk.ctx.asr = AsrClient(
        base_url="http://asr/v1", api_key="k", model="asr-1", transport=httpx.MockTransport(asr)
    )
    await ready(wdesk)
    await wdesk.wecom.customer_voice(b"#!AMR\nfake-voice")
    await wdesk.flush()
    [row] = await wdesk.sql("SELECT content, text_plain FROM messages WHERE content_type = 'voice'")
    content: dict[str, Any] = json.loads(row["content"])
    assert row["text_plain"] == "我想查一下订单到哪了"
    assert (content["mime"], content["transcript"]) == ("audio/mpeg", "我想查一下订单到哪了")
    assert content["name"].endswith(".mp3")
    assert any(data == b"MP3:#!AMR\nfake-voice" for data, _ in wdesk.storage.objects.values())
    assert requests and b'name="model"' in requests[0].content


# ---- 侧边栏 ----


async def test_sidebar_edits_tags_and_creates_groups(wdesk: WecomDesk) -> None:
    amy, bob = await with_agents(wdesk)
    await sync(wdesk, "contacts")
    a = await customer(wdesk, "wmA")
    response = await wdesk.client.put(
        f"/api/v1/sidebar/customers/{a['id']}/tags",
        headers=amy.headers,
        json={"tags": ["VIP", "高意向", "老客户"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["tags"] == ["VIP", "高意向", "老客户"]
    # 企业标签「高意向」写回企业微信。
    assert wdesk.wecom.marked[-1]["add_tag"] == ["tag-hot"]
    forbidden = await wdesk.client.put(
        f"/api/v1/sidebar/customers/{a['id']}/tags", headers=bob.headers, json={"tags": []}
    )
    assert forbidden.status_code == 404

    tags = await wdesk.client.get("/api/v1/sidebar/tags", headers=amy.headers)
    assert tags.json()["items"] == ["VIP", "新客户", "高意向"]
    members = await wdesk.client.get("/api/v1/sidebar/members", headers=amy.headers)
    assert {(m["userid"], m["staff_name"]) for m in members.json()["items"]} >= {
        ("zhangsan", "Amy"),
        ("lisi", "Bob"),
    }

    # 员工在侧边栏一键建群（JS-SDK），把群 ID 告诉平台：立即同步，群出现在客户档案里。
    chat_id = await wdesk.wecom.create_chat("zhangsan", ["lisi"], ["wmA"], "甲总的服务群")
    created = await wdesk.client.post(
        "/api/v1/sidebar/groups",
        headers=amy.headers,
        json={"chat_id": chat_id, "external_userid": "wmA"},
    )
    assert created.status_code == 200, created.text
    assert (created.json()["name"], created.json()["member_count"]) == ("甲总的服务群", 3)
    context = await wdesk.client.get(
        "/api/v1/sidebar/context", headers=amy.headers, params={"external_userid": "wmA"}
    )
    assert [g["chat_id"] for g in context.json()["wecom"]["group_chats"]] == [chat_id]


async def test_sidebar_answers_become_knowledge_candidates(wdesk: WecomDesk) -> None:
    amy, _ = await with_agents(wdesk)
    await sync(wdesk, "contacts")
    response = await wdesk.client.post(
        "/api/v1/sidebar/sent",
        headers=amy.headers,
        json={
            "content": "可以开电子专票，下单后在订单详情里申请。",
            "external_userid": "wmA",
            "question": "可以开电子专票吗？",
        },
    )
    assert response.status_code == 204, response.text
    await run_extraction(wdesk.ctx)
    rows = await wdesk.sql("SELECT question, answer, source, evidence FROM kb_candidates")
    assert [(r["question"], r["source"]) for r in rows] == [("可以开电子专票吗？", "sidebar")]
    [evidence] = json.loads(rows[0]["evidence"])
    assert evidence["session_id"] is None and evidence["sidebar_message_id"]
    [sent] = await wdesk.sql("SELECT extracted_at FROM wecom_sidebar_messages")
    assert sent["extracted_at"] is not None


# ---- 数据与智能专区 ----


async def test_zone_results_are_pulled(wdesk: WecomDesk) -> None:
    amy, _ = await with_agents(wdesk)
    await with_group(wdesk)
    assert await pull_zone_results(wdesk.ctx) == 0  # 没有开启专区
    response = await wdesk.client.put(
        f"{API}/settings",
        headers=wdesk.admin,
        json={"zone_enabled": True, "zone_program_id": "prog-1", "zone_ability_id": "analyze"},
    )
    assert response.status_code == 200, response.text
    assert await pull_zone_results(wdesk.ctx) == 3
    a = await customer(wdesk, "wmA")
    info = (
        await wdesk.client.get(f"/api/v1/customers/{a['id']}/wecom", headers=amy.headers)
    ).json()
    [group] = info["group_chats"]
    assert "团购" in group["summary"] and group["sentiment"] == "平稳"
    rows = await wdesk.sql("SELECT question, source FROM kb_candidates")
    assert [(r["question"], r["source"]) for r in rows] == [("团购满多少人有优惠？", "zone")]
    # 下一次从上次取到的时间继续。
    await pull_zone_results(wdesk.ctx)
    first, second = wdesk.wecom.zone_calls
    assert second["since"] == first["until"]
    status = await wdesk.status()
    assert status["sync_state"]["zone"]["count"] == 3
