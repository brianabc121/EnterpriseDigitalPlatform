"""优秀话术挖掘（设计文档 §12.4）与单条知识的使用和满意度（§12.7）。"""

import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.kb.extraction import run_extraction
from tests.desk import Agent, Desk, Visitor
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_ai_reception import ANSWER, enable_ai
from tests.test_kb_extraction import candidates

GOOD = "非常理解您的心情，我这边马上帮您加急处理，今天下班前一定给您回复进度。"
SHIPPING = "您的订单已经打包好了，预计明天上午从仓库发出，发出后会短信通知您单号。"


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def _serve(desk: Desk, agent: Agent, question: str, *replies: str) -> tuple[Visitor, str]:
    visitor = await desk.visitor()
    await desk.say(visitor, question)
    chat = await desk.session_of(visitor)
    for text in replies:
        sent = await desk.client.post(
            f"/api/v1/sessions/{chat['id']}/messages",
            headers=agent.headers,
            json={"client_msg_id": uuid.uuid4().hex, "text": text},
        )
        assert sent.status_code == 200, sent.text
    await desk.flush()
    closed = await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=agent.headers)
    assert closed.status_code == 200, closed.text
    await desk.flush()
    return visitor, str(chat["id"])


async def _rate(desk: Desk, visitor: Visitor, session_id: str, score: int) -> None:
    response = await desk.client.post(
        "/api/v1/visitor/csat",
        headers={"X-Visitor-Token": visitor.visitor_token},
        json={"session_id": session_id, "score": score},
    )
    assert response.status_code == 204, response.text


async def test_good_replies_from_satisfied_sessions_become_shared_phrases(desk: Desk) -> None:
    alice = await desk.agent("alice")
    happy, happy_id = await _serve(desk, alice, "我的订单怎么还没到，急用", GOOD, "好的")
    await _rate(desk, happy, happy_id, 5)
    unhappy, unhappy_id = await _serve(desk, alice, "发货了吗", SHIPPING)
    await _rate(desk, unhappy, unhappy_id, 2)

    report = await run_extraction(desk.ctx)
    assert report.phrases == 1
    [phrase] = await candidates(desk, kind="phrase")
    assert (phrase["kind"], phrase["answer"], phrase["category"]) == ("phrase", GOOD, "优秀话术")
    assert phrase["question"] == GOOD[:8]
    [evidence] = (
        await desk.client.get(f"/api/v1/kb/candidates/{phrase['id']}", headers=desk.admin)
    ).json()["evidence"]
    assert evidence["session_id"] == happy_id
    assert [line["text"] for line in evidence["lines"]] == [GOOD]
    # 不重复挖掘同一个会话。
    assert (await run_extraction(desk.ctx)).phrases == 0

    merged = await desk.client.post(
        f"/api/v1/kb/candidates/{phrase['id']}/merge",
        headers=desk.admin,
        json={"item_id": str(uuid.uuid4())},
    )
    assert merged.status_code == 422
    approved = await desk.client.post(
        f"/api/v1/kb/candidates/{phrase['id']}/approve",
        headers=desk.admin,
        json={"question": "安抚并承诺跟进"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    replies = (await desk.client.get("/api/v1/quick-replies", headers=alice.headers)).json()
    shared = [r for r in replies["items"] if r["shared"]]
    assert [(r["title"], r["content"], r["category"]) for r in shared] == [
        ("安抚并承诺跟进", GOOD, "优秀话术")
    ]

    # 已经是共享话术的回复不再作为候选。
    again, again_id = await _serve(desk, alice, "还要等多久", GOOD)
    await _rate(desk, again, again_id, 5)
    assert (await run_extraction(desk.ctx)).phrases == 0
    assert await candidates(desk, kind="phrase") == []


async def test_ratings_given_after_extraction_are_mined_later(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor, session_id = await _serve(desk, alice, "发货了吗", SHIPPING)
    first = await run_extraction(desk.ctx)
    assert (first.sessions, first.phrases) == (1, 0)
    await _rate(desk, visitor, session_id, 4)
    later = await run_extraction(desk.ctx)
    assert (later.sessions, later.phrases) == (0, 1)
    [phrase] = await candidates(desk, kind="phrase")
    assert phrase["answer"] == SHIPPING
    [row] = await desk.sql("SELECT phrases_at FROM kb_extractions")
    assert row["phrases_at"] is not None


async def _item_stats(desk: Desk, item_id: str) -> dict[str, Any]:
    response = await desk.client.get(f"/api/v1/kb/items/{item_id}/stats", headers=desk.admin)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_item_stats_combine_ratings_and_session_satisfaction(desk: Desk) -> None:
    await enable_ai(desk)
    [item] = (await desk.client.get("/api/v1/kb/items", headers=desk.admin)).json()["items"]
    assert item["content"] == ANSWER
    empty = await _item_stats(desk, item["id"])
    assert (empty["ai_sessions"], empty["csat_avg"], empty["zombie"]) == (0, None, False)

    alice = await desk.agent("alice")
    served: list[tuple[Visitor, str]] = []
    for _ in range(2):
        visitor = await desk.visitor()
        await desk.say(visitor, "快递几天能到")
        served.append((visitor, str((await desk.session_of(visitor))["id"])))
    # 第二位访客觉得回答没用，转了人工。
    visitor, session_id = served[1]
    [bot] = await desk.sql(
        "SELECT channel_msg_id FROM messages WHERE room_id = $1 AND sender_type = 'bot'",
        visitor.room_id,
    )
    await desk.client.post(
        "/api/v1/visitor/ai-feedback",
        headers={"X-Visitor-Token": visitor.visitor_token},
        json={"server_msg_id": bot["channel_msg_id"], "value": -1},
    )
    await desk.say(visitor, "转人工")
    for _, session_id in served:
        closed = await desk.client.post(f"/api/v1/sessions/{session_id}/close", headers=desk.admin)
        assert closed.status_code == 200, closed.text
    await _rate(desk, served[0][0], served[0][1], 5)
    await _rate(desk, served[1][0], served[1][1], 2)

    stats = await _item_stats(desk, item["id"])
    assert (stats["ai_sessions"], stats["handoff_sessions"]) == (2, 1)
    assert (stats["csat_count"], stats["csat_avg"]) == (2, 3.5)
    assert (stats["visitor_likes"], stats["visitor_dislikes"]) == (0, 1)
    assert stats["visitor_satisfaction"] == 0.0
    assert stats["hits"] >= 2

    metrics = await desk.client.get("/api/v1/kb/metrics", headers=desk.admin)
    assert metrics.json()["visitor_disliked_items"] == [
        {"item_id": item["id"], "title": item["title"], "count": 1}
    ]
    denied = await desk.client.get(f"/api/v1/kb/items/{item['id']}/stats", headers=alice.headers)
    assert denied.status_code == 403
