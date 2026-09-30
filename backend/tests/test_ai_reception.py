"""AI 接待（P3）：知识库回答、转人工与摘要、故障降级、护栏、额度、非工作时间、坐席助手、评测。"""

import json
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.dates import today
from app.modules.sessions.engine import run_session_timers, utcnow
from app.modules.usage.service import rollup_day
from tests.desk import Desk, Visitor
from tests.factories import bearer, create_platform_admin, platform_login
from tests.fake_llm import FakeLLM
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls

ANSWER = "一般 2 到 3 天送达，偏远地区 5 到 7 天。"


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


async def enable_ai(desk: Desk, **policy: Any) -> None:
    response = await desk.client.put(
        "/api/v1/ai/settings", headers=desk.admin, json={"enabled": True}
    )
    assert response.status_code == 200, response.text
    [default] = [
        p
        for p in (await desk.client.get("/api/v1/routing-policies", headers=desk.admin)).json()[
            "items"
        ]
        if p["is_default"]
    ]
    patched = await desk.client.patch(
        f"/api/v1/routing-policies/{default['id']}",
        headers=desk.admin,
        json={"mode": "ai_first", **policy},
    )
    assert patched.status_code == 200, patched.text
    item = await desk.client.post(
        "/api/v1/kb/items",
        headers=desk.admin,
        json={
            "title": "订单发货后多久能到？",
            "content": ANSWER,
            "questions": ["快递几天能到"],
            "publish": True,
        },
    )
    assert item.status_code == 201, item.text


def bot_texts(desk: Desk, visitor: Visitor) -> list[str]:
    return desk.room_texts(visitor, sender=f"{desk.code}_bot")


async def decisions(desk: Desk, visitor: Visitor) -> list[dict[str, Any]]:
    chat = await desk.session_of(visitor)
    response = await desk.client.get(
        f"/api/v1/sessions/{chat['id']}/ai-decisions", headers=desk.admin
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def test_ai_answers_from_the_knowledge_base(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()

    await desk.say(visitor, "请问订单大概多久能到")

    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("ai_serving", None)
    assert bot_texts(desk, visitor) == [ANSWER]
    [message] = [
        m for m in desk.im.groups[visitor.group_id].messages if m.send_id == f"{desk.code}_bot"
    ]
    assert json.loads(message.ex) == {"ai": True}
    [stored] = await desk.sql("SELECT text_plain FROM messages WHERE sender_type = 'bot'")
    assert stored["text_plain"] == ANSWER
    [decision] = await decisions(desk, visitor)
    assert (decision["action"], decision["reason"], decision["reply"]) == ("reply", None, ANSWER)
    assert decision["knowledge"][0]["title"] == "订单发货后多久能到？"
    assert await desk.events_of(chat["id"]) == ["created", "ai_serving"]
    [item] = await desk.sql("SELECT hits FROM kb_items")
    assert item["hits"] == 1
    calls = await desk.sql("SELECT scene, status FROM llm_calls ORDER BY created_at")
    assert ("reply", "ok") in {(c["scene"], c["status"]) for c in calls}
    # 坐席在线但 AI 能回答，不打扰坐席。
    assert desk.im.signals_to(alice.im_user) == []


async def test_customer_asking_for_a_human_is_handed_over_with_a_summary(
    desk: Desk, fake_llm: FakeLLM
) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "快递几天能到")

    await desk.say(visitor, "我要转人工")

    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", alice.staff_id)
    assert chat["handoff_reason"] == "customer_request"
    assert chat["ai_summary"] == "客户咨询：快递几天能到；我要转人工。"
    # 接手的坐席在工作台看到交接摘要。
    detail = await desk.client.get(f"/api/v1/sessions/{chat['id']}", headers=alice.headers)
    assert (detail.json()["ai_summary"], detail.json()["handoff_reason"]) == (
        chat["ai_summary"],
        "customer_request",
    )
    events = await desk.events_of(chat["id"])
    assert events[-3:] == ["handoff", "queued", "assigned"]
    last = await decisions(desk, visitor)
    assert (last[-1]["action"], last[-1]["reason"]) == ("handoff", "customer_request")
    # 客户要求人工时不调用回复模型。
    tasks = [
        r["messages"][0]["content"].split("\n", 1)[0] for r in fake_llm.requests if "messages" in r
    ]
    assert tasks.count("任务：在线客服回复") == 1


async def test_unanswerable_questions_are_handed_over(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()

    await desk.say(visitor, "你们的发票抬头能改成个人吗")

    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", alice.staff_id)
    assert chat["handoff_reason"] == "model_request"
    # 模型礼貌说明后转人工。
    assert bot_texts(desk, visitor) == ["抱歉，这个问题我暂时无法回答。"]


async def test_llm_outage_degrades_to_human(desk: Desk, fake_llm: FakeLLM) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    fake_llm.mode = "down"

    await desk.say(visitor, "快递几天能到")

    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", alice.staff_id)
    assert chat["handoff_reason"] == "ai_unavailable"
    assert chat["ai_summary"] == "客户最近的问题：快递几天能到"
    assert bot_texts(desk, visitor) == []
    failed = await desk.sql("SELECT scene FROM llm_calls WHERE status = 'error'")
    assert "reply" in {f["scene"] for f in failed}


async def test_guardrails_block_promises_then_hand_over(desk: Desk, fake_llm: FakeLLM) -> None:
    await desk.agent("alice")
    visitor = await desk.visitor()
    fake_llm.mode = "promise"

    await desk.say(visitor, "快递几天能到")
    first = await desk.session_of(visitor)
    await desk.say(visitor, "那快递到底几天能到")

    assert first["status"] == "ai_serving"
    [fallback] = bot_texts(desk, visitor)
    assert "转人工" in fallback and "保证" not in fallback
    history = await decisions(desk, visitor)
    assert [(d["action"], d["reason"], d["guard"]) for d in history] == [
        ("reply", "guardrail_retry", "promise"),
        ("handoff", "guardrail", "promise"),
    ]
    assert (await desk.session_of(visitor))["status"] == "human_serving"


async def test_consecutive_messages_get_one_reply(desk: Desk) -> None:
    visitor = await desk.visitor()
    desk.im.send_as(visitor.user_id, visitor.group_id, "你好")
    desk.im.send_as(visitor.user_id, visitor.group_id, "快递几天能到")

    await desk.flush()

    [decision] = await decisions(desk, visitor)
    assert decision["question"] == "你好\n快递几天能到"
    assert bot_texts(desk, visitor) == [ANSWER]


async def test_monthly_quota_hands_over_to_humans(desk: Desk) -> None:
    alice = await desk.agent("alice")
    await create_platform_admin(desk.app)
    ops = bearer(await platform_login(desk.client))
    await desk.client.patch(
        f"/platform/v1/tenants/{desk.tenant_id}", headers=ops, json={"ai_monthly_quota": 1}
    )
    first, second = await desk.visitor(), await desk.visitor()

    await desk.say(first, "快递几天能到")
    await desk.say(second, "快递几天能到")

    assert (await desk.session_of(first))["status"] == "ai_serving"
    # 额度用完后，新会话直接由人工接待，原因记在会话上。
    later = await desk.session_of(second)
    assert (later["status"], later["assignee_id"], later["handoff_reason"]) == (
        "human_serving",
        alice.staff_id,
        "quota",
    )
    assert bot_texts(desk, second) == []
    settings = await desk.client.get("/api/v1/ai/settings", headers=desk.admin)
    assert (settings.json()["monthly_quota"], settings.json()["used_this_month"]) == (1, 1)
    # 已在 AI 接待中的会话，下一次回复时发现额度用完，转人工。
    await desk.say(first, "那偏远地区呢")
    handed = await desk.session_of(first)
    assert (handed["status"], handed["handoff_reason"]) == ("human_serving", "quota")


async def test_ai_is_used_only_when_enabled_and_policy_is_ai_first(desk: Desk) -> None:
    alice = await desk.agent("alice")
    await desk.client.put("/api/v1/ai/settings", headers=desk.admin, json={"enabled": False})
    visitor = await desk.visitor()

    await desk.say(visitor, "快递几天能到")

    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", alice.staff_id)
    assert bot_texts(desk, visitor) == []


async def test_handoff_outside_business_hours_leaves_a_message(desk: Desk) -> None:
    now = utcnow().astimezone()
    closed_day = str((now.isoweekday() % 7) + 1)  # 明天营业、今天休息
    [default] = [
        p
        for p in (await desk.client.get("/api/v1/routing-policies", headers=desk.admin)).json()[
            "items"
        ]
        if p["is_default"]
    ]
    await desk.client.patch(
        f"/api/v1/routing-policies/{default['id']}",
        headers=desk.admin,
        json={"business_hours": {"tz": "UTC", "days": {closed_day: [["00:00", "24:00"]]}}},
    )
    visitor = await desk.visitor()

    await desk.say(visitor, "快递几天能到")
    assert (await desk.session_of(visitor))["status"] == "ai_serving"  # AI 不受工作时间限制
    await desk.say(visitor, "转人工")

    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["close_reason"]) == ("closed", "leave_message")
    [todo] = await desk.sql("SELECT title, detail FROM todos")
    assert todo["title"] == "非工作时间留言"
    assert todo["detail"].startswith("【客户要求人工】客户咨询：")


async def test_idle_ai_sessions_are_closed_as_resolved(desk: Desk) -> None:
    visitor = await desk.visitor()
    await desk.say(visitor, "快递几天能到")
    chat = await desk.session_of(visitor)

    await run_session_timers(desk.ctx, now=utcnow() + timedelta(hours=2))

    closed = await desk.session_of(visitor)
    assert (closed["id"], closed["status"], closed["close_reason"]) == (
        chat["id"],
        "closed",
        "ai_resolved",
    )


async def test_settings_test_console_and_evaluation(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    forbidden = await desk.client.get("/api/v1/ai/settings", headers=alice.headers)
    assert forbidden.status_code == 403

    updated = await desk.client.put(
        "/api/v1/ai/settings",
        headers=desk.admin,
        json={"bot_name": "小智", "handoff_keywords": ["找经理"], "handoff_threshold": 0.8},
    )
    body = updated.json()
    assert (body["bot_name"], body["handoff_keywords"], body["llm_configured"]) == (
        "小智",
        ["找经理"],
        True,
    )

    answered = await desk.client.post(
        "/api/v1/ai/test", headers=desk.admin, json={"question": "快递几天能到"}
    )
    manager = await desk.client.post(
        "/api/v1/ai/test", headers=desk.admin, json={"question": "我要找经理"}
    )
    assert (answered.json()["action"], answered.json()["reply"]) == ("reply", ANSWER)
    assert answered.json()["knowledge"][0]["title"] == "订单发货后多久能到？"
    assert (manager.json()["action"], manager.json()["reason"]) == ("handoff", "customer_request")

    run = await desk.client.post(
        "/api/v1/ai/evaluations",
        headers=desk.admin,
        json={
            "cases": [
                {"question": "快递几天能到", "expect_keywords": ["2 到 3 天"]},
                {"question": "订单多久能送到", "expect_keywords": ["次日达"]},
                {"question": "我要投诉", "expect_handoff": True},
                {"question": "发票抬头能改吗", "expect_handoff": False},
            ]
        },
    )
    assert run.status_code == 201, run.text
    result = run.json()
    assert (result["cases"], result["answer_accuracy"], result["handoff_accuracy"]) == (
        4,
        0.5,
        0.75,
    )
    listed = await desk.client.get("/api/v1/ai/evaluations", headers=desk.admin)
    assert [r["id"] for r in listed.json()["items"]] == [result["id"]]


async def test_agents_get_suggested_replies(desk: Desk) -> None:
    await desk.client.put("/api/v1/ai/settings", headers=desk.admin, json={"enabled": False})
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "快递几天能到")
    chat = await desk.session_of(visitor)

    response = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/suggestions", headers=alice.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["suggestions"] == [ANSWER]
    assert response.json()["knowledge"][0]["title"] == "订单发货后多久能到？"
    # 其他坐席看不到这个会话，也拿不到建议。
    bob = await desk.agent("bob")
    denied = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/suggestions", headers=bob.headers
    )
    assert denied.status_code == 404


async def test_ai_usage_is_metered(desk: Desk) -> None:
    await desk.agent("alice")
    answered, handed = await desk.visitor(), await desk.visitor()
    await desk.say(answered, "快递几天能到")
    await desk.say(handed, "转人工")
    tz = ZoneInfo(desk.settings.usage_timezone)

    await rollup_day(desk.ctx.db, today(tz), tz)

    rows = await desk.sql(
        "SELECT metric, value FROM usage_daily WHERE metric IN"
        " ('ai_sessions', 'ai_handoffs', 'bot_messages', 'llm_tokens')"
    )
    usage = {r["metric"]: r["value"] for r in rows}
    assert (usage["ai_sessions"], usage["ai_handoffs"], usage["bot_messages"]) == (2, 1, 1)
    [tokens] = await desk.sql("SELECT sum(prompt_tokens + completion_tokens) AS n FROM llm_calls")
    assert usage["llm_tokens"] == tokens["n"] > 0


async def test_bot_messages_carry_the_configured_name(desk: Desk) -> None:
    await desk.client.put("/api/v1/ai/settings", headers=desk.admin, json={"bot_name": "小智"})
    visitor = await desk.visitor()

    await desk.say(visitor, "快递几天能到")

    [message] = [
        m for m in desk.im.groups[visitor.group_id].messages if m.send_id == f"{desk.code}_bot"
    ]
    assert message.sender_nickname == "小智"
    history = await desk.client.get(f"/api/v1/rooms/{visitor.room_id}/messages", headers=desk.admin)
    assert [m["sender_name"] for m in history.json()["items"] if m["sender_type"] == "bot"] == [
        "小智"
    ]
    widget = await desk.client.get(
        "/api/v1/visitor/messages", headers={"X-Visitor-Token": visitor.visitor_token}
    )
    assert [m["sender_name"] for m in widget.json()["items"] if m["sender_type"] == "bot"] == [
        "小智"
    ]


async def test_reports_count_ai_sessions(desk: Desk) -> None:
    await desk.agent("alice")
    resolved, handed, serving = await desk.visitor(), await desk.visitor(), await desk.visitor()
    await desk.say(resolved, "快递几天能到")
    await desk.say(handed, "转人工")
    await run_session_timers(desk.ctx, now=utcnow() + timedelta(hours=2))
    await desk.say(serving, "快递几天能到")

    realtime = await desk.client.get("/api/v1/reports/realtime", headers=desk.admin)
    overview = await desk.client.get("/api/v1/reports/overview", headers=desk.admin)

    assert realtime.json()["ai_serving"] == 1
    totals = overview.json()["totals"]
    assert (
        totals["ai_sessions"],
        totals["ai_resolved"],
        totals["ai_handoffs"],
        totals["ai_resolution_rate"],
    ) == (3, 1, 1, 0.3333)
    assert sum(d["ai_sessions"] for d in overview.json()["days"]) == 3


async def test_a_score_equal_to_the_threshold_hands_over(desk: Desk) -> None:
    saved = await desk.client.put(
        "/api/v1/ai/settings",
        headers=desk.admin,
        json={"handoff_threshold": 0.2, "relevance_threshold": 0.55},
    )
    # 阈值原样保存（没有单精度浮点的误差）。
    assert (saved.json()["handoff_threshold"], saved.json()["relevance_threshold"]) == (0.2, 0.55)

    # 知识相关、模型有把握，只有"否定回答"一个信号（0.2），正好等于阈值。
    outcome = await desk.client.post(
        "/api/v1/ai/test", headers=desk.admin, json={"question": "快递几天能到，你说的不对"}
    )

    body = outcome.json()
    assert (body["action"], body["reason"], body["score"]) == ("handoff", "score", 0.2)
    assert body["signals"]["negation"] is True and body["signals"]["low_relevance"] is False
