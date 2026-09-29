"""企业微信：回调加解密、服务商授权、回调路由（设计文档 §7.4、§16）。"""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.integrations.wecom import CallbackCrypto, CallbackError, parse_xml
from tests.desk import Desk
from tests.factories import bearer, login, provision
from tests.fake_wecom import AES_KEY, CORP_ID, SUITE_ID, TOKEN, FakeWeCom
from tests.wecom_desk import API, WecomDesk

# 企业微信官方文档里 WXBizMsgCrypt 的示例数据。
OFFICIAL = {
    "token": "QDG6eK",
    "key": "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C",
    "signature": "5c45ff5e21c57e6ad56bac8758b79b1d9ac89fd3",
    "timestamp": "1409659589",
    "nonce": "263014780",
    "echostr": (
        "P9nAzCzyDtyTWESHep1vC5X9xho/qYX3Zpb4yKa9SKld1DsH3Iyt3tP3zNdtp+4RPcs8TgAE7OaBO+FZXvnaqQ=="
    ),
}


def test_crypto_matches_the_official_sample() -> None:
    crypto = CallbackCrypto(OFFICIAL["token"], OFFICIAL["key"])
    plaintext, receive_id = crypto.verify_url(
        msg_signature=OFFICIAL["signature"],
        timestamp=OFFICIAL["timestamp"],
        nonce=OFFICIAL["nonce"],
        echostr=OFFICIAL["echostr"],
    )
    assert (plaintext, receive_id) == ("1616140317555161061", "wx5823bf96d3bd56c7")


def test_crypto_round_trip_and_rejections() -> None:
    crypto = CallbackCrypto(TOKEN, AES_KEY)
    body, params = crypto.seal("<xml><Event><![CDATA[测试]]></Event><N>1</N></xml>", "corp")
    event, receive_id = crypto.open(
        msg_signature=params["msg_signature"],
        timestamp=params["timestamp"],
        nonce=params["nonce"],
        body=body,
    )
    assert event == {"Event": "测试", "N": "1"}
    assert receive_id == "corp"
    with pytest.raises(CallbackError):
        crypto.open(
            msg_signature="0" * 40, timestamp=params["timestamp"], nonce=params["nonce"], body=body
        )
    other = CallbackCrypto(TOKEN, "a" * 43)
    with pytest.raises(CallbackError):
        other.open(
            msg_signature=params["msg_signature"],
            timestamp=params["timestamp"],
            nonce=params["nonce"],
            body=body,
        )
    with pytest.raises(CallbackError):
        parse_xml('<!DOCTYPE x [<!ENTITY a "b">]><xml><A>&a;</A></xml>')
    assert parse_xml("<xml><L><I>1</I><I>2</I></L></xml>") == {"L": {"I": ["1", "2"]}}


async def test_callback_url_verification(wdesk: WecomDesk) -> None:
    crypto = CallbackCrypto(TOKEN, AES_KEY)
    echostr = crypto.encrypt("echo-123", SUITE_ID)
    params = {
        "msg_signature": crypto.signature("1700000000", "n1", echostr),
        "timestamp": "1700000000",
        "nonce": "n1",
        "echostr": echostr,
    }
    for kind in ("cmd", "data"):
        response = await wdesk.client.get(f"/hooks/wecom/{kind}", params=params)
        assert response.status_code == 200
        assert response.text == "echo-123"
    bad = {**params, "msg_signature": "0" * 40}
    assert (await wdesk.client.get("/hooks/wecom/cmd", params=bad)).status_code == 403


async def test_callbacks_are_disabled_without_provider_config(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/hooks/wecom/cmd",
        params={"msg_signature": "x", "timestamp": "1", "nonce": "n", "echostr": "e"},
    )
    assert response.status_code == 404


async def test_admin_authorizes_the_corp(wdesk: WecomDesk) -> None:
    status = await wdesk.status()
    assert status["enabled"] is True
    assert status["corp"] is None
    assert status["callback_urls"]["command"] == "http://testserver/hooks/wecom/cmd"

    await wdesk.authorize()

    status = await wdesk.status()
    assert status["corp"]["corp_id"] == CORP_ID
    assert status["corp"]["agent_id"] == 1000017
    assert status["corp"]["auth_user_id"] == "admin"
    # 全量同步：成员、客服账号、标签。
    assert status["counts"]["members"] == 3
    assert status["counts"]["tags"] == 3
    assert [a["name"] for a in status["kf_accounts"]] == ["官方客服"]
    assert status["kf_accounts"][0]["contact_url"].startswith("https://work.weixin.qq.com/kfid/")
    assert set(status["sync_state"]) >= {"members", "kf", "tags", "contacts", "groups"}
    # 永久授权码用租户数据密钥加密保存；客服账号和客户联系各有一个渠道。
    [corp] = await wdesk.sql("SELECT permanent_code_enc FROM wecom_corps")
    assert corp["permanent_code_enc"].startswith("v2:1:")
    assert wdesk.wecom.corp.permanent_code not in corp["permanent_code_enc"]
    channels = await wdesk.sql("SELECT type, name FROM channel_accounts ORDER BY created_at")
    assert [(c["type"], c["name"]) for c in channels][1:] == [
        ("wecom_contact", "企业微信客户联系"),
        ("wecom_kf", "官方客服"),
    ]


async def test_create_auth_callback_after_redirect_is_idempotent(wdesk: WecomDesk) -> None:
    await wdesk.wecom.push_suite_ticket()
    response = await wdesk.client.post(f"{API}/install", headers=wdesk.admin)
    state = parse_qs(urlsplit(response.json()["url"]).query)["state"][0]
    auth_code = wdesk.wecom.authorize(state)
    # 回调先到，浏览器跳转后到（临时授权码已经用过）。
    assert (await wdesk.wecom.push_create_auth(auth_code, state)).text == "success"
    response = await wdesk.client.get(
        "/api/v1/wecom/install/callback", params={"auth_code": auth_code, "state": state}
    )
    assert "installed=1" in response.headers["location"]
    assert len(await wdesk.sql("SELECT 1 FROM wecom_corps WHERE status = 'active'")) == 1


async def test_install_with_unknown_state_is_rejected(wdesk: WecomDesk) -> None:
    await wdesk.wecom.push_suite_ticket()
    auth_code = wdesk.wecom.authorize("forged")
    response = await wdesk.client.get(
        "/api/v1/wecom/install/callback", params={"auth_code": auth_code, "state": "forged"}
    )
    assert response.status_code == 302
    assert "error=" in response.headers["location"]
    assert await wdesk.sql("SELECT 1 FROM wecom_corps") == []
    # 没有 state 的 create_auth（不是从控制台发起的授权）被忽略。
    assert (await wdesk.wecom.push_create_auth(auth_code, "")).status_code == 200
    assert await wdesk.sql("SELECT 1 FROM wecom_corps") == []


async def test_a_corp_binds_only_one_tenant(wdesk: WecomDesk) -> None:
    await wdesk.authorize()
    other = await Desk(wdesk.app, wdesk.client, wdesk.im, wdesk.settings, wdesk.database_urls).open(
        "beta"
    )
    response = await other.client.post(f"{API}/install", headers=other.admin)
    state = parse_qs(urlsplit(response.json()["url"]).query)["state"][0]
    response = await other.client.get(
        "/api/v1/wecom/install/callback",
        params={"auth_code": wdesk.wecom.authorize(state), "state": state},
    )
    assert "error=" in response.headers["location"]
    assert "其他租户" in parse_qs(urlsplit(response.headers["location"]).query)["error"][0]


async def test_redirect_after_failed_callback_shows_the_reason(wdesk: WecomDesk) -> None:
    await wdesk.authorize()
    other = await Desk(wdesk.app, wdesk.client, wdesk.im, wdesk.settings, wdesk.database_urls).open(
        "beta"
    )
    response = await other.client.post(f"{API}/install", headers=other.admin)
    state = parse_qs(urlsplit(response.json()["url"]).query)["state"][0]
    auth_code = wdesk.wecom.authorize(state)
    # 回调先到：临时授权码被用掉，但企业已绑定其他租户，绑定失败。
    assert (await wdesk.wecom.push_create_auth(auth_code, state)).status_code == 200
    response = await other.client.get(
        "/api/v1/wecom/install/callback", params={"auth_code": auth_code, "state": state}
    )
    assert response.status_code == 302
    assert "其他租户" in parse_qs(urlsplit(response.headers["location"]).query)["error"][0]


async def test_expired_tokens_are_refreshed(wdesk: WecomDesk) -> None:
    await wdesk.authorize()
    wdesk.wecom.expire_tokens()
    response = await wdesk.client.post(
        f"{API}/sync", headers=wdesk.admin, json={"targets": ["members"]}
    )
    assert response.status_code == 202
    await wdesk.flush()
    status = await wdesk.status()
    assert status["sync_state"]["members"]["error"] is None


async def test_cancel_auth_disables_channels(wdesk: WecomDesk) -> None:
    await wdesk.authorize()
    assert (await wdesk.wecom.push_cancel_auth()).text == "success"
    status = await wdesk.status()
    assert status["corp"] is None
    rows = await wdesk.sql(
        "SELECT status FROM channel_accounts WHERE type IN ('wecom_kf', 'wecom_contact')"
    )
    assert {r["status"] for r in rows} == {"disabled"}
    # 已取消授权的企业的回调被忽略（仍然应答 success，企业微信不再重推）。
    response = await wdesk.wecom.customer_says("还在吗")
    assert response["msgid"]


async def test_admin_unbinds_and_reauthorizes(wdesk: WecomDesk) -> None:
    await wdesk.authorize()
    response = await wdesk.client.delete(API, headers=wdesk.admin)
    assert response.status_code == 204
    assert (await wdesk.status())["corp"] is None
    await wdesk.authorize()
    status = await wdesk.status()
    assert status["corp"]["status"] == "active"
    assert [a["status"] for a in status["kf_accounts"]] == ["active"]


async def test_data_callback_must_match_its_corp(wdesk: WecomDesk) -> None:
    await wdesk.authorize()
    crypto = CallbackCrypto(TOKEN, AES_KEY)
    event = (
        "<xml><ToUserName>wwother</ToUserName><MsgType>event</MsgType>"
        "<Event>kf_msg_or_event</Event><OpenKfId>wkfake0001</OpenKfId></xml>"
    )
    body, params = crypto.seal(event, CORP_ID)
    response = await wdesk.client.post("/hooks/wecom/data", params=params, content=body)
    assert response.status_code == 403


async def test_only_admins_manage_the_integration(wdesk: WecomDesk) -> None:
    agent = await wdesk.agent("amy")
    for method, path in (("GET", API), ("POST", f"{API}/install"), ("GET", f"{API}/members")):
        response = await wdesk.client.request(method, path, headers=agent.headers)
        assert response.status_code == 403, path


async def test_other_tenant_cannot_see_the_binding(wdesk: WecomDesk) -> None:
    await wdesk.authorize()
    await provision(wdesk.app, "beta")
    token = await login(wdesk.client, "beta")
    response = await wdesk.client.get(API, headers=bearer(token))
    assert response.json()["corp"] is None
    assert response.json()["kf_accounts"] == []


def test_fake_uses_the_same_key_as_settings() -> None:
    fake = FakeWeCom()
    assert fake.crypto.signature("1", "2", "3") == CallbackCrypto(TOKEN, AES_KEY).signature(
        "1", "2", "3"
    )
