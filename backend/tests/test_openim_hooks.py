import json
import uuid
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI

from app.core.config import Settings
from tests.factories import provision
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_visitor import channel_key, init

AFTER_SEND = "callbackAfterSendGroupMsgCommand"
BEFORE_CREATE = "callbackBeforeCreateGroupCommand"


def hook_path(settings: Settings, command: str) -> str:
    return f"/hooks/openim/{settings.openim_webhook_secret.get_secret_value()}/{command}"


async def deliver(client: httpx.AsyncClient, settings: Settings, callbacks: list[Any]) -> None:
    for body in callbacks:
        response = await client.post(hook_path(settings, AFTER_SEND), json=body)
        assert response.status_code == 200, response.text
        assert response.json()["nextCode"] == 0


async def rows(database_urls: DatabaseUrls, query: str, *args: Any) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(database_urls.platform_dsn)
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


async def new_visitor(app: FastAPI, client: httpx.AsyncClient, code: str = "acme") -> Any:
    await provision(app, code)
    response = await init(client, await channel_key(client, code))
    return response.json()


async def test_customer_message_is_stored_once(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> None:
    visitor = await new_visitor(app, client)
    im = visitor["im"]
    sent = fake_im.send_as(im["user_id"], im["group_id"], "你好，想咨询价格")

    # 同一条回调投递两次（例如 OpenIM 重试或对账重复拉取）也只入库一次。
    await deliver(client, settings, fake_im.callbacks * 2)

    [msg] = await rows(database_urls, "SELECT * FROM messages")
    assert msg["room_id"] == uuid.UUID(visitor["room_id"])
    assert (msg["direction"], msg["sender_type"], msg["source"]) == ("in", "customer", "webhook")
    assert msg["sender_id"].hex == im["user_id"].rsplit("_", 1)[1]
    assert (msg["content_type"], json.loads(msg["content"])) == (
        "text",
        {"text": "你好，想咨询价格"},
    )
    assert msg["text_plain"] == "你好，想咨询价格"
    assert msg["channel_msg_id"] == sent.server_msg_id
    assert msg["im_seq"] is None
    assert int(msg["sent_at"].timestamp() * 1000) == sent.send_time
    [room] = await rows(database_urls, "SELECT last_message_at FROM rooms")
    assert room["last_message_at"] == msg["sent_at"]


async def test_bot_and_system_messages_are_stored_but_signalling_is_not(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> None:
    visitor = await new_visitor(app, client)
    group_id = visitor["im"]["group_id"]
    im = app.state.im
    await im.send_group_message(
        send_id="acme_bot", group_id=group_id, content_type=101, content={"content": "您好"}
    )
    await im.send_group_message(
        send_id="acme_sys", group_id=group_id, content_type=101, content={"content": "已转接"}
    )
    await im.send_group_message(
        send_id="acme_sys",
        group_id=group_id,
        content_type=110,
        content={"data": "{}", "description": "", "extension": ""},
        online_only=True,
    )

    await deliver(client, settings, fake_im.callbacks)

    stored = await rows(
        database_urls, "SELECT sender_type, direction, text_plain FROM messages ORDER BY sent_at"
    )
    assert [tuple(r) for r in stored] == [("bot", "out", "您好"), ("system", "out", "已转接")]


async def test_non_text_messages_keep_their_body(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> None:
    visitor = await new_visitor(app, client)
    picture = {"sourcePicture": {"url": "https://example.com/a.png", "width": 10, "height": 10}}
    fake_im.send_as(visitor["im"]["user_id"], visitor["im"]["group_id"], "x")
    body = fake_im.callbacks[-1] | {"contentType": 102, "content": json.dumps(picture)}

    await deliver(client, settings, [body])

    [msg] = await rows(database_urls, "SELECT content_type, content, text_plain FROM messages")
    assert msg["content_type"] == "image"
    assert json.loads(msg["content"]) == {"im_content_type": 102, "body": picture}
    assert msg["text_plain"] is None


async def test_messages_outside_service_rooms_are_ignored(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> None:
    visitor = await new_visitor(app, client)
    fake_im.send_as(visitor["im"]["user_id"], visitor["im"]["group_id"], "x")
    base = fake_im.callbacks[-1]
    unknown_room = f"acme_r_{uuid.uuid4().hex}"
    other_visitor = f"acme_c_{uuid.uuid4().hex}"
    bad = [
        base | {"groupID": "acme_evil", "serverMsgID": "1"},
        base | {"groupID": unknown_room, "serverMsgID": "2"},
        base | {"groupID": f"nobody_r_{uuid.uuid4().hex}", "serverMsgID": "3"},
        # 服务群里出现了不是这个 Room 客户身份的"客户"
        base | {"sendID": other_visitor, "serverMsgID": "4"},
        base | {"sendID": "imAdmin", "serverMsgID": "5"},
        {"unexpected": "payload"},
    ]

    await deliver(client, settings, bad)

    assert await rows(database_urls, "SELECT 1 FROM messages") == []


async def test_wrong_secret_gets_a_non_json_response(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    response = await client.post(f"/hooks/openim/wrong/{BEFORE_CREATE}", json={})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/plain")


async def test_before_create_group_only_allows_platform_service_rooms(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    room = f"acme_r_{uuid.uuid4().hex}"

    def payload(group_id: str, owner: str) -> dict[str, Any]:
        return {
            "callbackCommand": BEFORE_CREATE,
            "groupID": group_id,
            "ownerUserID": "",
            "initMemberList": [
                {"userID": owner, "roleLevel": 100},
                {"userID": "acme_bot", "roleLevel": 20},
            ],
        }

    cases = [
        (payload(room, "acme_sys"), 0),
        (payload(room, f"acme_c_{uuid.uuid4().hex}"), 1),
        (payload(room, "globex_sys"), 1),
        (payload("acme_mygroup", "acme_sys"), 1),
        (payload("", "acme_sys"), 1),
        ({"initMemberList": "nope"}, 1),
    ]
    for body, next_code in cases:
        response = await client.post(hook_path(settings, BEFORE_CREATE), json=body)
        assert response.status_code == 200
        assert response.json()["nextCode"] == next_code, body


async def test_other_callbacks_are_acknowledged(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    response = await client.post(hook_path(settings, "callbackAfterUserOnlineCommand"), json={})

    assert response.status_code == 200
    assert response.json() == {
        "actionCode": 0,
        "errCode": 0,
        "errMsg": "",
        "errDlt": "",
        "nextCode": 0,
    }
