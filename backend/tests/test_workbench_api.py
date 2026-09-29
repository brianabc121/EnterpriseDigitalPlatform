"""工作台用到的接口：客户面板（档案、备注、标签、渠道身份）与快捷话术。"""

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from tests.desk import Desk
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


async def test_serving_agent_sees_and_edits_the_customer_panel(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    url = f"/api/v1/customers/{chat['customer_id']}"

    detail = (await desk.client.get(url, headers=alice.headers)).json()

    assert detail["notes"] is None
    assert detail["tags"] == []
    [identity] = detail["identities"]
    assert (identity["channel_type"], identity["channel_name"], identity["verified"]) == (
        "web",
        "官网",
        False,
    )
    assert set(identity["profile"]) == {"user_agent", "first_page", "referrer"}

    response = await desk.client.patch(
        url,
        headers=alice.headers,
        json={
            "display_name": "王先生",
            "notes": "想了解企业版报价",
            "tags": ["意向高", " 企业版 ", "意向高"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["display_name"], body["notes"], body["tags"]) == (
        "王先生",
        "想了解企业版报价",
        ["意向高", "企业版"],
    )
    listed = (await desk.client.get("/api/v1/customers", headers=desk.admin)).json()["items"]
    assert [(c["display_name"], c["tags"]) for c in listed] == [("王先生", ["意向高", "企业版"])]
    [audit] = await desk.sql("SELECT action FROM audit_logs WHERE action = 'customer.update'")
    assert audit["action"] == "customer.update"

    # 看不到的客户不能修改。
    bob = await desk.agent("bob", online=False)
    assert (
        await desk.client.patch(url, headers=bob.headers, json={"notes": "x"})
    ).status_code == 404


async def test_quick_replies(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)
    lead = await desk.agent("lead", roles=["supervisor"], online=False)

    # 坐席只能建个人话术；主管可以建全员共享的。
    forbidden = await desk.client.post(
        "/api/v1/quick-replies",
        headers=alice.headers,
        json={"shared": True, "title": "问候", "content": "您好"},
    )
    assert forbidden.status_code == 403
    shared = await desk.client.post(
        "/api/v1/quick-replies",
        headers=lead.headers,
        json={
            "shared": True,
            "category": "通用",
            "title": "问候",
            "content": "您好，请问有什么可以帮您？",
        },
    )
    assert shared.status_code == 201, shared.text
    mine = await desk.client.post(
        "/api/v1/quick-replies",
        headers=alice.headers,
        json={"title": "报价", "content": "企业版每年 9800 元"},
    )
    assert mine.status_code == 201

    listed = (await desk.client.get("/api/v1/quick-replies", headers=alice.headers)).json()["items"]
    assert [(r["title"], r["shared"]) for r in listed] == [("问候", True), ("报价", False)]
    bobs = (await desk.client.get("/api/v1/quick-replies", headers=bob.headers)).json()["items"]
    assert [r["title"] for r in bobs] == ["问候"]

    # 别人的个人话术看不到也改不了；共享话术只有有权限的人能改。
    mine_id, shared_id = mine.json()["id"], shared.json()["id"]
    assert (
        await desk.client.patch(
            f"/api/v1/quick-replies/{mine_id}", headers=bob.headers, json={"title": "x"}
        )
    ).status_code == 404
    assert (
        await desk.client.patch(
            f"/api/v1/quick-replies/{shared_id}", headers=alice.headers, json={"title": "x"}
        )
    ).status_code == 403
    updated = await desk.client.patch(
        f"/api/v1/quick-replies/{mine_id}",
        headers=alice.headers,
        json={"content": "企业版每年 8800 元"},
    )
    assert updated.json()["content"] == "企业版每年 8800 元"
    assert (
        await desk.client.delete(f"/api/v1/quick-replies/{mine_id}", headers=alice.headers)
    ).status_code == 204
    listed = (await desk.client.get("/api/v1/quick-replies", headers=alice.headers)).json()["items"]
    assert [r["title"] for r in listed] == ["问候"]


async def test_session_messages_are_limited_to_that_session(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "第一次")
    first = await desk.session_of(visitor)
    await desk.client.post(f"/api/v1/sessions/{first['id']}/close", headers=alice.headers)
    await desk.flush()
    await desk.say(visitor, "第二次")
    second = await desk.session_of(visitor)

    page = await desk.client.get(f"/api/v1/sessions/{second['id']}/messages", headers=alice.headers)

    assert page.status_code == 200, page.text
    texts = [m["text_plain"] for m in page.json()["items"] if m["sender_type"] == "customer"]
    assert texts == ["第二次"]
