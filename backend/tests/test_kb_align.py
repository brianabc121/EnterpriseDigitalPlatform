"""知识库整理（设计文档 §33.7）：对照现行的规章制度找出冲突、缺失和重复的知识，建议进审核台；
增量更新索引（§33.9）：知识库和制度都没变时跳过，只核对 change_seq 变了的知识。"""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.kb import align
from app.modules.kb import search as kb_search
from app.modules.kb.models import KbItem
from app.modules.wake import queue, runner
from app.modules.wake.models import RunKind, RunTrigger
from tests.desk import Desk
from tests.fake_llm import FakeLLM
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls

KB = "/api/v1/kb"
POLICY = """# 售后服务制度
## 退货
退货期限：自签收之日起 15 天内可以申请无理由退货，退回运费由公司承担。
## 换货
换货期限：收到商品 30 天内出现质量问题可以换货。"""


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def item(desk: Desk, title: str, content: str, **extra: Any) -> dict[str, Any]:
    body = {"kind": "faq", "title": title, "content": content, "publish": True, **extra}
    response = await desk.client.post(f"{KB}/items", headers=desk.admin, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def align_now(desk: Desk, *, force: bool = False) -> dict[str, Any]:
    now = datetime.now(UTC)
    trigger = RunTrigger.MANUAL if force else RunTrigger.SCHEDULE
    async with desk.ctx.db.tenant_session(desk.tenant_id) as session:
        run = await queue.enqueue(session, desk.tenant_id, RunKind.KB, trigger, now=now)
        await session.commit()
        run_id = run.id
    outcome = await runner._kb(desk.ctx, run_id, desk.tenant_id, now, force=force)
    return outcome.stats


async def candidates(desk: Desk) -> list[dict[str, Any]]:
    response = await desk.client.get(f"{KB}/candidates?source=policy&limit=100", headers=desk.admin)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def knowledge(desk: Desk) -> dict[str, dict[str, Any]]:
    return {
        "policy": await item(
            desk, "售后服务制度", POLICY, kind="doc", policy=True, category="制度"
        ),
        "return": await item(desk, "退货期限是多久？", "签收后 7 天内可以退货，运费自理。"),
        "contact": await item(desk, "怎么联系人工客服？", "在对话框里输入人工即可转接。"),
        "invoice": await item(desk, "发票怎么开？", "下单时填写抬头和税号，发货后开具。"),
        "invoice2": await item(
            desk, "发票怎么开", "联系客服提供抬头和税号。", questions=["可以开专票吗"]
        ),
    }


async def test_alignment_finds_conflicts_gaps_and_duplicates(desk: Desk, fake_llm: FakeLLM) -> None:
    items = await knowledge(desk)
    stats = await align_now(desk)
    assert stats["skipped"] is False and stats["errors"] == 0
    assert (stats["policies"], stats["items"]) == (1, 4)
    assert stats["conflict"] == 1 and stats["duplicate"] == 1 and stats["gap"] >= 1

    found = {(c["kind"], c["question"]): c for c in await candidates(desk)}
    conflict = found[("conflict", "退货期限是多久？")]
    assert conflict["target_item_id"] == items["return"]["id"]
    assert conflict["answer"] == "签收后 15 天内可以退货，运费自理。"
    detail = (await desk.client.get(f"{KB}/candidates/{conflict['id']}", headers=desk.admin)).json()
    [evidence] = detail["evidence"]
    assert (evidence["kind"], evidence["policy_title"]) == ("policy", "售后服务制度")
    assert "15 天" in evidence["excerpt"] and evidence["reason"] == "知识里是 7 天，制度规定 15 天"

    gap = found[("new", "换货期限是怎么规定的？")]
    assert gap["answer"] == "收到商品 30 天内出现质量问题可以换货。"
    duplicate = next(c for (kind, _), c in found.items() if kind == "duplicate")
    assert {duplicate["target_item_id"]} <= {items["invoice"]["id"], items["invoice2"]["id"]}

    # 通知知识管理员（来源"制度对齐"）。
    notes = await desk.sql(
        "SELECT title FROM staff_notifications WHERE tenant_id = $1 AND kind = 'kb_align'",
        desk.tenant_id,
    )
    assert [n["title"] for n in notes] == [
        f"知识库整理：1 条和现行制度冲突、建议新增 {stats['gap']} 条问答、1 组重复"
    ]
    # 交给大模型的知识和制度都经过脱敏；任务名按场景区分。
    tasks = {
        r["messages"][0]["content"].split("\n", 1)[0] for r in fake_llm.requests if "messages" in r
    }
    assert {"任务：知识与制度核对", "任务：制度转问答"} <= tasks

    # 再整理一次：知识库和制度都没变（增量更新索引里 kb_items 的编号一样），直接跳过。
    calls = len(fake_llm.requests)
    again = await align_now(desk)
    assert again["skipped"] is True and again["checked"] == 0 and again["searched"] == 0
    assert len(fake_llm.requests) == calls
    assert len(await candidates(desk)) == len(found)


async def test_only_changed_knowledge_is_rechecked(desk: Desk) -> None:
    items = await knowledge(desk)
    await align_now(desk)
    # 改了一条问答的分类：只检索这一条（版本没变，不用再问大模型）。
    updated = await desk.client.patch(
        f"{KB}/items/{items['contact']['id']}", headers=desk.admin, json={"category": "服务"}
    )
    assert updated.status_code == 200, updated.text
    stats = await align_now(desk)
    assert stats["skipped"] is False
    assert (stats["searched"], stats["checked"], stats["unchanged"]) == (1, 0, 4)

    # 制度改了（15 天改成 20 天）：现行制度的指纹变了，全部重新检索；冲突的那条更新已有的建议，
    # 不重复生成；制度变化后 10 分钟自动整理已经排进队列。
    policy = await desk.client.patch(
        f"{KB}/items/{items['policy']['id']}",
        headers=desk.admin,
        json={"content": POLICY.replace("15 天", "20 天")},
    )
    assert policy.status_code == 200, policy.text
    queued = await desk.sql(
        "SELECT trigger, not_before > now() + interval '9 minutes' AS later FROM wake_runs"
        " WHERE tenant_id = $1 AND kind = 'kb' AND status = 'queued'",
        desk.tenant_id,
    )
    assert [(r["trigger"], r["later"]) for r in queued] == [("event", True)]
    stats = await align_now(desk)
    assert stats["searched"] == 4 and stats["conflict"] == 1
    conflicts = [c for c in await candidates(desk) if c["kind"] == "conflict"]
    assert [c["answer"] for c in conflicts] == ["签收后 20 天内可以退货，运费自理。"]


async def test_review_desk_merges_duplicates_and_applies_conflicts(desk: Desk) -> None:
    items = await knowledge(desk)
    await align_now(desk)
    found = await candidates(desk)
    duplicate = next(c for c in found if c["kind"] == "duplicate")
    keep = duplicate["target_item_id"]
    other = items["invoice2"]["id"] if keep == items["invoice"]["id"] else items["invoice"]["id"]
    merged = await desk.client.post(
        f"{KB}/candidates/{duplicate['id']}/approve", headers=desk.admin, json={}
    )
    assert merged.status_code == 200, merged.text
    assert merged.json()["status"] == "merged"
    kept = (await desk.client.get(f"{KB}/items/{keep}", headers=desk.admin)).json()
    gone = (await desk.client.get(f"{KB}/items/{other}", headers=desk.admin)).json()
    # 另一条下线，它的问法并进保留的一条（和标准问一样的不重复加）。
    assert gone["status"] == "archived"
    assert set(gone["questions"]) <= set(kept["questions"]) and kept["version"] == 2

    conflict = next(c for c in found if c["kind"] == "conflict")
    applied = await desk.client.post(
        f"{KB}/candidates/{conflict['id']}/approve", headers=desk.admin, json={}
    )
    assert applied.status_code == 200, applied.text
    return_path = f"{KB}/items/{items['return']['id']}"
    fixed = (await desk.client.get(return_path, headers=desk.admin)).json()
    assert fixed["content"] == "签收后 15 天内可以退货，运费自理。" and fixed["version"] == 2

    # 处理完再整理：改过的那条和制度一致了，不再有新的建议。
    stats = await align_now(desk)
    assert stats["conflict"] == 0 and stats["duplicate"] == 0


async def test_hold_conflicts_pauses_them_for_customers_only(desk: Desk) -> None:
    items = await knowledge(desk)
    await align_now(desk)

    async def visible(customer_facing: bool) -> set[str]:
        async with desk.ctx.db.tenant_session(desk.tenant_id) as session:
            hits = await kb_search.search(
                desk.ctx,
                session,
                desk.tenant_id,
                "退货期限是多久",
                visibilities=("public",),
                customer_facing=customer_facing,
            )
        return {str(h.item_id) for h in hits}

    assert items["return"]["id"] in await visible(customer_facing=True)
    current = (await desk.client.get("/api/v1/wake/settings", headers=desk.admin)).json()
    saved = await desk.client.put(
        "/api/v1/wake/settings",
        headers=desk.admin,
        json={**current["settings"], "kb_hold_conflicts": True},
    )
    assert saved.status_code == 200, saved.text
    assert items["return"]["id"] not in await visible(customer_facing=True)
    assert items["return"]["id"] in await visible(customer_facing=False)


async def test_alignment_api_policy_flag_and_permissions(desk: Desk) -> None:
    await knowledge(desk)
    policies = (await desk.client.get(f"{KB}/items?policy=true", headers=desk.admin)).json()
    assert [i["title"] for i in policies["items"]] == ["售后服务制度"]
    assert policies["items"][0]["policy"] is True

    empty = (await desk.client.get(f"{KB}/alignment", headers=desk.admin)).json()
    assert (empty["finished_at"], empty["policies"], empty["pending"]) == (None, 1, 0)
    assert empty["running"] is True  # 新建的制度已经排进了"制度变化后自动整理"
    queued = await desk.client.post(f"{KB}/alignment/run", headers=desk.admin)
    assert queued.status_code == 200, queued.text
    assert queued.json()["kind"] == "kb"
    # 立即整理合并到排队中的那一条，并且马上开始。
    assert await runner.run_due(desk.ctx) == 1
    report = (await desk.client.get(f"{KB}/alignment", headers=desk.admin)).json()
    assert report["finished_at"] is not None and report["running"] is False
    assert report["pending"] == report["report"]["pending"] >= 3
    assert report["report"]["policies"] == 1

    agent = await desk.agent("amy", online=False)
    assert (await desk.client.get(f"{KB}/alignment", headers=agent.headers)).status_code == 403
    denied = await desk.client.post(f"{KB}/alignment/run", headers=agent.headers)
    assert denied.status_code == 403


async def test_policy_changes_queue_one_alignment(desk: Desk) -> None:
    """制度新增、修改、下线后 10 分钟整理；几次修改合并成一次；关闭了就不排。"""
    policy = await item(desk, "价格管理规定", "最低折扣：不得低于 8 折。", kind="doc", policy=True)
    await desk.client.patch(
        f"{KB}/items/{policy['id']}",
        headers=desk.admin,
        json={"content": "最低折扣：不低于 7 折。"},
    )
    archived = await desk.client.post(f"{KB}/items/{policy['id']}/archive", headers=desk.admin)
    assert archived.status_code == 200, archived.text
    rows = await desk.sql(
        "SELECT trigger FROM wake_runs WHERE tenant_id = $1 AND kind = 'kb'", desk.tenant_id
    )
    assert [r["trigger"] for r in rows] == ["event"]
    # 不是制度的知识不触发。
    await desk.sql("DELETE FROM wake_runs WHERE tenant_id = $1", desk.tenant_id)
    await item(desk, "营业时间", "每天 9 点到 18 点。")
    assert await desk.sql("SELECT 1 FROM wake_runs WHERE tenant_id = $1", desk.tenant_id) == []


def test_policy_fingerprint_changes_with_versions() -> None:
    first, second = KbItem(id=uuid.uuid4(), version=1), KbItem(id=uuid.uuid4(), version=1)
    base = align.policy_fingerprint([first, second])
    assert align.policy_fingerprint([second, first]) == base
    assert align.policy_fingerprint([second]) != base
    first.version = 2
    assert align.policy_fingerprint([first, second]) != base
