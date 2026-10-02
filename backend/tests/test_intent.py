"""AI 意图判断（设计文档 §32）：判断模型的接口、对话内容与结果的解读、坐席工作台、
作为 AI 回复的依据、设置与自定义意图、失败重试，以及运营后台的判断模型供应商。"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.integrations.llm import LLMUnavailable
from app.integrations.typesafe import Answer, JudgeClient, JudgeEndpoint, Question
from app.modules.ai import intent
from app.modules.ai.prompts import Turn
from tests.desk import Agent, Desk, Visitor
from tests.factories import bearer, create_platform_admin, platform_login
from tests.fake_llm import FakeLLM
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_ai_reception import bot_texts, decisions, enable_ai

FAKE_URL = "http://fake-llm/v1"
PROVIDERS = "/platform/v1/llm-providers"
ROUTES = "/platform/v1/settings/llm-routes"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """环境变量配置的判断模型指向模拟服务（运营后台没有路由"意图判断"时使用）。"""
    settings.judge_base_url = FAKE_URL
    settings.judge_api_key = settings.judge_api_key.__class__("sk-judge")
    settings.judge_price_input = 0.03
    return settings


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


def _decide_calls(fake: FakeLLM) -> list[dict[str, Any]]:
    return [r for r in fake.requests if "questions" in r]


async def _intent(desk: Desk, agent: Agent | None, session_id: str) -> Any:
    headers = agent.headers if agent else desk.admin
    response = await desk.client.get(f"/api/v1/sessions/{session_id}/intent", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _serving(desk: Desk, agent: Agent, first: str) -> tuple[Visitor, str]:
    visitor = await desk.visitor()
    await desk.say(visitor, first)
    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", agent.staff_id)
    return visitor, str(chat["id"])


async def _settings(desk: Desk, **changes: Any) -> httpx.Response:
    return await desk.client.put("/api/v1/ai/settings", headers=desk.admin, json=changes)


async def _ops(app: FastAPI, client: httpx.AsyncClient) -> dict[str, str]:
    await create_platform_admin(app)
    return bearer(await platform_login(client))


# ---- 判断模型的接口 ----


async def test_judge_client_sends_questions_and_reads_typed_answers() -> None:
    seen: list[dict[str, Any]] = []
    statuses = [529, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "auth": request.headers.get("authorization")})
        seen[-1]["body"] = json.loads(request.content)
        status = statuses.pop(0)
        if status != 200:
            return httpx.Response(status, json={"error": "overloaded"})
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "buy": {
                        "type": "score",
                        "score": 2.8,
                        "confidence": 0.9,
                        "legend": {"0": "无", "1": "低", "2": "中", "3": "高"},
                        "probabilities": {"0": 0.0, "1": 0.05, "2": 0.1, "3": 0.85},
                    },
                    "topic": {
                        "type": "choice",
                        "choice": "price",
                        "confidence": 0.8,
                        "probabilities": {"price": 0.8, "other": 0.2},
                    },
                    # 一些网关把是非题的概率包装成 probability。
                    "human": {"type": "noul", "probability": 0.12},
                    "broken": {"type": "choice", "choice": "unknown"},
                },
                "usage": {"input_tokens": 1000, "output_tokens": 50},
            },
        )

    client = JudgeClient(
        JudgeEndpoint(
            base_url="https://api.typesafe.ai/v1/",
            api_key="sk-1",
            model="jev-1.13.0",
            name="Jev",
            price_input=0.03,
        ),
        transport=httpx.MockTransport(handler),
    )
    questions = {
        "buy": Question("score", "想买的程度", ["无", "低", "中", "高"]),
        "topic": Question("choice", "主题", {"price": "价格", "other": "其他"}),
        "human": Question("noul", "要人工", {"true": "要", "false": "不要"}),
        "broken": Question("choice", "坏的回答", {"a": "甲", "b": "乙"}),
    }
    try:
        result = await client.decide("客户：多少钱？", questions)
    finally:
        await client.aclose()

    # 过载（529）时重试一次。
    assert len(seen) == 2
    assert seen[1]["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen[1]["auth"] == "Bearer sk-1"
    body = seen[1]["body"]
    assert (body["state"], body["model"]) == ("客户：多少钱？", "jev-1.13.0")
    assert body["questions"]["buy"] == {
        "type": "score",
        "instructions": "想买的程度",
        "criteria": ["无", "低", "中", "高"],
    }
    assert body["questions"]["human"]["type"] == "noul"
    assert body["questions"]["topic"]["criteria"] == {"price": "价格", "other": "其他"}

    assert result.model == "jev-1.13.0"
    assert (result.input_tokens, result.output_tokens) == (1000, 50)
    assert result.cost == pytest.approx(0.03)
    assert result.answers["buy"].score == pytest.approx(2.8)
    assert result.answers["buy"].probabilities["3"] == pytest.approx(0.85)
    assert (result.answers["topic"].choice, result.answers["topic"].confidence) == ("price", 0.8)
    assert result.answers["human"].probability == pytest.approx(0.12)
    # 选项以外的回答、没有概率的回答当作没有回答。
    assert "broken" not in result.answers


async def test_judge_client_gives_up_on_auth_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "invalid api key"})

    client = JudgeClient(
        JudgeEndpoint(base_url="https://api.typesafe.ai/v1", api_key="bad"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMUnavailable, match="HTTP 401"):
        await client.decide("客户：你好", {"q": Question("noul", "是不是")})
    await client.aclose()
    # 鉴权失败不重试。
    assert calls == 1


# ---- 问题、对话内容与结果的解读 ----


def test_questions_custom_intents_and_routes() -> None:
    options = intent.intent_options(
        [
            {"name": "定制尺寸", "description": "按客户的尺寸定做"},
            {"name": "  ", "description": "没有名称的不要"},
            {"name": "以旧换新"},
        ]
    )
    assert list(options)[-1] == "other"
    assert options["x:定制尺寸"] == ("定制尺寸", "定制尺寸：按客户的尺寸定做")
    assert options["x:以旧换新"] == ("以旧换新", "以旧换新")
    questions, back = intent.build_questions(options, ["售后", "售前"])
    assert set(questions) == {"purchase", "intent", "concern", "emotion", "human", "route"}
    assert questions["purchase"].kind == "score"
    assert len(questions["purchase"].criteria or []) == 5
    # 自定义意图在请求里用 x1、x2 这样的键，结果按键换回编码。
    criteria = questions["intent"].criteria
    assert isinstance(criteria, dict)
    assert criteria["x1"] == "定制尺寸：按客户的尺寸定做"
    assert (back["x1"], back["x2"], back["price"]) == ("x:定制尺寸", "x:以旧换新", "price")
    assert questions["human"].kind == "noul"
    assert list(questions["route"].criteria or {}) == ["售后", "售前", "其他"]
    # 没有按意图分配时不问分配意图。
    assert "route" not in intent.build_questions(options, [])[0]


def test_interpret_derives_stage_intent_and_concerns() -> None:
    options = intent.intent_options([{"name": "定制尺寸"}])
    _, back = intent.build_questions(options, ["售后"])
    judgment = intent.interpret(
        {
            "purchase": Answer(
                kind="score",
                score=3.2,
                probabilities={"0": 0.0, "1": 0.05, "2": 0.1, "3": 0.5, "4": 0.35},
            ),
            "intent": Answer(
                kind="choice", choice="x1", probabilities={"x1": 0.7, "price": 0.2, "other": 0.1}
            ),
            "concern": Answer(
                kind="choice",
                choice="delivery",
                probabilities={"delivery": 0.5, "price": 0.36, "quality": 0.1, "none": 0.04},
            ),
            "emotion": Answer(kind="score", score=1.6),
            "human": Answer(kind="noul", probability=0.2),
            "route": Answer(kind="choice", choice="售后", probabilities={"售后": 0.4}),
        },
        back,
        ["售后"],
    )
    assert judgment is not None
    assert (judgment.stage, round(judgment.stage_probability, 2)) == (3, 0.5)
    assert judgment.purchase_probability == pytest.approx(0.85)
    assert judgment.has_purchase_intent and judgment.reached(3) and not judgment.reached(4)
    assert (judgment.intent, judgment.intent_probability) == ("x:定制尺寸", 0.7)
    assert intent.intent_label(judgment.intent) == "定制尺寸"
    assert judgment.concerns == ["delivery", "price"]
    assert intent.emotion_label(judgment.emotion) == "生气激动"
    # 分配意图的概率不够时不用。
    assert judgment.route is None

    block = intent.prompt_block(judgment)
    assert block.startswith("【客户意图判断】")
    assert "下单意向：意向明确（有下单意向的把握 85%）" in block
    assert "真实意图：定制尺寸" in block
    assert "客户在意：发货时效、价格" in block
    assert "顺势推进下单" in block and "不要承诺优惠" in block and "先回应客户的感受" in block

    # 只有加权位置时按位置分到相邻的两级；没有下单意向的回答时没有结果。
    only_score = intent.interpret({"purchase": Answer(kind="score", score=1.25)}, back, [])
    assert only_score is not None
    assert only_score.distribution == pytest.approx([0.0, 0.75, 0.25, 0.0, 0.0])
    assert (only_score.stage, only_score.has_purchase_intent) == (1, False)
    assert "不要催客户下单" in intent.prompt_block(only_score)
    assert intent.interpret({"intent": Answer(kind="choice", choice="price")}, back, []) is None


def test_llm_output_and_state() -> None:
    options = intent.intent_options([])
    parsed = intent.parse_llm(
        '好的：{"purchase": 4, "purchase_confidence": 0.9, "intent": "purchase", '
        '"concerns": ["price", "x"], "emotion": 0, "human": 0.1, "route": "售前"}',
        options,
        ["售前"],
    )
    assert parsed is not None
    assert (parsed.stage, parsed.intent, parsed.concerns, parsed.route) == (
        4,
        "purchase",
        ["price"],
        "售前",
    )
    assert parsed.purchase_probability == pytest.approx(0.925)
    assert intent.parse_llm("不是 JSON", options, []) is None
    assert intent.parse_llm('{"intent": "price"}', options, []) is None

    assert intent.trivial("你好！") and intent.trivial("谢谢~") and intent.trivial(" 嗯嗯 ")
    # "好的""可以"可能是在确认下单。
    assert not intent.trivial("好的") and not intent.trivial("可以")

    state = intent.build_state(
        [Turn("customer", "你好"), Turn("bot", "您好，请问有什么可以帮您？")],
        ["我要两个，电话 13800001111", "地址是上海市" + "很长" * 400],
    )
    lines = state.splitlines()
    assert lines[1:3] == ["客户：你好", "智能客服：您好，请问有什么可以帮您？"]
    assert lines[4] == "【客户最新的消息】"
    assert lines[5] == "客户：我要两个，电话 [手机号1]"
    # 每条最多 300 字，脱敏后再发出。
    assert len(lines[6]) <= len("客户：") + intent.MESSAGE_CHARS
    assert "13800001111" not in state


# ---- 坐席工作台 ----


async def test_agent_sees_purchase_intent_and_real_intent(desk: Desk, fake_llm: FakeLLM) -> None:
    alice = await desk.agent("alice")
    visitor, session_id = await _serving(desk, alice, "你好")
    # 单独的寒暄不判断。
    assert _decide_calls(fake_llm) == []
    assert await _intent(desk, alice, session_id) is None

    await desk.say(visitor, "这款沙发多少钱？有灰色的吗")
    first = await _intent(desk, alice, session_id)
    assert (first["stage"], first["stage_label"], first["has_purchase_intent"]) == (
        2,
        "有兴趣",
        False,
    )
    assert (first["intent"], first["intent_label"]) == ("price", "询价比价")
    assert [c["label"] for c in first["concerns"]] == ["价格"]
    assert (first["emotion_label"], first["source"], first["model"]) == (
        "平静",
        "judge",
        "jev-fake-1",
    )
    assert first["human_probability"] == pytest.approx(0.03)
    assert len(first["distribution"]) == 5 and first["pending"] is False
    [call] = _decide_calls(fake_llm)
    assert call["model"] == "jev-latest"
    # 客服回复之前客户连着发的几条都是"客户最新的消息"。
    assert call["state"].split("【客户最新的消息】")[1].splitlines()[1:] == [
        "客户：你好",
        "客户：这款沙发多少钱？有灰色的吗",
    ]
    signals = [s for s in desk.im.signals_to(alice.im_user) if s["type"] == "intent.updated"]
    assert signals[-1]["session_id"] == session_id and signals[-1]["stage"] == 2

    listed = await desk.client.get("/api/v1/sessions?mine=true", headers=alice.headers)
    [item] = [s for s in listed.json()["items"] if s["id"] == session_id]
    assert (item["purchase_stage"], item["real_intent"]) == (2, "询价比价")
    assert item["intent_at"] is not None

    # 准备下单：坐席助手提醒一次。
    await desk.say(visitor, "我要两个，今天能发货吗")
    ready = await _intent(desk, alice, session_id)
    assert (ready["stage"], ready["stage_label"], ready["has_purchase_intent"]) == (
        4,
        "准备下单",
        True,
    )
    assert ready["purchase_probability"] > 0.8
    assert (ready["peak_stage"], [p["stage"] for p in ready["history"]]) == (4, [2, 4])
    # 坐席还没有回复：之前问的价格也在"客户最新的消息"里。
    assert [c["label"] for c in ready["concerns"]] == ["价格", "发货时效"]
    alerts = [s for s in desk.im.signals_to(alice.im_user) if s["type"] == "copilot.alert"]
    assert [a["kind"] for a in alerts] == ["purchase_ready"]
    assert "客户准备下单" in alerts[0]["text"]
    await desk.say(visitor, "收货人张三，地址是上海市浦东新区")
    alerts = [s for s in desk.im.signals_to(alice.im_user) if s["type"] == "copilot.alert"]
    assert len(alerts) == 1
    stored = await desk.client.get(f"/api/v1/sessions/{session_id}/alerts", headers=alice.headers)
    assert [a["kind"] for a in stored.json()["items"]] == ["purchase_ready"]

    # 坐席的消息不判断；已经判断过最新一条客户消息时不再判断。
    calls = len(_decide_calls(fake_llm))
    response = await desk.client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=alice.headers,
        json={"client_msg_id": "intent-agent-reply-1", "text": "好的，马上为您下单"},
    )
    assert response.status_code == 200, response.text
    await desk.flush()
    assert len(_decide_calls(fake_llm)) == calls
    assert await intent.run_due(desk.ctx) == 0

    # 每次判断都记账（场景"意图判断"）。
    rows = await desk.sql(
        "SELECT provider, model, status, prompt_tokens, cost FROM llm_calls"
        " WHERE scene = 'intent' ORDER BY created_at"
    )
    assert len(rows) == 3
    assert {(r["provider"], r["model"], r["status"]) for r in rows} == {
        ("typesafe", "jev-fake-1", "ok")
    }
    assert all(r["prompt_tokens"] > 0 and r["cost"] > 0 for r in rows)

    # 看不到这个会话的人拿不到判断。
    bob = await desk.agent("bob", online=False)
    hidden = await desk.client.get(f"/api/v1/sessions/{session_id}/intent", headers=bob.headers)
    assert hidden.status_code == 404


async def test_judgment_failures_retry_then_wait_for_the_next_message(
    desk: Desk, fake_llm: FakeLLM
) -> None:
    alice = await desk.agent("alice")
    fake_llm.judge_mode = "down"
    visitor, session_id = await _serving(desk, alice, "这个多少钱")
    [row] = await desk.sql("SELECT * FROM session_intents WHERE session_id = $1::uuid", session_id)
    assert (row["failures"], row["judgments"], row["stage"]) == (1, 0, None)
    assert row["due_at"] is not None
    assert await _intent(desk, alice, session_id) is None

    # 30 秒后重试，最多 3 次。
    later = datetime.now(UTC) + timedelta(minutes=1)
    assert await intent.run_due(desk.ctx, now=later) == 1
    assert await intent.run_due(desk.ctx, now=later + timedelta(minutes=1)) == 1
    [row] = await desk.sql("SELECT * FROM session_intents WHERE session_id = $1::uuid", session_id)
    assert (row["failures"], row["due_at"]) == (3, None)
    # 每次判断记一笔（客户端内部重试一次）。
    failed = await desk.sql(
        "SELECT count(*) AS n FROM llm_calls WHERE scene = 'intent' AND status = 'error'"
    )
    assert failed[0]["n"] == 3

    # 判断模型恢复后，客户的下一条消息照常判断。
    fake_llm.judge_mode = "normal"
    await desk.say(visitor, "有现货吗")
    judged = await _intent(desk, alice, session_id)
    assert (judged["stage"], judged["intent"]) == (3, "delivery")
    [row] = await desk.sql("SELECT * FROM session_intents WHERE session_id = $1::uuid", session_id)
    assert (row["failures"], row["judgments"]) == (0, 1)

    # 每个会话最多判断 60 次。
    await desk.sql(
        "UPDATE session_intents SET judgments = 60 WHERE session_id = $1::uuid", session_id
    )
    calls = len(_decide_calls(fake_llm))
    await desk.say(visitor, "我要两个")
    assert len(_decide_calls(fake_llm)) == calls
    assert (await _intent(desk, alice, session_id))["stage"] == 3


# ---- 作为 AI 回复的依据 ----


async def test_ai_reply_uses_the_judgment(desk: Desk, fake_llm: FakeLLM) -> None:
    await enable_ai(desk)
    await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "快递几天能到？我比较着急")
    assert bot_texts(desk, visitor)
    replies = [
        r["messages"][0]["content"]
        for r in fake_llm.requests
        if r.get("messages") and r["messages"][0]["content"].startswith("任务：在线客服回复")
    ]
    assert "【客户意图判断】" in replies[-1]
    assert "下单意向：意向明确" in replies[-1]
    assert "客户在意：发货时效" in replies[-1]
    assert "先回应客户的感受" in replies[-1]
    [decision] = await decisions(desk, visitor)
    assert decision["action"] == "reply"
    assert decision["signals"]["intent"] == {
        "stage": 3,
        "purchase": pytest.approx(0.86, abs=0.01),
        "intent": "delivery",
        "concerns": ["delivery"],
        "emotion": 1.0,
        "human": 0.03,
        "source": "judge",
    }
    # AI 回复前已经判断过的（实时消费进程先判断），回复时直接用，不再重复判断。
    assert len(_decide_calls(fake_llm)) == 1

    # 关键词里没有的说法：判断模型认为客户在要求人工，立即转人工。
    other = await desk.visitor()
    await desk.say(other, "能不能让你们负责人跟我说")
    [decision] = await decisions(desk, other)
    assert (decision["action"], decision["reason"]) == ("handoff", "customer_request")
    assert decision["signals"]["intent_human"] == pytest.approx(0.95)
    chat = await desk.session_of(other)
    assert chat["handoff_reason"] == "customer_request"

    # 不参考意图判断时照常回复，提示词里没有判断。
    assert (await _settings(desk, intent_in_reply=False)).status_code == 200
    third = await desk.visitor()
    await desk.say(third, "快递几天能到")
    last = [
        r["messages"][0]["content"]
        for r in fake_llm.requests
        if r.get("messages") and r["messages"][0]["content"].startswith("任务：在线客服回复")
    ][-1]
    assert "【客户意图判断】" not in last
    [decision] = await decisions(desk, third)
    assert "intent" not in decision["signals"]


async def test_high_intent_customers_are_handed_to_agents(desk: Desk, fake_llm: FakeLLM) -> None:
    await enable_ai(desk)
    alice = await desk.agent("alice")
    response = await _settings(desk, intent_handoff_stage=4)
    assert response.status_code == 200, response.text
    assert response.json()["intent_handoff_stage"] == 4

    curious = await desk.visitor()
    await desk.say(curious, "快递几天能到")
    assert (await desk.session_of(curious))["status"] == "ai_serving"

    buyer = await desk.visitor()
    await desk.say(buyer, "我要两个，快递几天能到")
    [decision] = await decisions(desk, buyer)
    assert (decision["action"], decision["reason"]) == ("handoff", "purchase_intent")
    assert decision["signals"]["intent_stage"] == 4
    chat = await desk.session_of(buyer)
    assert (chat["status"], chat["handoff_reason"], chat["assignee_id"]) == (
        "human_serving",
        "purchase_intent",
        alice.staff_id,
    )
    assert chat["ai_summary"]
    # 接手的坐席看得到之前的判断。
    judged = await _intent(desk, alice, str(chat["id"]))
    assert (judged["stage"], judged["intent"]) == (4, "purchase")

    # 传 null 恢复不转。
    response = await _settings(desk, intent_handoff_stage=None)
    assert response.json()["intent_handoff_stage"] is None
    # 同样的话 AI 照常回答（知识库里有答案）。
    again = await desk.visitor()
    await desk.say(again, "我要两个，快递几天能到")
    assert (await desk.session_of(again))["status"] == "ai_serving"
    assert bot_texts(desk, again)


async def test_judged_route_picks_the_skill_group(desk: Desk) -> None:
    alice = await desk.agent("alice")
    await desk.agent("bob")
    group = await desk.client.post(
        "/api/v1/skill-groups",
        headers=desk.admin,
        json={"name": "售后组", "members": [{"staff_id": str(alice.staff_id)}]},
    )
    assert group.status_code == 201, group.text
    # 意图的关键词里没有"坏了"：关键词转人工（投诉）时用判断出的分配意图选技能组。
    await enable_ai(
        desk,
        intent_routes=[
            {"intent": "售后", "keywords": ["维修"], "skill_group_id": group.json()["id"]}
        ],
    )
    visitor = await desk.visitor()
    await desk.say(visitor, "东西坏了，我要投诉")
    chat = await desk.session_of(visitor)
    assert (chat["handoff_reason"], chat["intent"], str(chat["skill_group_id"])) == (
        "sensitive",
        "售后",
        group.json()["id"],
    )
    assert chat["assignee_id"] == alice.staff_id


# ---- 设置、自定义意图与"试一试" ----


async def test_settings_custom_intents_and_test_console(desk: Desk, fake_llm: FakeLLM) -> None:
    body = (await desk.client.get("/api/v1/ai/settings", headers=desk.admin)).json()
    assert (body["intent_enabled"], body["intent_in_reply"], body["intent_handoff_stage"]) == (
        True,
        True,
        None,
    )
    assert (body["intent_source"], body["intent_model"]) == ("judge", "typesafe（jev-latest）")
    assert body["custom_intents"] == []

    duplicate = await _settings(desk, custom_intents=[{"name": "定制尺寸"}, {"name": " 定制尺寸 "}])
    assert duplicate.status_code == 422
    too_many = await _settings(desk, custom_intents=[{"name": f"意图{i}"} for i in range(11)])
    assert too_many.status_code == 422
    stage = await _settings(desk, intent_handoff_stage=2)
    assert stage.status_code == 422
    saved = await _settings(
        desk, custom_intents=[{"name": "定制尺寸", "description": "按客户的尺寸定做"}]
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["custom_intents"] == [
        {"name": "定制尺寸", "description": "按客户的尺寸定做"}
    ]

    alice = await desk.agent("alice")
    visitor, session_id = await _serving(desk, alice, "沙发能做定制尺寸吗")
    judged = await _intent(desk, alice, session_id)
    assert (judged["intent"], judged["intent_label"]) == ("x:定制尺寸", "定制尺寸")
    assert "x1" in _decide_calls(fake_llm)[-1]["questions"]["intent"]["criteria"]

    # 试一试：同时显示这个问题的判断。
    await enable_ai(desk)
    result = await desk.client.post(
        "/api/v1/ai/test", headers=desk.admin, json={"question": "我要买两个，怎么下单"}
    )
    assert result.status_code == 200, result.text
    tested = result.json()["intent"]
    assert (tested["stage"], tested["intent"], tested["has_purchase_intent"]) == (
        4,
        "purchase",
        True,
    )

    # 关掉后不再判断。
    assert (await _settings(desk, intent_enabled=False)).status_code == 200
    calls = len(_decide_calls(fake_llm))
    await desk.say(visitor, "多少钱")
    assert len(_decide_calls(fake_llm)) == calls
    result = await desk.client.post(
        "/api/v1/ai/test", headers=desk.admin, json={"question": "多少钱"}
    )
    assert result.json()["intent"] is None


# ---- 运营后台：判断模型供应商与"意图判断"路由 ----


async def test_platform_routes_intent_to_a_decision_model(
    desk: Desk, app: FastAPI, fake_llm: FakeLLM
) -> None:
    ops = await _ops(app, desk.client)
    as_default = await desk.client.post(
        PROVIDERS,
        headers=ops,
        json={
            "name": "Jev",
            "protocol": "typesafe",
            "base_url": FAKE_URL,
            "chat_model": "jev-1.13.0",
            "is_default": True,
        },
    )
    assert as_default.status_code == 422
    created = await desk.client.post(
        PROVIDERS,
        headers=ops,
        json={
            "name": "Jev",
            "protocol": "typesafe",
            "base_url": FAKE_URL,
            "api_key": "sk-jev-0001",
            "chat_model": "jev-1.13.0",
            "fast_model": "ignored",
            "embed_model": "ignored",
            "prices": {"input": 0.03, "output": 0},
        },
    )
    assert created.status_code == 201, created.text
    provider = created.json()
    assert (provider["protocol"], provider["fast_model"], provider["embed_model"]) == (
        "typesafe",
        "",
        "",
    )
    jev = provider["id"]
    checked = await desk.client.post(f"{PROVIDERS}/{jev}/test", headers=ops)
    assert checked.status_code == 200, checked.text
    assert (checked.json()["chat"]["ok"], checked.json()["chat"]["model"]) == (True, "jev-fake-1")
    assert checked.json()["embed"] is None

    # 判断模型只能用于意图判断：不能设为默认、不能用于其他场景、不能指定给租户。
    made_default = await desk.client.patch(
        f"{PROVIDERS}/{jev}", headers=ops, json={"is_default": True}
    )
    assert made_default.status_code == 422
    wrong_scene = await desk.client.put(ROUTES, headers=ops, json={"routes": {"reply": jev}})
    assert wrong_scene.status_code == 422
    assigned = await desk.client.put(
        f"/platform/v1/tenants/{desk.tenant_id}/llm", headers=ops, json={"provider_id": jev}
    )
    assert assigned.status_code == 422
    routed = await desk.client.put(ROUTES, headers=ops, json={"routes": {"intent": jev}})
    assert routed.status_code == 200, routed.text
    assert routed.json()["scenes"]["intent"] == "意图判断"
    desk.ctx.llms.invalidate()

    body = (await desk.client.get("/api/v1/ai/settings", headers=desk.admin)).json()
    assert (body["intent_source"], body["intent_model"]) == ("judge", "Jev（jev-1.13.0）")
    alice = await desk.agent("alice")
    _, session_id = await _serving(desk, alice, "这个多少钱")
    assert (await _intent(desk, alice, session_id))["stage"] == 2
    assert _decide_calls(fake_llm)[-1]["model"] == "jev-1.13.0"
    [row] = await desk.sql("SELECT provider, cost FROM llm_calls WHERE scene = 'intent'")
    assert row["provider"] == "Jev" and row["cost"] > 0

    # 路由到 OpenAI 兼容的供应商：用它的轻量模型按同样的问题输出 JSON。
    chat_provider = await desk.client.post(
        PROVIDERS,
        headers=ops,
        json={
            "name": "chat",
            "base_url": FAKE_URL,
            "chat_model": "fake-chat",
            "fast_model": "fake-fast",
            "is_default": True,
        },
    )
    assert chat_provider.status_code == 201, chat_provider.text
    routed = await desk.client.put(
        ROUTES, headers=ops, json={"routes": {"intent": chat_provider.json()["id"]}}
    )
    assert routed.status_code == 200, routed.text
    desk.ctx.llms.invalidate()
    body = (await desk.client.get("/api/v1/ai/settings", headers=desk.admin)).json()
    assert (body["intent_source"], body["intent_model"]) == ("llm", "chat")
    _, session_id = await _serving(desk, alice, "我要两个，什么时候发货")
    judged = await _intent(desk, alice, session_id)
    assert (judged["source"], judged["stage"], judged["model"]) == ("llm", 4, "fake-fast")
    asked = [
        r
        for r in fake_llm.requests
        if r.get("messages") and r["messages"][0]["content"].startswith("任务：意图判断")
    ]
    assert asked and asked[-1]["model"] == "fake-fast"
    assert asked[-1]["response_format"] == {"type": "json_object"}

    # 删除判断模型时路由里一并去掉。
    deleted = await desk.client.delete(f"{PROVIDERS}/{jev}", headers=ops)
    assert deleted.status_code == 204
