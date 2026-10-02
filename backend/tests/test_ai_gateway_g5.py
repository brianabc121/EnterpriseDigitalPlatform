"""大模型网关与回复流水线的增强（设计文档 §11.1、§11.5）：问题改写、语义缓存、工具调用、
重排序、提示词版本、费用记账与按租户的并发上限。"""

import json
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.dates import today
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway, limiter
from app.modules.usage.service import rollup_day
from tests.desk import Desk
from tests.factories import bearer, create_platform_admin, platform_login
from tests.fake_llm import FakeLLM, rerank_score
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_ai_reception import ANSWER, bot_texts, decisions, enable_ai

PROVIDERS = "/platform/v1/llm-providers"
FAKE_URL = "http://fake-llm/v1"
RETURNS = "在订单页点「申请退货」，审核通过后按页面上的地址寄回即可。"
PURCHASE = "批量采购请留下联系方式，销售会在一个工作日内回电。"


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


async def _faq(desk: Desk, title: str, content: str, *questions: str) -> str:
    response = await desk.client.post(
        "/api/v1/kb/items",
        headers=desk.admin,
        json={"title": title, "content": content, "questions": list(questions), "publish": True},
    )
    assert response.status_code == 201, response.text
    item_id: str = response.json()["id"]
    return item_id


async def _settings(desk: Desk, **changes: Any) -> dict[str, Any]:
    response = await desk.client.put("/api/v1/ai/settings", headers=desk.admin, json=changes)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _ops(app: FastAPI, client: httpx.AsyncClient) -> dict[str, str]:
    await create_platform_admin(app)
    return bearer(await platform_login(client))


def _reply_calls(fake: FakeLLM) -> int:
    return sum(
        1
        for r in fake.requests
        if r.get("messages") and r["messages"][0]["content"].startswith("任务：在线客服回复")
    )


async def test_rewrite_splits_questions_and_resolves_references(
    desk: Desk, fake_llm: FakeLLM
) -> None:
    returns = await _faq(desk, "怎么退货？", RETURNS, "如何申请退货")
    visitor = await desk.visitor()

    await desk.say(visitor, "怎么退货？快递几天能到？")
    [first] = await decisions(desk, visitor)
    assert first["signals"]["rewritten"] == ["怎么退货", "快递几天能到"]
    assert returns in {k["item_id"] for k in first["knowledge"]}
    assert len(first["knowledge"]) >= 2
    [call] = await desk.sql(
        "SELECT prompt_version FROM llm_calls WHERE scene = 'rewrite' ORDER BY created_at"
    )
    assert call["prompt_version"] == "rewrite@builtin"

    # 有上文时补全指代："那偏远地区呢" → 结合上一句客户消息检索。
    other = await desk.visitor()
    await desk.say(other, "快递几天能到")
    await desk.say(other, "那偏远地区呢")
    history = await decisions(desk, other)
    assert "rewritten" not in history[0]["signals"]
    assert history[1]["signals"]["rewritten"] == ["快递几天能到 偏远地区呢"]
    assert bot_texts(desk, other) == [ANSWER, ANSWER]

    # 关闭问题改写后不再调用改写模型。
    await _settings(desk, rewrite_enabled=False)
    before = await desk.sql("SELECT count(*) AS n FROM llm_calls WHERE scene = 'rewrite'")
    third = await desk.visitor()
    await desk.say(third, "怎么退货？快递几天能到？")
    after = await desk.sql("SELECT count(*) AS n FROM llm_calls WHERE scene = 'rewrite'")
    assert before[0]["n"] == after[0]["n"]


async def test_answer_cache_hits_and_is_cleared(desk: Desk, fake_llm: FakeLLM) -> None:
    first = await desk.visitor()
    await desk.say(first, "快递几天能到")
    assert bot_texts(desk, first) == [ANSWER]
    assert _reply_calls(fake_llm) == 1
    [cached] = await desk.sql("SELECT question, answer, hits FROM ai_answer_cache")
    assert (cached["question"], cached["answer"], cached["hits"]) == ("快递几天能到", ANSWER, 0)

    # 另一位访客问同样的问题：直接用缓存，不再调用回复模型。
    second = await desk.visitor()
    await desk.say(second, "快递几天能到")
    assert bot_texts(desk, second) == [ANSWER]
    assert _reply_calls(fake_llm) == 1
    [hit] = await decisions(desk, second)
    assert hit["signals"]["cache"] >= 0.95
    assert (await desk.sql("SELECT hits FROM ai_answer_cache"))[0]["hits"] == 1

    # 含个人信息的问题不进缓存；第二轮的回答也不进缓存。
    third = await desk.visitor()
    await desk.say(third, "我的手机号是13800001111，快递几天能到")
    await desk.say(third, "快递几天能到呢")
    assert len(await desk.sql("SELECT id FROM ai_answer_cache")) == 1

    # 知识变化后缓存清空。
    await _faq(desk, "可以开发票吗？", "可以，在订单详情里申请电子发票。")
    assert await desk.sql("SELECT id FROM ai_answer_cache") == []
    await desk.say(await desk.visitor(), "快递几天能到")
    assert len(await desk.sql("SELECT id FROM ai_answer_cache")) == 1
    # AI 设置变化后同样清空；关闭缓存后不再使用。
    await _settings(desk, answer_cache=False)
    assert await desk.sql("SELECT id FROM ai_answer_cache") == []
    calls = _reply_calls(fake_llm)
    await desk.say(await desk.visitor(), "快递几天能到")
    assert _reply_calls(fake_llm) == calls + 1
    assert await desk.sql("SELECT id FROM ai_answer_cache") == []


async def _tools_provider(desk: Desk, app: FastAPI) -> dict[str, str]:
    ops = await _ops(app, desk.client)
    created = await desk.client.post(
        PROVIDERS,
        headers=ops,
        json={
            "name": "tools",
            "base_url": FAKE_URL,
            "api_key": "sk-tools-1234",
            "chat_model": "tools-chat",
            "capabilities": {"tools": True, "context_tokens": 128000},
            "prices": {"input": 1.0, "output": 2.0},
            "is_default": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["capabilities"]["tools"] is True
    return ops


async def test_tools_capture_leads_hand_off_and_register_todos(
    desk: Desk, app: FastAPI, fake_llm: FakeLLM
) -> None:
    await _tools_provider(desk, app)
    settings = await _settings(desk, tools_enabled=True)
    assert (settings["tools_enabled"], settings["tools_supported"]) == (True, True)
    await _faq(desk, "批量采购有优惠吗？", PURCHASE, "想批量采购")
    fake_llm.tool_plan = [
        ("get_customer_profile", {}),
        (
            "save_lead_info",
            {
                "name": "王先生",
                "company": "星河科技",
                "phone": "[手机号1]",
                "requirement": "采购 100 台",
            },
        ),
    ]
    visitor = await desk.visitor()
    await desk.say(visitor, "我是星河科技的王先生，电话13800001111，想批量采购100台")

    assert bot_texts(desk, visitor) == [PURCHASE]
    [decision] = await decisions(desk, visitor)
    assert decision["signals"]["tools"] == ["get_customer_profile", "save_lead_info"]
    [draft] = await desk.sql(
        "SELECT customer_id, session_id, fields, status FROM customer_lead_drafts"
    )
    fields = json.loads(draft["fields"])
    assert draft["status"] == "pending"
    assert (fields["name"], fields["company"], fields["requirement"]) == (
        "王先生",
        "星河科技",
        "采购 100 台",
    )
    assert fields["phone_masked"] == "138****1111"
    assert "13800001111" not in draft["fields"]
    # 模型拿到的档案不含联系方式；回传给模型的内容里个人信息仍是占位符。
    tool_messages = [
        m["content"]
        for r in fake_llm.requests
        for m in r.get("messages", [])
        if m.get("role") == "tool"
    ]
    assert tool_messages and all("13800001111" not in m for m in tool_messages)
    assert any('"咨询次数"' in m for m in tool_messages)

    # 模型请求转人工：交接摘要来自工具参数，不再单独生成摘要。
    await desk.agent("alice")
    fake_llm.tool_plan = [
        ("request_human_handoff", {"reason": "需要报价", "summary": "客户想要 100 台的报价单"})
    ]
    other = await desk.visitor()
    await desk.say(other, "想批量采购，给我报个价")
    chat = await desk.session_of(other)
    assert (chat["status"], chat["handoff_reason"], chat["ai_summary"]) == (
        "human_serving",
        "model_request",
        "客户想要 100 台的报价单",
    )
    summaries = await desk.sql("SELECT id FROM llm_calls WHERE scene = 'summary'")
    assert summaries == []

    # 登记待办（回电）：进入待确认页，由人工确认。
    fake_llm.tool_plan = [
        (
            "create_todo",
            {"type": "callback", "title": "回电", "detail": "客户希望明天上午回电"},
        )
    ]
    third = await desk.visitor()
    await desk.say(third, "想批量采购，明天上午给我回电话")
    [todo] = await desk.sql("SELECT source, title, detail, status FROM todos")
    assert (todo["source"], todo["title"], todo["detail"], todo["status"]) == (
        "ai_chat",
        "回电",
        "客户希望明天上午回电",
        "pending",
    )


async def test_tools_are_dry_run_in_the_playground(
    desk: Desk, app: FastAPI, fake_llm: FakeLLM
) -> None:
    ops = await _tools_provider(desk, app)
    await _settings(desk, tools_enabled=True)
    fake_llm.tool_plan = [("save_lead_info", {"company": "星河科技"})]
    response = await desk.client.post(
        "/api/v1/ai/test", headers=desk.admin, json={"question": "快递几天能到"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["signals"]["tools"] == ["save_lead_info"]
    assert await desk.sql("SELECT id FROM customer_lead_drafts") == []
    # 模型不支持工具时不提供工具。
    fake_llm.tool_plan = [("save_lead_info", {"company": "星河科技"})]
    providers = (await desk.client.get(PROVIDERS, headers=ops)).json()["items"]
    await desk.client.patch(
        f"{PROVIDERS}/{providers[0]['id']}", headers=ops, json={"capabilities": {"tools": False}}
    )
    app.state.ctx.llms.invalidate()
    assert (await desk.client.get("/api/v1/ai/settings", headers=desk.admin)).json()[
        "tools_supported"
    ] is False
    response = await desk.client.post(
        "/api/v1/ai/test", headers=desk.admin, json={"question": "快递几天能到"}
    )
    assert "tools" not in response.json()["signals"]
    assert "tools" not in fake_llm.requests[-1]


async def test_prompt_versions_cost_and_usage(desk: Desk, app: FastAPI, fake_llm: FakeLLM) -> None:
    ops = await _tools_provider(desk, app)
    await desk.client.post("/api/v1/ai/test", headers=desk.admin, json={"question": "快递几天能到"})
    [call] = await desk.sql(
        "SELECT provider, prompt_tokens, completion_tokens, cost, prompt_version FROM llm_calls"
        " WHERE scene = 'test'"
    )
    expected = (call["prompt_tokens"] * 1.0 + call["completion_tokens"] * 2.0) / 1000
    assert call["provider"] == "tools" and call["prompt_version"] == "reply@builtin"
    assert call["cost"] == pytest.approx(expected, abs=1e-3) and call["cost"] > 0

    prompts = (await desk.client.get("/platform/v1/prompts", headers=ops)).json()["items"]
    reply = next(p for p in prompts if p["key"] == "reply")
    assert (reply["active_version"], reply["variables"]) == (
        None,
        ["company", "bot_name", "persona"],
    )
    bad = await desk.client.post(
        "/platform/v1/prompts/reply/versions",
        headers=ops,
        json={"content": "你是 {company} 的 {nickname}，回答要简短。"},
    )
    assert bad.status_code == 422
    assert "nickname" in bad.json()["error"]["message"]
    created = await desk.client.post(
        "/platform/v1/prompts/reply/versions",
        headers=ops,
        json={
            "content": "你是「{company}」的客服「{bot_name}」。回答务必简短，先说结论。",
            "note": "更简短",
            "activate": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["active_version"] == 1
    app.state.ctx.prompts.invalidate()
    await desk.client.post("/api/v1/ai/test", headers=desk.admin, json={"question": "快递几天能到"})
    system = fake_llm.requests[-1]["messages"][0]["content"]
    assert system.startswith("任务：在线客服回复\n你是「") and "先说结论" in system
    [latest] = await desk.sql(
        "SELECT prompt_version FROM llm_calls WHERE scene = 'test' ORDER BY created_at DESC LIMIT 1"
    )
    assert latest["prompt_version"] == "reply@1"
    # 改回内置模板。
    reverted = await desk.client.post(
        "/platform/v1/prompts/reply/activate", headers=ops, json={"version": None}
    )
    assert reverted.json()["active_version"] is None
    audit = await desk.sql(
        "SELECT action FROM audit_logs WHERE action LIKE 'prompt.%' ORDER BY created_at"
    )
    assert [a["action"] for a in audit] == ["prompt.version", "prompt.activate"]

    # 费用汇总进用量，运营后台按模型、租户、场景查看。
    ctx = app.state.ctx
    tz = ZoneInfo(ctx.settings.usage_timezone)
    await rollup_day(ctx.db, today(tz), tz)
    [cost] = await desk.sql("SELECT value FROM usage_daily WHERE metric = 'llm_cost'")
    assert cost["value"] >= 1
    usage = (await desk.client.get("/platform/v1/llm-usage?days=7", headers=ops)).json()
    assert usage["total_calls"] >= 2 and usage["total_cost"] > 0
    assert any(row["key"] == "tools/tools-chat" for row in usage["by_model"])
    assert any(row["label"] == "AI 设置里的试一试" for row in usage["by_scene"])


async def test_tenant_concurrency_limit(desk: Desk, app: FastAPI) -> None:
    ops = await _ops(app, desk.client)
    ctx = app.state.ctx
    limited = await desk.client.put(
        f"/platform/v1/tenants/{desk.tenant_id}/llm",
        headers=ops,
        json={"provider_id": None, "concurrency": 1},
    )
    assert (limited.json()["concurrency"], limited.json()["default_concurrency"]) == (1, 8)
    ctx.settings.llm_queue_seconds = 0.3
    messages = [{"role": "user", "content": "你好"}]
    async with limiter.slot(ctx, desk.tenant_id):
        state = await desk.client.get(f"/platform/v1/tenants/{desk.tenant_id}/llm", headers=ops)
        assert state.json()["in_use"] == 1
        with pytest.raises(LLMUnavailable):
            await gateway.chat(ctx, desk.tenant_id, messages, scene="test")
    [busy] = await desk.sql("SELECT status, error FROM llm_calls WHERE scene = 'test'")
    assert busy["status"] == "busy" and "并发" in busy["error"]
    # 名额释放后可以调用。
    result = await gateway.chat(ctx, desk.tenant_id, messages, scene="test")
    assert result.content
    # 不传 concurrency 时不修改。
    kept = await desk.client.put(
        f"/platform/v1/tenants/{desk.tenant_id}/llm", headers=ops, json={"provider_id": None}
    )
    assert kept.json()["concurrency"] == 1


async def test_rerank_orders_knowledge(desk: Desk, app: FastAPI, fake_llm: FakeLLM) -> None:
    ops = await _ops(app, desk.client)
    created = await desk.client.post(
        PROVIDERS,
        headers=ops,
        json={
            "name": "main",
            "base_url": FAKE_URL,
            "api_key": "sk-main-1234",
            "chat_model": "main-chat",
            "embed_model": "fake-embed",
            "embed_dim": 1024,
            "rerank_model": "fake-rerank",
            "prices": {"input": 2, "output": 6},
            "is_default": True,
        },
    )
    assert created.status_code == 201, created.text
    await _faq(desk, "偏远地区快递几天能到？", "偏远地区 5 到 7 天。")
    await _faq(desk, "快递可以指定时间送达吗？", "可以在下单时选择送达时间。")
    response = await desk.client.get(
        "/api/v1/kb/search", headers=desk.admin, params={"q": "偏远地区快递几天能到"}
    )
    assert response.status_code == 200, response.text
    hits = response.json()["items"]
    scores = [h["score"] for h in hits]
    assert hits[0]["title"] == "偏远地区快递几天能到？"
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == rerank_score("偏远地区快递几天能到", "偏远地区快递几天能到？")
    reranks = await desk.sql(
        "SELECT model, status, prompt_tokens, cost FROM llm_calls WHERE scene = 'rerank'"
    )
    assert reranks and reranks[-1]["model"] == "fake-rerank"
    # 供应商返回了用量：记 tokens，按输入价计费（token 计费，设计文档 §37.3）。
    used = reranks[-1]["prompt_tokens"]
    assert used > 0 and reranks[-1]["cost"] == pytest.approx(used * 2 / 1000, abs=1e-4)
