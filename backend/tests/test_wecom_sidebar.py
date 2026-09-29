"""企业微信登录（扫码、免登）、JS-SDK 签名与聊天工具栏侧边栏（设计文档 §10.4、§10.5）。"""

import hashlib
from urllib.parse import parse_qs, urlsplit

from tests.factories import bearer
from tests.fake_wecom import CORP_ID
from tests.test_ai_reception import ANSWER, enable_ai
from tests.test_wecom_contacts import customer, sync, with_agents
from tests.wecom_desk import WecomDesk


async def test_sso_url_points_to_the_corp_app(wdesk: WecomDesk) -> None:
    await wdesk.authorize()
    response = await wdesk.client.get(
        "/api/v1/auth/wecom/sso", params={"tenant_code": "acme", "state": "abc12345"}
    )
    assert response.status_code == 200, response.text
    url = urlsplit(response.json()["url"])
    query = parse_qs(url.query)
    assert url.netloc == "login.work.weixin.qq.com"
    assert query["login_type"] == ["CorpApp"]
    assert query["appid"] == [CORP_ID]
    assert query["agentid"] == ["1000017"]
    assert query["redirect_uri"] == [f"http://console/wecom/login?corp={CORP_ID}"]
    assert query["state"] == ["abc12345"]
    missing = await wdesk.client.get(
        "/api/v1/auth/wecom/sso", params={"tenant_code": "nobody", "state": "abc12345"}
    )
    assert missing.status_code == 404


async def test_member_logs_in_with_wecom(wdesk: WecomDesk) -> None:
    await with_agents(wdesk)
    code = wdesk.wecom.login_code("zhangsan")
    response = await wdesk.client.post(
        "/api/v1/auth/wecom", json={"corp_id": CORP_ID, "code": code}
    )
    assert response.status_code == 200, response.text
    assert "edp_refresh" in response.headers.get("set-cookie", "")
    me = await wdesk.client.get("/api/v1/me", headers=bearer(response.json()["access_token"]))
    assert me.json()["username"] == "amy"
    # code 只能用一次。
    again = await wdesk.client.post("/api/v1/auth/wecom", json={"corp_id": CORP_ID, "code": code})
    assert again.status_code == 401


async def test_unbound_member_is_told_to_bind(wdesk: WecomDesk) -> None:
    await wdesk.authorize()
    response = await wdesk.client.post(
        "/api/v1/auth/wecom", json={"corp_id": CORP_ID, "code": wdesk.wecom.login_code("admin")}
    )
    assert response.status_code == 403
    assert "admin" in response.json()["error"]["message"]


async def test_staff_binds_their_own_wecom_account(wdesk: WecomDesk) -> None:
    await wdesk.authorize()
    carol = await wdesk.agent("carol")
    response = await wdesk.client.post(
        "/api/v1/me/wecom",
        headers=carol.headers,
        json={"corp_id": CORP_ID, "code": wdesk.wecom.login_code("lisi")},
    )
    assert response.status_code == 204, response.text
    login = await wdesk.client.post(
        "/api/v1/auth/wecom", json={"corp_id": CORP_ID, "code": wdesk.wecom.login_code("lisi")}
    )
    assert login.status_code == 200
    # 已被别人绑定的成员不能再绑定。
    dave = await wdesk.agent("dave")
    taken = await wdesk.client.post(
        "/api/v1/me/wecom",
        headers=dave.headers,
        json={"corp_id": CORP_ID, "code": wdesk.wecom.login_code("lisi")},
    )
    assert taken.status_code == 409


async def test_jssdk_config_signatures(wdesk: WecomDesk) -> None:
    amy, _ = await with_agents(wdesk)
    url = "http://console/wecom/sidebar?x=1"
    response = await wdesk.client.get(
        "/api/v1/wecom/jssdk-config", headers=amy.headers, params={"url": f"{url}#frag"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["corp_id"] == CORP_ID
    assert body["agent_id"] == 1000017
    for key, ticket in (("config", "fake-jsapi-ticket"), ("agent_config", "fake-agent-ticket")):
        sig = body[key]
        raw = (
            f"jsapi_ticket={ticket}&noncestr={sig['nonce_str']}"
            f"&timestamp={sig['timestamp']}&url={url}"
        )
        assert sig["signature"] == hashlib.sha1(raw.encode()).hexdigest()


async def test_sidebar_shows_the_current_customer(wdesk: WecomDesk) -> None:
    amy, bob = await with_agents(wdesk)
    await sync(wdesk, "contacts")
    response = await wdesk.client.get(
        "/api/v1/sidebar/context", headers=amy.headers, params={"external_userid": "wmA"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "contact"
    assert body["customer"]["display_name"] == "甲总"
    assert body["customer"]["tags"] == ["VIP"]
    assert body["wecom"]["follows"][0]["staff_name"] == "Amy"
    # bob 既不是归属坐席，也没有在企业微信里添加这位客户。
    response = await wdesk.client.get(
        "/api/v1/sidebar/context", headers=bob.headers, params={"external_userid": "wmA"}
    )
    assert response.status_code == 404


async def test_sidebar_allows_the_member_who_added_the_customer(wdesk: WecomDesk) -> None:
    _, bob = await with_agents(wdesk)
    await sync(wdesk, "contacts")
    # 李四也添加了客户甲：bob 在侧边栏能看到（客户归属仍是 amy）。
    wdesk.wecom.add_contact("wmA", "客户甲", userid="lisi")
    await wdesk.wecom.contact_changed("wmA", userid="lisi")
    await wdesk.flush()
    response = await wdesk.client.get(
        "/api/v1/sidebar/context", headers=bob.headers, params={"external_userid": "wmA"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["customer"]["owner_name"] == "Amy"


async def test_sidebar_group_context(wdesk: WecomDesk) -> None:
    amy, _ = await with_agents(wdesk)
    await sync(wdesk, "contacts")
    wdesk.wecom.add_group("wrg1", "售后群", externals=["wmA", "wmB"])
    await wdesk.wecom.group_changed("wrg1", "create")
    await wdesk.flush()
    response = await wdesk.client.get(
        "/api/v1/sidebar/context", headers=amy.headers, params={"chat_id": "wrg1"}
    )
    body = response.json()
    assert body["kind"] == "group"
    assert body["group"]["name"] == "售后群"
    # 群里的客户按数据范围过滤：amy 只看到自己的客户。
    assert [c["display_name"] for c in body["group_customers"]] == ["甲总"]


async def test_sidebar_suggestions_and_sent_record(wdesk: WecomDesk) -> None:
    amy, _ = await with_agents(wdesk)
    await sync(wdesk, "contacts")
    await enable_ai(wdesk)
    response = await wdesk.client.post(
        "/api/v1/sidebar/suggestions",
        headers=amy.headers,
        json={"question": "快递几天能到", "external_userid": "wmA"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["suggestions"] == [ANSWER]
    assert body["knowledge"][0]["title"] == "订单发货后多久能到？"

    response = await wdesk.client.post(
        "/api/v1/sidebar/sent",
        headers=amy.headers,
        json={"content": ANSWER, "origin": "suggestion", "external_userid": "wmA"},
    )
    assert response.status_code == 204, response.text
    [row] = await wdesk.sql("SELECT * FROM wecom_sidebar_messages")
    assert row["origin"] == "suggestion"
    assert row["customer_id"] == (await customer(wdesk, "wmA"))["id"]
    assert row["staff_id"] == amy.staff_id
