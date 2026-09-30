"""坐席助手（设计文档 §11.4）：人工接待时的实时提醒、会话小结写入客户档案、AI 登记的线索确认。"""

import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.ai import summaries
from tests.desk import Agent, Desk, Visitor
from tests.fake_llm import FakeLLM
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_ai_gateway_g5 import PURCHASE, _faq, _settings, _tools_provider
from tests.test_ai_reception import enable_ai


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def _serving(desk: Desk, agent: Agent, first: str) -> tuple[Visitor, str]:
    visitor = await desk.visitor()
    await desk.say(visitor, first)
    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", agent.staff_id)
    return visitor, str(chat["id"])


async def _send(desk: Desk, agent: Agent, session_id: str, text: str) -> None:
    response = await desk.client.post(
        f"/api/v1/sessions/{session_id}/messages",
        headers=agent.headers,
        json={"client_msg_id": uuid.uuid4().hex, "text": text},
    )
    assert response.status_code == 200, response.text
    await desk.flush()


def _alerts_to(desk: Desk, agent: Agent) -> list[str]:
    return [s["kind"] for s in desk.im.signals_to(agent.im_user) if s["type"] == "copilot.alert"]


async def test_realtime_alerts_for_the_serving_agent(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor, session_id = await _serving(desk, alice, "你好，想问一下订单的事")
    assert _alerts_to(desk, alice) == []

    await desk.say(visitor, "等了一个星期还没发货，我很生气")
    assert _alerts_to(desk, alice) == ["negative"]
    # 最近几条里多次出现负面情绪：提醒情绪持续激动（每个会话一次）。
    await desk.say(visitor, "你们就是骗人的！！")
    assert _alerts_to(desk, alice) == ["negative", "escalation"]
    # 10 分钟内不再重复提醒情绪。
    await desk.say(visitor, "太差劲了")
    assert _alerts_to(desk, alice) == ["negative", "escalation"]

    await desk.say(visitor, "我的身份证号是 110101199003071234，帮我查一下")
    assert _alerts_to(desk, alice)[-1] == "sensitive_info"
    # 手机号是常规的联系方式，不提醒。
    await desk.say(visitor, "电话 13800001111")
    assert len(_alerts_to(desk, alice)) == 3

    await _send(desk, alice, session_id, "非常抱歉，我保证今天给您处理好")
    signals = [s for s in desk.im.signals_to(alice.im_user) if s["type"] == "copilot.alert"]
    assert signals[-1]["kind"] == "promise"
    assert "保证" in signals[-1]["text"]
    assert signals[-1]["session_id"] == session_id

    response = await desk.client.get(f"/api/v1/sessions/{session_id}/alerts", headers=alice.headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [a["kind"] for a in items] == ["negative", "escalation", "sensitive_info", "promise"]
    assert all(a["staff_id"] == str(alice.staff_id) for a in items)
    assert "身份证号" in items[2]["text"]
    # 提醒只给坐席：客户所在的服务群里没有这些信令，也看不到这个会话的员工拿不到。
    assert all(s.get("type") != "copilot.alert" for s in desk.im.group_signals(visitor.group_id))
    bob = await desk.agent("bob", online=False)
    hidden = await desk.client.get(f"/api/v1/sessions/{session_id}/alerts", headers=bob.headers)
    assert hidden.status_code == 404


async def _summary(desk: Desk, agent: Agent, session_id: str) -> Any:
    response = await desk.client.get(
        f"/api/v1/sessions/{session_id}/summary", headers=agent.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_session_summary_is_confirmed_into_the_customer_profile(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor, session_id = await _serving(desk, alice, "我买的耳机有杂音")
    await desk.say(visitor, "想退货，怎么操作")
    await _send(desk, alice, session_id, "您在订单页点申请退货，审核后寄回即可。")
    # 会话结束前不能生成小结。
    early = await desk.client.post(f"/api/v1/sessions/{session_id}/summary", headers=alice.headers)
    assert early.status_code == 409
    closed = await desk.client.post(f"/api/v1/sessions/{session_id}/close", headers=alice.headers)
    assert closed.status_code == 200, closed.text
    assert await _summary(desk, alice, session_id) is None

    # 调度任务为结束的人工会话生成草稿；已有小结的会话不再重复生成。
    assert await summaries.run_pending(desk.ctx) == 1
    assert await summaries.run_pending(desk.ctx) == 0
    draft = await _summary(desk, alice, session_id)
    assert draft["status"] == "draft"
    assert draft["tags"] == ["售后"]
    assert "想退货" in draft["summary"]

    bob = await desk.agent("bob", online=False)
    hidden = await desk.client.post(
        f"/api/v1/sessions/{session_id}/summary/confirm",
        headers=bob.headers,
        json={"summary": "x", "tags": []},
    )
    assert hidden.status_code == 404

    confirmed = await desk.client.post(
        f"/api/v1/sessions/{session_id}/summary/confirm",
        headers=alice.headers,
        json={"summary": "耳机有杂音，客户要求退货，已告知退货流程。", "tags": ["售后", "耳机"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert (body["status"], body["tags"], body["confirmed_by"]) == (
        "confirmed",
        ["售后", "耳机"],
        str(alice.staff_id),
    )
    customer_id = draft["customer_id"]
    customer = (
        await desk.client.get(f"/api/v1/customers/{customer_id}", headers=desk.admin)
    ).json()
    assert {"售后", "耳机"} <= set(customer["tags"])
    assert customer["notes"].startswith("【")
    assert "会话小结】耳机有杂音" in customer["notes"].splitlines()[0]
    listed = await desk.client.get(f"/api/v1/customers/{customer_id}/summaries", headers=desk.admin)
    assert [s["session_id"] for s in listed.json()["items"]] == [session_id]

    # 已确认的小结不能再改、不能重新生成、不能丢弃。
    for path in ("summary", "summary/discard"):
        again = await desk.client.post(
            f"/api/v1/sessions/{session_id}/{path}", headers=alice.headers
        )
        assert again.status_code == 409, path
    [audit] = await desk.sql(
        "SELECT resource_id FROM audit_logs WHERE action = 'customer.summary_confirm'"
    )
    assert audit["resource_id"] == customer_id


async def test_summary_can_be_generated_now_or_discarded(desk: Desk, fake_llm: FakeLLM) -> None:
    alice = await desk.agent("alice")
    _, session_id = await _serving(desk, alice, "发票怎么开")
    await desk.client.post(f"/api/v1/sessions/{session_id}/close", headers=alice.headers)

    fake_llm.mode = "down"
    down = await desk.client.post(f"/api/v1/sessions/{session_id}/summary", headers=alice.headers)
    assert down.status_code == 503
    fake_llm.mode = "normal"
    generated = await desk.client.post(
        f"/api/v1/sessions/{session_id}/summary", headers=alice.headers
    )
    assert generated.status_code == 200, generated.text
    assert (generated.json()["status"], generated.json()["tags"]) == ("draft", ["咨询"])

    discarded = await desk.client.post(
        f"/api/v1/sessions/{session_id}/summary/discard", headers=alice.headers
    )
    assert discarded.json()["status"] == "discarded"
    # 丢弃的小结不能确认，也不会被调度任务重新生成；需要时可以手动重新生成。
    confirm = await desk.client.post(
        f"/api/v1/sessions/{session_id}/summary/confirm",
        headers=alice.headers,
        json={"summary": "x"},
    )
    assert confirm.status_code == 404
    assert await summaries.run_pending(desk.ctx) == 0
    again = await desk.client.post(f"/api/v1/sessions/{session_id}/summary", headers=alice.headers)
    assert again.json()["status"] == "draft"
    customer = (
        await desk.client.get(
            f"/api/v1/customers/{again.json()['customer_id']}", headers=desk.admin
        )
    ).json()
    assert customer["notes"] in (None, "")


async def test_ai_lead_drafts_are_confirmed_by_agents(
    desk: Desk, app: FastAPI, fake_llm: FakeLLM
) -> None:
    await enable_ai(desk)
    await _tools_provider(desk, app)
    await _settings(desk, tools_enabled=True)
    await _faq(desk, "批量采购有优惠吗？", PURCHASE, "想批量采购")
    fields = {
        "name": "王先生",
        "company": "星河科技",
        "phone": "[手机号1]",
        "requirement": "采购 100 台",
    }
    fake_llm.tool_plan = [("save_lead_info", fields)]
    visitor = await desk.visitor()
    await desk.say(visitor, "我是星河科技的王先生，电话13800001111，想批量采购100台")
    chat = await desk.session_of(visitor)
    customer_id = str(chat["customer_id"])

    listed = await desk.client.get(
        f"/api/v1/customers/{customer_id}/lead-drafts", headers=desk.admin
    )
    assert listed.status_code == 200, listed.text
    [draft] = listed.json()["items"]
    assert draft["status"] == "pending"
    assert draft["fields"] == {
        "name": "王先生",
        "company": "星河科技",
        "phone": "138****1111",
        "email": None,
        "requirement": "采购 100 台",
    }
    assert draft["session_id"] == str(chat["id"])

    # 看不到这个客户的坐席不能处理。
    bob = await desk.agent("bob", online=False)
    hidden = await desk.client.post(
        f"/api/v1/customers/lead-drafts/{draft['id']}/confirm", headers=bob.headers
    )
    assert hidden.status_code == 404

    confirmed = await desk.client.post(
        f"/api/v1/customers/lead-drafts/{draft['id']}/confirm", headers=desk.admin
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    customer = (
        await desk.client.get(f"/api/v1/customers/{customer_id}", headers=desk.admin)
    ).json()
    assert (customer["display_name"], customer["company"]) == ("王先生", "星河科技")
    assert customer["phone"] == "138****1111"
    assert "AI 登记】需求：采购 100 台" in customer["notes"]
    revealed = await desk.client.get(
        f"/api/v1/customers/{customer_id}/sensitive", headers=desk.admin
    )
    assert revealed.json()["phone"] == "13800001111"

    twice = await desk.client.post(
        f"/api/v1/customers/lead-drafts/{draft['id']}/discard", headers=desk.admin
    )
    assert twice.status_code == 409

    # 再登记一次，这次忽略：档案不变。
    fake_llm.tool_plan = [("save_lead_info", {"company": "别的公司"})]
    await desk.say(visitor, "我们公司想批量采购")
    items = (
        await desk.client.get(f"/api/v1/customers/{customer_id}/lead-drafts", headers=desk.admin)
    ).json()["items"]
    pending = [d for d in items if d["status"] == "pending"]
    assert len(pending) == 1
    discarded = await desk.client.post(
        f"/api/v1/customers/lead-drafts/{pending[0]['id']}/discard", headers=desk.admin
    )
    assert discarded.json()["status"] == "discarded"
    customer = (
        await desk.client.get(f"/api/v1/customers/{customer_id}", headers=desk.admin)
    ).json()
    assert customer["company"] == "星河科技"
    actions = [
        r["action"]
        for r in await desk.sql(
            "SELECT action FROM audit_logs WHERE action LIKE 'customer.lead%' ORDER BY created_at"
        )
    ]
    assert actions == ["customer.lead_confirm", "customer.lead_discard"]


async def test_summary_job_is_not_blocked_by_tenants_without_ai(
    desk: Desk,
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 没有 AI 功能的租户先结束了会话；每批只处理一个时，也不能挡住其他租户。
    other = await Desk(app, client, fake_im, settings, database_urls).open("globex")
    bob = await other.agent("bob")
    _, blocked = await _serving(other, bob, "发票怎么开")
    await other.client.post(f"/api/v1/sessions/{blocked}/close", headers=bob.headers)
    alice = await desk.agent("alice")
    _, session_id = await _serving(desk, alice, "我买的耳机有杂音")
    await desk.client.post(f"/api/v1/sessions/{session_id}/close", headers=alice.headers)

    real = summaries.has_feature

    async def feature(session: Any, tenant_id: uuid.UUID, key: str) -> bool:
        return tenant_id != other.tenant_id and await real(session, tenant_id, key)

    monkeypatch.setattr(summaries, "has_feature", feature)
    monkeypatch.setattr(summaries, "BATCH", 1)
    assert await summaries.run_pending(desk.ctx) == 1
    assert (await _summary(desk, alice, session_id))["status"] == "draft"
    assert await summaries.run_pending(desk.ctx) == 0
