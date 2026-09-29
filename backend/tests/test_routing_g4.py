"""排队优先级、按意图分配、技能组溢出、排队期间 AI 继续回答（设计文档 §11.3）。"""

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.routing import priority as prio
from app.modules.sessions.engine import run_session_timers
from tests.desk import Desk, Visitor
from tests.fake_llm import FakeLLM
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_ai_reception import ANSWER, bot_texts, enable_ai


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def _default_policy(desk: Desk) -> dict[str, Any]:
    items = (await desk.client.get("/api/v1/routing-policies", headers=desk.admin)).json()["items"]
    policy: dict[str, Any] = next(p for p in items if p["is_default"])
    return policy


async def _patch_policy(desk: Desk, **changes: Any) -> dict[str, Any]:
    policy = await _default_policy(desk)
    response = await desk.client.patch(
        f"/api/v1/routing-policies/{policy['id']}", headers=desk.admin, json=changes
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _group(desk: Desk, name: str, members: list[uuid.UUID], **extra: Any) -> str:
    response = await desk.client.post(
        "/api/v1/skill-groups",
        headers=desk.admin,
        json={"name": name, "members": [{"staff_id": str(m)} for m in members], **extra},
    )
    assert response.status_code == 201, response.text
    group_id: str = response.json()["id"]
    return group_id


async def _tag_customer(desk: Desk, visitor: Visitor, tags: list[str]) -> None:
    [room] = await desk.sql("SELECT customer_id FROM rooms WHERE id = $1", visitor.room_id)
    response = await desk.client.patch(
        f"/api/v1/customers/{room['customer_id']}", headers=desk.admin, json={"tags": tags}
    )
    assert response.status_code == 200, response.text


def test_priority_tiers_and_requeue_bumps() -> None:
    assert (
        prio.base_priority(
            priority_tags=["VIP"], urgent_first=True, customer_tags=["VIP"], text="我要投诉"
        )
        == 20
    )
    assert (
        prio.base_priority(priority_tags=["VIP"], urgent_first=True, customer_tags=[], text="退款")
        == 10
    )
    assert (
        prio.base_priority(priority_tags=["VIP"], urgent_first=False, customer_tags=[], text="退款")
        == 0
    )
    assert (
        prio.base_priority(
            priority_tags=[], urgent_first=True, customer_tags=[], text="你好", reason="sensitive"
        )
        == 10
    )
    # 退回队列在同一档内往前排，最多 9 次，不会跨到上一档。
    assert prio.bump(0) == 1 and prio.bump(9) == 9 and prio.bump(20) == 21
    group = uuid.uuid4()
    routes = [{"intent": "售后", "keywords": ["退货"], "skill_group_id": str(group)}]
    assert prio.match_intent(routes, text="我想退货") == prio.IntentMatch("售后", group, "keyword")
    assert prio.match_intent(routes, text="你好", ai_intent="售后") == prio.IntentMatch(
        "售后", group, "ai"
    )
    assert prio.match_intent(routes, text="你好", ai_intent="售前") is None


async def test_vip_and_urgent_customers_jump_the_queue(desk: Desk) -> None:
    normal = await desk.visitor()
    await desk.say(normal, "你好")
    angry = await desk.visitor()
    await desk.say(angry, "我要投诉你们的服务")
    vip = await desk.visitor()
    await _tag_customer(desk, vip, ["VIP"])
    await desk.say(vip, "你好")

    queued = await desk.client.get("/api/v1/sessions?status=queued", headers=desk.admin)
    order = [(s["room_id"], s["priority"]) for s in queued.json()["items"]]
    assert order == [(str(vip.room_id), 20), (str(angry.room_id), 10), (str(normal.room_id), 0)]
    chat = await desk.session_of(vip)
    [queued_event] = await desk.sql(
        "SELECT payload FROM session_events WHERE session_id = $1 AND type = 'queued'", chat["id"]
    )
    assert '"priority": "vip"' in queued_event["payload"]
    state = await desk.client.get(
        "/api/v1/visitor/session", headers={"X-Visitor-Token": normal.visitor_token}
    )
    assert state.json()["queue_position"] == 3

    # 策略关闭"投诉优先"、去掉 VIP 标签后，新来的客户按普通排队。
    await _patch_policy(desk, urgent_first=False, priority_tags=[])
    later = await desk.visitor()
    await desk.say(later, "我要投诉")
    assert (await desk.session_of(later))["priority"] == 0


async def test_intent_keywords_pick_the_skill_group(desk: Desk) -> None:
    alice = await desk.agent("alice")
    await desk.agent("bob")
    after_sales = await _group(desk, "售后", [alice.staff_id])
    policy = await _patch_policy(
        desk,
        intent_routes=[
            {"intent": "售后", "keywords": ["退货", "维修"], "skill_group_id": after_sales}
        ],
    )
    assert policy["intent_routes"][0]["intent"] == "售后"

    visitor = await desk.visitor()
    await desk.say(visitor, "我买的耳机想退货")
    chat = await desk.session_of(visitor)
    assert (chat["intent"], str(chat["skill_group_id"]), chat["assignee_id"]) == (
        "售后",
        after_sales,
        alice.staff_id,
    )
    [queued_event] = await desk.sql(
        "SELECT payload FROM session_events WHERE session_id = $1 AND type = 'queued'", chat["id"]
    )
    assert '"intent_by": "keyword"' in queued_event["payload"]

    bad = await desk.client.patch(
        f"/api/v1/routing-policies/{policy['id']}",
        headers=desk.admin,
        json={
            "intent_routes": [
                {"intent": "售后", "keywords": [], "skill_group_id": after_sales},
                {"intent": "售后", "keywords": [], "skill_group_id": after_sales},
            ]
        },
    )
    assert bad.status_code == 422
    missing = await desk.client.patch(
        f"/api/v1/routing-policies/{policy['id']}",
        headers=desk.admin,
        json={"intent_routes": [{"intent": "技术", "skill_group_id": str(uuid.uuid4())}]},
    )
    assert missing.status_code == 422


async def test_ai_intent_routes_the_handoff(desk: Desk, fake_llm: FakeLLM) -> None:
    alice = await desk.agent("alice")
    await desk.agent("bob")
    tech = await _group(desk, "技术支持", [alice.staff_id])
    await enable_ai(
        desk,
        intent_routes=[{"intent": "技术", "keywords": [], "skill_group_id": tech}],
    )
    fake_llm.intent = "技术"
    fake_llm.mode = "handoff"
    visitor = await desk.visitor()

    await desk.say(visitor, "设备连不上网络怎么办")

    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["intent"], str(chat["skill_group_id"])) == (
        "human_serving",
        "技术",
        tech,
    )
    assert chat["assignee_id"] == alice.staff_id
    systems = [
        m["content"]
        for r in fake_llm.requests
        for m in r.get("messages", [])
        if m.get("role") == "system" and "在线客服回复" in m["content"]
    ]
    assert systems and "从「技术」中选一个" in systems[0]


async def test_queue_overflows_to_the_backup_group(desk: Desk) -> None:
    alice = await desk.agent("alice")
    bob = await desk.agent("bob")
    backup = await _group(desk, "二线", [bob.staff_id])
    primary = await _group(
        desk, "一线", [alice.staff_id], overflow_group_id=backup, overflow_after_seconds=60
    )
    await _patch_policy(desk, default_skill_group_id=primary)
    await desk.set_status(alice, "away")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    assert (chat["status"], str(chat["skill_group_id"])) == ("queued", primary)

    early = await run_session_timers(desk.ctx, now=chat["queued_at"] + timedelta(seconds=30))
    assert early.overflowed == 0
    report = await run_session_timers(desk.ctx, now=chat["queued_at"] + timedelta(seconds=61))
    await desk.flush()
    assert report.overflowed == 1
    chat = await desk.session_of(visitor)
    assert (str(chat["skill_group_id"]), chat["assignee_id"]) == (backup, bob.staff_id)
    assert chat["overflowed_at"] is not None
    assert "overflowed" in await desk.events_of(chat["id"])

    groups = (await desk.client.get("/api/v1/skill-groups", headers=desk.admin)).json()["items"]
    assert next(g for g in groups if g["id"] == primary)["overflow_after_seconds"] == 60
    itself = await desk.client.patch(
        f"/api/v1/skill-groups/{primary}", headers=desk.admin, json={"overflow_group_id": primary}
    )
    assert itself.status_code == 422


async def test_ai_keeps_answering_while_queued(desk: Desk, fake_llm: FakeLLM) -> None:
    await enable_ai(desk, ai_while_queued=True)
    visitor = await desk.visitor()
    await desk.say(visitor, "转人工")
    chat = await desk.session_of(visitor)
    assert chat["status"] == "queued"
    assert "排队期间您可以继续提问" in desk.notices(visitor)[-1]

    await desk.say(visitor, "快递几天能到")
    assert bot_texts(desk, visitor) == [ANSWER]
    assert (await desk.session_of(visitor))["status"] == "queued"
    # AI 判断需要人工时不再重复转人工，也不发过渡话术。
    fake_llm.mode = "handoff"
    await desk.say(visitor, "我要改收货地址")
    assert bot_texts(desk, visitor) == [ANSWER]
    assert (await desk.session_of(visitor))["status"] == "queued"

    # 分配给坐席后 AI 不再回答。
    fake_llm.mode = "normal"
    await desk.agent("alice")
    await desk.say(visitor, "快递几天能到")
    assert (await desk.session_of(visitor))["status"] == "human_serving"
    assert bot_texts(desk, visitor) == [ANSWER]
