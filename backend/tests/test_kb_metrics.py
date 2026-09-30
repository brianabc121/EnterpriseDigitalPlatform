"""知识运营指标（设计文档 §12.7）、AI 建议采纳率与知识周报（§12.6）。"""

import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.kb.extraction import run_extraction
from app.modules.kb.metrics import run_digests
from app.modules.sessions.engine import utcnow
from tests.desk import Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_ai_reception import enable_ai


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    desk = await Desk(app, client, fake_im, settings, database_urls).open()
    await enable_ai(desk)
    return desk


async def test_knowledge_metrics_and_weekly_digest(desk: Desk) -> None:
    alice = await desk.agent("alice")
    answered, handed = await desk.visitor(), await desk.visitor()
    await desk.say(answered, "快递几天能到")
    await desk.say(handed, "你们的发票抬头能改成个人吗")
    chat = await desk.session_of(handed)
    assert chat["assignee_id"] == alice.staff_id

    # 坐席请求 AI 建议并采用后发送；然后结束会话。
    suggested = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/suggestions", headers=alice.headers
    )
    assert suggested.status_code == 200
    sent = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages",
        headers=alice.headers,
        json={
            "client_msg_id": uuid.uuid4().hex,
            "text": "可以的，开票时填写个人姓名即可。",
            "origin": "suggestion",
        },
    )
    assert sent.status_code == 200, sent.text
    [stored] = await desk.sql(
        "SELECT content FROM messages WHERE id = $1", uuid.UUID(sent.json()["id"])
    )
    assert '"origin": "suggestion"' in stored["content"]
    await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=alice.headers)
    await desk.flush()

    # 提炼出知识缺口，审核补充答案后发布；坐席给已有问答打了"没用"。
    await run_extraction(desk.ctx)
    [gap] = (
        await desk.client.get("/api/v1/kb/candidates", headers=desk.admin, params={"kind": "gap"})
    ).json()["items"]
    approved = await desk.client.post(
        f"/api/v1/kb/candidates/{gap['id']}/approve",
        headers=desk.admin,
        json={"answer": "可以，开票时抬头填写个人姓名。"},
    )
    assert approved.status_code == 200, approved.text
    [faq] = [
        i
        for i in (await desk.client.get("/api/v1/kb/items", headers=desk.admin)).json()["items"]
        if i["title"] == "订单发货后多久能到？"
    ]
    await desk.client.post(
        f"/api/v1/kb/items/{faq['id']}/feedback", headers=alice.headers, json={"value": -1}
    )

    metrics = (await desk.client.get("/api/v1/kb/metrics", headers=desk.admin)).json()

    assert (metrics["ai_replies"], metrics["knowledge_hit_rate"]) == (1, 1.0)
    assert metrics["handoff_reasons"] == [{"reason": "model_request", "count": 1}]
    assert (metrics["gaps_open"], metrics["gaps_new"], metrics["gaps_closed"]) == (0, 1, 1)
    assert metrics["gap_close_hours"] is not None
    assert (metrics["candidates_reviewed"], metrics["pass_rate"]) == (1, 1.0)
    assert (metrics["suggestions"], metrics["suggestions_adopted"], metrics["adoption_rate"]) == (
        1,
        1,
        1.0,
    )
    assert [(i["title"], i["count"]) for i in metrics["top_items"]] == [("订单发货后多久能到？", 1)]
    assert [(i["title"], i["count"]) for i in metrics["disliked_items"]] == [
        ("订单发货后多久能到？", 1)
    ]
    assert metrics["stale_items"] == 0
    denied = await desk.client.get("/api/v1/kb/metrics", headers=alice.headers)
    assert denied.status_code == 403

    # 周报：管理员立即生成本周的；坐席也能查看。
    generated = await desk.client.post("/api/v1/kb/digests", headers=desk.admin, json={})
    assert generated.status_code == 200, generated.text
    data = generated.json()["data"]
    assert {i["title"] for i in data["new_items"]} == {
        "订单发货后多久能到？",
        "你们的发票抬头能改成个人吗",
    }
    assert [h["title"] for h in data["hot_items"]] == ["订单发货后多久能到？"]
    assert data["top_gaps"] == []
    assert data["candidates"] == {"pending": 0, "reviewed": 1, "accepted": 1}
    assert (data["ai"]["sessions"], data["ai"]["resolved"]) == (2, 0)
    listed = await desk.client.get("/api/v1/kb/digests", headers=alice.headers)
    assert [d["week_start"] for d in listed.json()["items"]] == [generated.json()["week_start"]]


async def test_digests_are_generated_once_a_week(desk: Desk) -> None:
    now = utcnow()

    # 下周：上一周（本周）的周报还没有，生成一次；再运行不重复生成。
    assert await run_digests(desk.ctx, now=now + timedelta(days=7)) == 1
    assert await run_digests(desk.ctx, now=now + timedelta(days=7)) == 0
    [row] = await desk.sql("SELECT week_start FROM kb_digests")
    assert row["week_start"].weekday() == 0
