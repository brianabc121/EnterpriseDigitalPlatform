"""企业微信客户联系与客户群（设计文档 §10.4）、在职继承（§14.3）、应用消息提醒。"""

import json
from typing import Any

from app.modules.wecom.contacts import poll_transfers
from tests.desk import Agent
from tests.wecom_desk import API, WecomDesk


async def with_agents(wdesk: WecomDesk) -> tuple[Agent, Agent]:
    """授权企业微信，张三、李四分别绑定坐席 amy、bob。"""
    wdesk.wecom.add_contact("wmA", "客户甲", userid="zhangsan", remark="甲总", tags=["tag-vip"])
    wdesk.wecom.add_contact("wmB", "客户乙", userid="lisi")
    await wdesk.authorize()
    amy = await wdesk.agent("amy")
    bob = await wdesk.agent("bob")
    await wdesk.bind("zhangsan", amy)
    await wdesk.bind("lisi", bob)
    return amy, bob


async def customer(wdesk: WecomDesk, external_userid: str) -> dict[str, Any]:
    [row] = await wdesk.sql(
        "SELECT c.* FROM customers c JOIN customer_identities i ON i.customer_id = c.id "
        "WHERE i.external_id = $1",
        external_userid,
    )
    return dict(row)


async def sync(wdesk: WecomDesk, *targets: str) -> None:
    response = await wdesk.client.post(
        f"{API}/sync", headers=wdesk.admin, json={"targets": list(targets)}
    )
    assert response.status_code == 202, response.text
    await wdesk.flush()


async def test_members_and_binding(wdesk: WecomDesk) -> None:
    amy, _ = await with_agents(wdesk)
    response = await wdesk.client.get(f"{API}/members", headers=wdesk.admin)
    members = {m["userid"]: m for m in response.json()["items"]}
    assert members["zhangsan"]["name"] == "张三"
    assert members["zhangsan"]["follow"] is True
    assert members["zhangsan"]["staff_name"] == "Amy"
    assert members["admin"]["follow"] is False
    # 一个成员只能绑定一位员工；改绑时原绑定自动解除。
    await wdesk.bind("admin", amy)
    response = await wdesk.client.get(f"{API}/members", headers=wdesk.admin)
    members = {m["userid"]: m for m in response.json()["items"]}
    assert members["admin"]["staff_name"] == "Amy"
    assert members["zhangsan"]["staff_id"] is None


async def test_full_sync_creates_customers_with_owner_and_tags(wdesk: WecomDesk) -> None:
    amy, bob = await with_agents(wdesk)
    # 授权时成员还没有绑定员工：重新同步后，添加人成为归属坐席。
    await sync(wdesk, "contacts")
    a = await customer(wdesk, "wmA")
    assert a["display_name"] == "甲总"
    assert a["source_channel"] == "wecom_contact"
    assert a["owner_id"] == amy.staff_id
    assert a["tags"] == ["VIP"]
    assert (await customer(wdesk, "wmB"))["owner_id"] == bob.staff_id
    history = await wdesk.sql("SELECT reason FROM customer_owner_history")
    assert {h["reason"] for h in history} == {"wecom"}
    # 坐席只看到自己的客户。
    response = await wdesk.client.get("/api/v1/customers", headers=amy.headers)
    assert [c["display_name"] for c in response.json()["items"]] == ["甲总"]
    # 客户 360：添加人和标签。
    info = await wdesk.client.get(f"/api/v1/customers/{a['id']}/wecom", headers=amy.headers)
    assert info.status_code == 200, info.text
    body = info.json()
    assert body["external_userid"] == "wmA"
    assert [(f["userid"], f["staff_name"], f["tags"]) for f in body["follows"]] == [
        ("zhangsan", "Amy", ["VIP"])
    ]
    forbidden = await wdesk.client.get(f"/api/v1/customers/{a['id']}/wecom", headers=bob.headers)
    assert forbidden.status_code == 404


async def test_new_contact_callback_and_welcome_message(wdesk: WecomDesk) -> None:
    amy, _ = await with_agents(wdesk)
    status = await wdesk.status()
    open_kfid = status["kf_accounts"][0]["open_kfid"]
    response = await wdesk.client.put(
        f"{API}/settings",
        headers=wdesk.admin,
        json={
            "welcome_enabled": True,
            "welcome_text": "您好，我是张三，很高兴为您服务！",
            "welcome_kf_id": open_kfid,
        },
    )
    assert response.status_code == 200, response.text

    code = await wdesk.wecom.staff_adds_contact("wmC", "客户丙", userid="zhangsan")
    await wdesk.flush()
    c = await customer(wdesk, "wmC")
    assert c["owner_id"] == amy.staff_id
    [welcome] = wdesk.wecom.welcomes
    assert welcome["welcome_code"] == code
    assert welcome["text"]["content"] == "您好，我是张三，很高兴为您服务！"
    assert welcome["attachments"][0]["link"]["url"].endswith(f"{open_kfid}?enc_scene=edp")

    # 客户删除了员工：关系标记为已解除。
    await wdesk.wecom.contact_changed("wmC", change="del_follow_user")
    await wdesk.flush()
    [follow] = await wdesk.sql(
        "SELECT deleted_at FROM wecom_contact_follows WHERE external_userid = 'wmC'"
    )
    assert follow["deleted_at"] is not None


async def test_welcome_is_off_by_default(wdesk: WecomDesk) -> None:
    await with_agents(wdesk)
    await wdesk.wecom.staff_adds_contact("wmC", "客户丙")
    await wdesk.flush()
    assert wdesk.wecom.welcomes == []


async def test_platform_tags_are_written_back(wdesk: WecomDesk) -> None:
    amy, _ = await with_agents(wdesk)
    await sync(wdesk, "contacts")
    a = await customer(wdesk, "wmA")
    response = await wdesk.client.patch(
        f"/api/v1/customers/{a['id']}",
        headers=amy.headers,
        json={"tags": ["高意向", "老客户"]},
    )
    assert response.status_code == 200, response.text
    # 企业标签里有的写回企业微信（加"高意向"、去掉"VIP"），平台自有的"老客户"不写回。
    [marked] = wdesk.wecom.marked
    assert marked["add_tag"] == ["tag-hot"]
    assert marked["remove_tag"] == ["tag-vip"]
    assert wdesk.wecom.corp.follows[("wmA", "zhangsan")]["tag_id"] == ["tag-hot"]
    # 之后的同步不会把平台上的改动改回去。
    await sync(wdesk, "contacts")
    assert sorted((await customer(wdesk, "wmA"))["tags"]) == ["老客户", "高意向"]


async def test_group_chats_link_customers(wdesk: WecomDesk) -> None:
    amy, _ = await with_agents(wdesk)
    await sync(wdesk, "contacts")
    wdesk.wecom.add_group("wrgroup1", "甲总的服务群", owner="zhangsan", externals=["wmA"])
    await wdesk.wecom.group_changed("wrgroup1", "create")
    await wdesk.flush()
    a = await customer(wdesk, "wmA")
    body = (
        await wdesk.client.get(f"/api/v1/customers/{a['id']}/wecom", headers=amy.headers)
    ).json()
    assert [(g["name"], g["owner_name"], g["member_count"]) for g in body["group_chats"]] == [
        ("甲总的服务群", "张三", 2)
    ]
    await wdesk.wecom.group_changed("wrgroup1", "dismiss")
    await wdesk.flush()
    [group] = await wdesk.sql("SELECT status, member_count FROM wecom_group_chats")
    assert (group["status"], group["member_count"]) == ("dismissed", 0)


async def test_transfer_customers_with_wecom_inheritance(wdesk: WecomDesk) -> None:
    _, bob = await with_agents(wdesk)
    await sync(wdesk, "contacts")
    a = await customer(wdesk, "wmA")
    b = await customer(wdesk, "wmB")
    response = await wdesk.client.post(
        "/api/v1/customers/transfer",
        headers=wdesk.admin,
        json={
            "customer_ids": [str(a["id"]), str(b["id"])],
            "to_owner_id": str(bob.staff_id),
            "note": "张三调岗",
            "sync_wecom": True,
        },
    )
    assert response.status_code == 200, response.text
    # 客户乙本来就归 bob：没有变更。客户甲在企业微信里从张三转给李四。
    assert response.json() == {
        "transferred": 1,
        "wecom": {
            "requested": 1,
            "skipped": 0,
            "failed": 0,
            "resigned": 0,
            "groups_transferred": 0,
            "groups_failed": 0,
        },
    }
    history = await wdesk.client.get(
        f"/api/v1/customers/{a['id']}/owner-history", headers=wdesk.admin
    )
    assert history.json()["items"][0]["wecom_sync_status"] == "waiting"

    # 24 小时后企业微信自动接替；调度进程回收结果。
    wdesk.wecom.complete_transfers()
    assert await poll_transfers(wdesk.ctx) == 1
    history = await wdesk.client.get(
        f"/api/v1/customers/{a['id']}/owner-history", headers=wdesk.admin
    )
    assert history.json()["items"][0]["wecom_sync_status"] == "success"
    info = (
        await wdesk.client.get(f"/api/v1/customers/{a['id']}/wecom", headers=bob.headers)
    ).json()
    active = [f["userid"] for f in info["follows"] if not f["deleted"]]
    assert active == ["lisi"]
    assert [t["status"] for t in info["transfers"]] == ["success"]


async def test_transfer_without_binding_is_skipped(wdesk: WecomDesk) -> None:
    await with_agents(wdesk)
    await sync(wdesk, "contacts")
    carol = await wdesk.agent("carol")  # 没有绑定企业微信成员
    a = await customer(wdesk, "wmA")
    response = await wdesk.client.post(
        "/api/v1/customers/transfer",
        headers=wdesk.admin,
        json={
            "customer_ids": [str(a["id"])],
            "to_owner_id": str(carol.staff_id),
            "sync_wecom": True,
        },
    )
    assert response.json()["wecom"] == {
        "requested": 0,
        "skipped": 1,
        "failed": 0,
        "resigned": 0,
        "groups_transferred": 0,
        "groups_failed": 0,
    }
    assert wdesk.wecom.corp.transfers == {}


async def test_agents_get_app_messages(wdesk: WecomDesk) -> None:
    amy, bob = await with_agents(wdesk)
    wdesk.wecom.add_kf_customer("wmcust0001", "王小明")
    # 新会话分配：提醒接待的坐席，链接经网页授权进入控制台。
    await wdesk.set_status(bob, "offline")
    await wdesk.customer_says("你好")
    [card] = wdesk.wecom.app_messages
    assert card["touser"] == "zhangsan"
    assert card["textcard"]["title"] == "新会话分配"
    assert "王小明" in card["textcard"]["description"]
    assert card["textcard"]["url"].startswith("https://open.weixin.qq.com/connect/oauth2/")
    assert "wecom%2Flogin" in card["textcard"]["url"]

    # 转接请求：提醒目标坐席。
    await wdesk.set_status(bob, "online")
    chat = await wdesk.kf_session()
    response = await wdesk.client.post(
        f"/api/v1/sessions/{chat['id']}/transfer",
        headers=amy.headers,
        json={"to_staff_id": str(bob.staff_id), "note": "请帮忙跟进"},
    )
    assert response.status_code == 200, response.text
    await wdesk.flush()
    assert wdesk.wecom.app_messages[-1]["touser"] == "lisi"
    assert wdesk.wecom.app_messages[-1]["textcard"]["title"] == "会话转接请求"

    # 关闭提醒后不再发送。
    await wdesk.client.put(f"{API}/settings", headers=wdesk.admin, json={"notify_agents": False})
    sent = len(wdesk.wecom.app_messages)
    await wdesk.customer_says("还在吗", external_userid="wmcust0002")
    assert len(wdesk.wecom.app_messages) == sent


async def test_must_read_knowledge_is_announced(wdesk: WecomDesk) -> None:
    await with_agents(wdesk)
    response = await wdesk.client.post(
        "/api/v1/kb/items",
        headers=wdesk.admin,
        json={
            "title": "十一假期发货安排",
            "content": "10 月 1 日至 7 日暂停发货。",
            "must_read": True,
            "publish": True,
        },
    )
    assert response.status_code == 201, response.text
    await wdesk.flush()
    [card] = wdesk.wecom.app_messages
    assert card["textcard"]["title"] == "必读知识：十一假期发货安排"
    assert set(card["touser"].split("|")) == {"zhangsan", "lisi"}


async def test_contact_events_for_other_tenants_are_isolated(wdesk: WecomDesk) -> None:
    await with_agents(wdesk)
    rows = await wdesk.sql("SELECT DISTINCT tenant_id FROM wecom_contact_follows")
    assert [r["tenant_id"] for r in rows] == [wdesk.tenant_id]
    profile = await wdesk.sql("SELECT profile FROM customer_identities WHERE external_id = 'wmA'")
    assert json.loads(profile[0]["profile"])["nickname"] == "客户甲"
