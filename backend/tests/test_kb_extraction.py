"""从会话提炼知识（设计文档 §12.4）与审核台（§12.5）。"""

import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.conversation.models import Message
from app.modules.kb.extraction import consistent, run_extraction, transcript
from tests.desk import Agent, Desk
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
    created = await desk.client.post(
        "/api/v1/kb/items",
        headers=desk.admin,
        json={"title": "订单发货后多久能到？", "content": ANSWER, "publish": True},
    )
    assert created.status_code == 201, created.text
    return desk


async def serve(desk: Desk, agent: Agent, *turns: tuple[str, str]) -> dict[str, Any]:
    """一次人工接待：访客与坐席按顺序对话（("客户"|"坐席", 内容)），然后坐席结束会话。"""
    visitor = await desk.visitor()
    chat: dict[str, Any] | None = None
    for role, text in turns:
        if role == "客户":
            await desk.say(visitor, text)
            chat = dict(await desk.session_of(visitor))
        else:
            assert chat is not None
            sent = await desk.client.post(
                f"/api/v1/sessions/{chat['id']}/messages",
                headers=agent.headers,
                json={"client_msg_id": uuid.uuid4().hex, "text": text},
            )
            assert sent.status_code == 200, sent.text
            await desk.flush()
    assert chat is not None
    closed = await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=agent.headers)
    assert closed.status_code == 200, closed.text
    await desk.flush()
    return chat


async def candidates(desk: Desk, **params: Any) -> list[dict[str, Any]]:
    response = await desk.client.get("/api/v1/kb/candidates", headers=desk.admin, params=params)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def test_answers_are_consistent_unless_facts_change() -> None:
    assert consistent("一般 2 到 3 天就到了", ANSWER)
    assert not consistent("现在一般 1 到 2 天送达", ANSWER)
    assert not consistent("不支持货到付款", "支持货到付款")


async def test_conversations_become_clustered_candidates(desk: Desk) -> None:
    alice = await desk.agent("alice")
    await serve(
        desk,
        alice,
        ("客户", "你好"),
        ("客户", "你们周末发货吗"),
        ("坐席", "周末正常发货，周日下午 4 点前的订单当天发出。"),
        ("客户", "谢谢"),
    )
    await serve(desk, alice, ("客户", "周末发货吗"), ("坐席", "周末也发货。"))

    report = await run_extraction(desk.ctx)

    assert (report.sessions, report.pairs, report.failed) == (2, 2, 0)
    [candidate] = await candidates(desk)
    assert (candidate["kind"], candidate["occurrences"], candidate["recent"]) == ("new", 2, 2)
    assert candidate["question"] == "你们周末发货吗"
    assert candidate["variants"] == ["你们周末发货吗", "周末发货吗"]
    assert (candidate["model"], candidate["prompt_version"]) == ("fake-chat", "extract@builtin")
    detail = await desk.client.get(f"/api/v1/kb/candidates/{candidate['id']}", headers=desk.admin)
    evidence = detail.json()["evidence"]
    assert [line["role"] for line in evidence[0]["lines"]] == ["客户", "坐席"]
    # 寒暄已去掉。
    assert all("谢谢" not in line["text"] for e in evidence for line in e["lines"])
    # 同一批会话不会重复提炼。
    again = await run_extraction(desk.ctx)
    assert again.sessions == 0


async def test_existing_knowledge_yields_similar_questions_and_conflicts(desk: Desk) -> None:
    alice = await desk.agent("alice")
    await serve(desk, alice, ("客户", "订单发货后多久能到"), ("坐席", "一般 2 到 3 天就到了。"))
    await serve(desk, alice, ("客户", "请问订单发货后多久能到呀"), ("坐席", "一般 2 到 3 天。"))
    await serve(desk, alice, ("客户", "订单发货后多久能到？"), ("坐席", "现在一般 1 到 2 天送达。"))

    await run_extraction(desk.ctx)

    found = {c["kind"]: c for c in await candidates(desk)}
    # 第一次问法与已有问答完全相同，不需要审核。
    assert set(found) == {"similar", "conflict"}
    assert found["similar"]["question"] == "请问订单发货后多久能到呀"
    assert found["similar"]["target_title"] == "订单发货后多久能到？"
    assert found["conflict"]["answer"] == "现在一般 1 到 2 天送达。"


async def test_unanswered_questions_become_gaps_and_personal_details_stay_out(
    desk: Desk,
) -> None:
    alice = await desk.agent("alice")
    await serve(
        desk,
        alice,
        ("客户", "可以开增值税专用发票吗"),
        ("坐席", "这个我帮您问一下，稍后回复您。"),
        ("客户", "我的手机号 13812345678 能改收货地址吗"),
        ("坐席", "可以的，在订单详情里修改收货地址即可。"),
    )

    await run_extraction(desk.ctx)

    [gap] = await candidates(desk, kind="gap")
    assert (gap["question"], gap["answer"]) == ("可以开增值税专用发票吗", None)
    # 含个人信息的问答不进入候选；证据里的手机号已脱敏。
    assert await candidates(desk, kind="new") == []
    detail = await desk.client.get(f"/api/v1/kb/candidates/{gap['id']}", headers=desk.admin)
    assert "13812345678" not in detail.text
    rows = await desk.sql("SELECT evidence::text AS e FROM kb_candidates")
    assert all("13812345678" not in r["e"] for r in rows)


async def test_only_worthwhile_sessions_are_extracted_and_failures_retry(
    desk: Desk, fake_llm: FakeLLM
) -> None:
    alice = await desk.agent("alice")
    await serve(desk, alice, ("客户", "你们周末发货吗"), ("坐席", "周末正常发货。"))
    fake_llm.mode = "down"

    failed = await run_extraction(desk.ctx)
    fake_llm.mode = "normal"
    retried = await run_extraction(desk.ctx)

    assert (failed.sessions, failed.failed) == (1, 1)
    assert (retried.sessions, retried.pairs) == (1, 1)
    [row] = await desk.sql("SELECT status, attempts FROM kb_extractions")
    assert (row["status"], row["attempts"]) == ("done", 2)
    # 关闭自动提炼后跳过这个租户。
    await desk.client.put(
        "/api/v1/ai/settings", headers=desk.admin, json={"extraction_enabled": False}
    )
    await serve(desk, alice, ("客户", "能开发票吗"), ("坐席", "可以开电子发票。"))
    assert (await run_extraction(desk.ctx)).sessions == 0


async def test_review_desk_turns_candidates_into_knowledge(desk: Desk) -> None:
    alice = await desk.agent("alice")
    await serve(desk, alice, ("客户", "你们周末发货吗"), ("坐席", "周末正常发货。"))
    await serve(desk, alice, ("客户", "请问订单发货后多久能到呀"), ("坐席", "一般 2 到 3 天。"))
    await serve(desk, alice, ("客户", "订单发货后多久能到？"), ("坐席", "现在一般 1 到 2 天送达。"))
    await serve(desk, alice, ("客户", "可以开增值税专用发票吗"), ("坐席", "我帮您问一下。"))
    await run_extraction(desk.ctx)
    found = {c["kind"]: c for c in await candidates(desk)}

    async def act(candidate: dict[str, Any], action: str, **body: Any) -> httpx.Response:
        return await desk.client.post(
            f"/api/v1/kb/candidates/{candidate['id']}/{action}", headers=desk.admin, json=body
        )

    # 新问题：编辑后通过，新建为问答并发布，AI 与坐席马上可以检索到。
    approved = await act(found["new"], "approve", answer="周末正常发货，节假日另行通知。")
    assert approved.json()["status"] == "approved"
    item_id = approved.json()["result_item_id"]
    item = (await desk.client.get(f"/api/v1/kb/items/{item_id}", headers=desk.admin)).json()
    assert (item["source"], item["status"], item["content"]) == (
        "extracted",
        "published",
        "周末正常发货，节假日另行通知。",
    )
    hits = await desk.client.get(
        "/api/v1/kb/search", headers=alice.headers, params={"q": "周末发货吗"}
    )
    assert hits.json()["items"][0]["item_id"] == item_id

    # 相似问法：并入原问答（新版本）。
    merged = await act(found["similar"], "approve")
    assert merged.json()["status"] == "merged"
    original = (
        await desk.client.get(
            f"/api/v1/kb/items/{merged.json()['result_item_id']}", headers=desk.admin
        )
    ).json()
    assert "请问订单发货后多久能到呀" in original["questions"]
    assert original["version"] == 2

    # 冲突：用新答案更新原问答。
    updated = await act(found["conflict"], "approve")
    assert updated.json()["status"] == "approved"
    versions = await desk.client.get(
        f"/api/v1/kb/items/{original['id']}/versions", headers=desk.admin
    )
    assert [(v["version"], v["change"]) for v in versions.json()["items"]][:2] == [
        (3, "updated"),
        (2, "merged"),
    ]

    # 缺口：必须补充答案；驳回需要理由；处理过的不能再处理。
    assert (await act(found["gap"], "approve")).status_code == 422
    assert (await act(found["gap"], "reject", reason="")).status_code == 422
    rejected = await act(found["gap"], "reject", reason="专票需要财务确认，暂不入库")
    assert (rejected.json()["status"], rejected.json()["review_note"]) == (
        "rejected",
        "专票需要财务确认，暂不入库",
    )
    assert (await act(found["gap"], "approve", answer="可以")).status_code == 409
    assert await candidates(desk) == []
    history = await candidates(desk, status="rejected")
    assert history[0]["reviewed_by_name"] == "管理员"


async def test_candidates_can_be_merged_into_chosen_knowledge(desk: Desk) -> None:
    alice = await desk.agent("alice")
    await serve(desk, alice, ("客户", "快递一般几天能收到"), ("坐席", "一般 2 到 3 天。"))
    await run_extraction(desk.ctx)
    [candidate] = await candidates(desk)
    [target] = (await desk.client.get("/api/v1/kb/items", headers=desk.admin)).json()["items"]

    merged = await desk.client.post(
        f"/api/v1/kb/candidates/{candidate['id']}/merge",
        headers=desk.admin,
        json={"item_id": target["id"]},
    )

    assert merged.json()["status"] == "merged"
    item = (await desk.client.get(f"/api/v1/kb/items/{target['id']}", headers=desk.admin)).json()
    assert item["questions"] == ["快递一般几天能收到"]


async def test_similar_questions_can_merge_automatically(desk: Desk) -> None:
    alice = await desk.agent("alice")
    await desk.client.put(
        "/api/v1/ai/settings", headers=desk.admin, json={"auto_merge_similar": True}
    )
    for _ in range(3):
        await serve(desk, alice, ("客户", "请问订单发货后多久能到呀"), ("坐席", "一般 2 到 3 天。"))

    await run_extraction(desk.ctx)

    [merged] = await candidates(desk, status="merged")
    assert merged["review_note"] == "自动合并（证据 3 条）"
    [item] = (await desk.client.get("/api/v1/kb/items", headers=desk.admin)).json()["items"]
    assert item["questions"] == ["请问订单发货后多久能到呀"]


async def test_agents_cannot_use_the_review_desk(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    assert (
        await desk.client.get("/api/v1/kb/candidates", headers=alice.headers)
    ).status_code == 403


def test_transcript_masks_merges_and_drops_greetings() -> None:
    def message(sender: str, text: str) -> Message:
        return Message(sender_type=sender, text_plain=text)

    lines = transcript(
        [
            message("customer", "你好"),
            message("customer", "我的手机 13812345678"),
            message("customer", "能改地址吗"),
            message("system", "客服 小艾 为您服务"),
            message("agent", "可以的"),
            message("customer", "好的！"),
        ]
    )

    assert lines == [("客户", "我的手机 [手机号1]\n能改地址吗"), ("坐席", "可以的")]
