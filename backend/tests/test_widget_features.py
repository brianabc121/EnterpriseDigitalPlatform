"""访客 Widget 相关能力：实名访客、嵌入来源、Widget 设置、满意度、留言、请求人工、
会话续接、图片消息。"""

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from tests.desk import Desk, Visitor
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_visitor import channel_key


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


async def channel(desk: Desk) -> dict[str, Any]:
    response = await desk.client.get("/api/v1/channels", headers=desk.admin)
    item: dict[str, Any] = response.json()["items"][0]
    return item


async def init(desk: Desk, **body: Any) -> httpx.Response:
    key = await channel_key(desk.client, desk.code)
    return await desk.client.post("/api/v1/visitor/init", json={"channel_key": key, **body})


def signed(
    secret: str, external_id: str, name: str | None, ts: int | None = None
) -> dict[str, Any]:
    ts = int(time.time()) if ts is None else ts
    message = f"{external_id}:{name or ''}:{ts}"
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return {"external_id": external_id, "name": name, "timestamp": ts, "signature": signature}


async def test_identified_visitor_keeps_one_identity_across_devices(desk: Desk) -> None:
    ch = await channel(desk)
    assert ch["identity_secret"] is None
    unconfigured = await init(desk, identity=signed("x" * 64, "u-1", "王先生"))
    assert unconfigured.status_code == 403

    rotated = await desk.client.post(
        f"/api/v1/channels/{ch['id']}/identity-secret", headers=desk.admin
    )
    secret = rotated.json()["identity_secret"]
    assert len(secret) == 64

    first = await init(desk, identity=signed(secret, "u-1", "王先生"))
    assert first.status_code == 200, first.text
    assert (first.json()["customer_name"], first.json()["verified"]) == ("王先生", True)
    # 另一台设备（没有访客令牌）以同一用户登录：同一个客户和 Room。
    second = await init(desk, identity=signed(secret, "u-1", "王先生"))
    assert second.json()["room_id"] == first.json()["room_id"]
    [identity] = await desk.sql("SELECT external_id, verified FROM customer_identities")
    assert (identity["external_id"], identity["verified"]) == ("u:u-1", True)

    forged = await init(desk, identity={**signed(secret, "u-1", "王先生"), "external_id": "u-2"})
    assert forged.status_code == 401
    stale = await init(desk, identity=signed(secret, "u-1", "王先生", int(time.time()) - 3600))
    assert stale.status_code == 401


async def test_widget_settings_and_allowed_origins(desk: Desk) -> None:
    ch = await channel(desk)
    response = await desk.client.patch(
        f"/api/v1/channels/{ch['id']}",
        headers=desk.admin,
        json={
            "widget": {
                "title": "Acme 客服",
                "welcome_message": "您好，有什么可以帮您？",
                "privacy_notice": "对话内容将用于提供服务。",
                "allowed_origins": ["https://www.acme.com/"],
            }
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["widget"]["allowed_origins"] == ["https://www.acme.com"]
    invalid = await desk.client.patch(
        f"/api/v1/channels/{ch['id']}",
        headers=desk.admin,
        json={"widget": {"allowed_origins": ["acme.com"]}},
    )
    assert invalid.status_code == 422

    allowed = await init(desk, embed_origin="https://www.acme.com")
    assert allowed.status_code == 200
    assert allowed.json()["widget"] == {
        "title": "Acme 客服",
        "welcome_message": "您好，有什么可以帮您？",
        "privacy_notice": "对话内容将用于提供服务。",
    }
    assert (await init(desk, embed_origin="https://evil.example")).status_code == 403
    assert (await init(desk)).status_code == 403
    # Widget 自己的页面（例如控制台里的访客测试页）总是允许。
    assert (await init(desk, embed_origin=desk.settings.widget_public_url)).status_code == 200


async def test_csat_after_the_session_ends(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    body = {"session_id": str(chat["id"]), "score": 5, "comment": "很专业"}

    early = await desk.client.post("/api/v1/visitor/csat", headers=headers(visitor), json=body)
    assert early.status_code == 409

    await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=alice.headers)
    rated = await desk.client.post("/api/v1/visitor/csat", headers=headers(visitor), json=body)
    again = await desk.client.post("/api/v1/visitor/csat", headers=headers(visitor), json=body)

    assert (rated.status_code, again.status_code) == (204, 409)
    chat = await desk.session_of(visitor)
    assert (chat["csat"], chat["csat_comment"]) == (5, "很专业")
    state = await desk.client.get("/api/v1/visitor/session", headers=headers(visitor))
    assert state.json()["csat"] == 5
    other = await desk.visitor()
    foreign = await desk.client.post("/api/v1/visitor/csat", headers=headers(other), json=body)
    assert foreign.status_code == 404


async def test_leave_message_creates_a_ticket(desk: Desk) -> None:
    visitor = await desk.visitor()
    body = {"content": "想要报价单", "contact": "13800000000"}

    response = await desk.client.post(
        "/api/v1/visitor/tickets", headers=headers(visitor), json=body
    )

    assert response.status_code == 204
    [ticket] = await desk.sql("SELECT source, content, contact, status FROM tickets")
    assert tuple(ticket) == ("visitor", "想要报价单", "13800000000", "open")
    for _ in range(4):
        await desk.client.post("/api/v1/visitor/tickets", headers=headers(visitor), json=body)
    limited = await desk.client.post("/api/v1/visitor/tickets", headers=headers(visitor), json=body)
    assert limited.status_code == 429


async def test_visitor_can_ask_for_a_human_before_writing(desk: Desk) -> None:
    visitor = await desk.visitor()

    response = await desk.client.post("/api/v1/visitor/handoff", headers=headers(visitor))
    await desk.flush()

    assert response.json()["status"] == "queued"
    chat = await desk.session_of(visitor)
    assert chat["status"] == "queued"
    events = await desk.sql(
        "SELECT payload FROM session_events WHERE session_id = $1 AND type = 'queued'", chat["id"]
    )
    assert json.loads(events[0]["payload"]) == {"reason": "visitor_request"}
    # 已经在排队时再点不会重复创建。
    again = await desk.client.post("/api/v1/visitor/handoff", headers=headers(visitor))
    assert again.json()["session_id"] == str(chat["id"])


async def test_returning_customer_goes_back_to_the_previous_agent(desk: Desk) -> None:
    bob = await desk.agent("bob")
    visitor = await desk.visitor()
    await desk.say(visitor, "第一次咨询")
    first = await desk.session_of(visitor)
    assert first["assignee_id"] == bob.staff_id
    await desk.client.post(f"/api/v1/sessions/{first['id']}/close", headers=bob.headers)
    await desk.flush()
    # Alice 刚上线、从没接待过：没有续接时会优先分给她（空闲最久）。
    await desk.agent("alice")

    await desk.say(visitor, "我又来了")

    second = await desk.session_of(visitor)
    assert second["id"] != first["id"]
    assert second["assignee_id"] == bob.staff_id
    [event] = await desk.sql(
        "SELECT payload FROM session_events WHERE session_id = $1 AND type = 'assigned'",
        second["id"],
    )
    assert json.loads(event["payload"])["via"] == "previous"


async def test_agent_sends_an_image(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "请发一下产品图")
    chat = await desk.session_of(visitor)
    upload = await desk.client.post(
        "/api/v1/uploads",
        headers=alice.headers,
        json={"filename": "产品.png", "content_type": "image/png", "size": 4096},
    )
    file_url = upload.json()["file_url"]

    bad = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages",
        headers=alice.headers,
        json={
            "client_msg_id": uuid.uuid4().hex,
            "type": "image",
            "attachment": {
                "url": "https://evil.example/x.png",
                "name": "x.png",
                "size": 1,
                "content_type": "image/png",
            },
        },
    )
    assert bad.status_code == 422
    sent = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages",
        headers=alice.headers,
        json={
            "client_msg_id": uuid.uuid4().hex,
            "type": "image",
            "attachment": {
                "url": file_url,
                "name": "产品.png",
                "size": 4096,
                "content_type": "image/png",
                "width": 800,
                "height": 600,
            },
        },
    )
    await desk.flush()

    assert sent.status_code == 200, sent.text
    assert (sent.json()["content_type"], sent.json()["content"]["url"]) == ("image", file_url)
    [im_msg] = [m for m in desk.im.groups[visitor.group_id].messages if m.content_type == 102]
    picture = json.loads(im_msg.content)["sourcePicture"]
    assert (picture["url"], picture["width"], picture["type"]) == (file_url, 800, "image/png")
    rows = await desk.sql("SELECT content_type FROM messages WHERE sender_type = 'agent'")
    assert [r["content_type"] for r in rows] == ["image"]
    history = await desk.client.get("/api/v1/visitor/messages", headers=headers(visitor))
    newest = history.json()["items"][0]
    assert newest["attachment"]["url"] == file_url

    # 访客也可以申请上传。
    upload = await desk.client.post(
        "/api/v1/visitor/uploads",
        headers=headers(visitor),
        json={"filename": "截图.jpg", "content_type": "image/jpeg", "size": 1000},
    )
    assert upload.status_code == 200
    assert upload.json()["file_url"].startswith(desk.settings.public_api_url)
