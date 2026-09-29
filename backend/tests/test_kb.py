"""知识库：条目管理、发布与检索（语义 + 关键词）、可见范围、有效期、版本、导入。"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app import cli
from app.core.config import Settings
from tests.desk import Desk
from tests.fake_llm import FakeLLM
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


async def create(desk: Desk, **body: Any) -> dict[str, Any]:
    payload = {"title": "订单发货后多久能到？", "content": "一般 2 到 3 天送达。", **body}
    response = await desk.client.post("/api/v1/kb/items", headers=desk.admin, json=payload)
    assert response.status_code == 201, response.text
    item: dict[str, Any] = response.json()
    return item


async def search(desk: Desk, q: str, headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
    response = await desk.client.get(
        "/api/v1/kb/search", headers=headers or desk.admin, params={"q": q}
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def test_published_faqs_are_found_by_similar_questions(desk: Desk) -> None:
    item = await create(desk, questions=["快递几天能到", "多久能收到货"], category="物流")
    assert (item["status"], item["version"]) == ("draft", 1)
    assert await search(desk, "订单多久能到") == []

    published = await desk.client.post(f"/api/v1/kb/items/{item['id']}/publish", headers=desk.admin)
    assert published.json()["status"] == "published"
    chunks = await desk.sql(
        "SELECT kind, embedding IS NOT NULL AS embedded FROM kb_chunks WHERE item_id = $1",
        uuid.UUID(item["id"]),
    )
    assert [(c["kind"], c["embedded"]) for c in chunks] == [("question", True)] * 3

    [hit] = await search(desk, "请问快递大概几天能到")
    assert (hit["item_id"], hit["text"]) == (item["id"], "一般 2 到 3 天送达。")
    assert hit["dense"] is not None and hit["score"] >= hit["lexical"] > 0.4
    assert await search(desk, "怎么开发票") == []


async def test_documents_are_split_and_the_best_passage_is_returned(desk: Desk) -> None:
    intro = "本手册介绍会员积分规则。" * 30
    returns = "退货说明：签收后 7 天内可以申请无理由退货，商品需保持完好，运费由买家承担。"
    doc = await create(
        desk, kind="doc", title="售后服务手册", content=f"{intro}\n\n{returns}", publish=True
    )

    [hit] = await search(desk, "签收后几天内可以无理由退货")

    assert (hit["item_id"], hit["kind"]) == (doc["id"], "doc")
    assert "7 天内可以申请无理由退货" in hit["text"]
    assert hit["text"].startswith("售后服务手册")


async def test_visibility_validity_and_roles(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    await create(
        desk, title="内部折扣怎么申请", content="找主管审批。", visibility="admin", publish=True
    )
    await create(
        desk,
        title="退款多久到账",
        content="3 个工作日内原路退回。",
        visibility="agent",
        publish=True,
    )
    expired = datetime.now(UTC) - timedelta(days=1)
    await create(
        desk,
        title="国庆活动怎么参加",
        content="活动已结束。",
        valid_from=(expired - timedelta(days=7)).isoformat(),
        valid_to=expired.isoformat(),
        publish=True,
    )
    await create(desk, title="发票怎么开", content="在订单详情里申请。")

    admin_titles = {h["title"] for h in await search(desk, "内部折扣怎么申请")}
    agent_titles = {h["title"] for h in await search(desk, "内部折扣怎么申请", alice.headers)}
    assert ("内部折扣怎么申请" in admin_titles, "内部折扣怎么申请" in agent_titles) == (True, False)
    assert [h["title"] for h in await search(desk, "退款多久到账", alice.headers)] == [
        "退款多久到账"
    ]
    assert await search(desk, "国庆活动怎么参加") == []

    listed = await desk.client.get("/api/v1/kb/items", headers=alice.headers)
    assert {i["title"] for i in listed.json()["items"]} == {"退款多久到账", "国庆活动怎么参加"}
    all_items = await desk.client.get(
        "/api/v1/kb/items", headers=desk.admin, params={"status": "draft"}
    )
    assert [i["title"] for i in all_items.json()["items"]] == ["发票怎么开"]
    forbidden = await desk.client.post(
        "/api/v1/kb/items", headers=alice.headers, json={"title": "x", "content": "y"}
    )
    assert forbidden.status_code == 403


async def test_edits_bump_the_version_and_archive_removes_from_search(desk: Desk) -> None:
    item = await create(desk, publish=True)

    edited = await desk.client.patch(
        f"/api/v1/kb/items/{item['id']}",
        headers=desk.admin,
        json={"questions": ["几天能收到"], "content": "一般 1 到 2 天送达。"},
    )

    assert (edited.json()["version"], edited.json()["status"]) == (2, "published")
    [hit] = await search(desk, "几天能收到")
    assert hit["text"] == "一般 1 到 2 天送达。"
    category_only = await desk.client.patch(
        f"/api/v1/kb/items/{item['id']}", headers=desk.admin, json={"category": "物流"}
    )
    assert category_only.json()["version"] == 2

    conflict = await desk.client.delete(f"/api/v1/kb/items/{item['id']}", headers=desk.admin)
    assert conflict.status_code == 409
    await desk.client.post(f"/api/v1/kb/items/{item['id']}/archive", headers=desk.admin)
    assert await search(desk, "几天能收到") == []
    deleted = await desk.client.delete(f"/api/v1/kb/items/{item['id']}", headers=desk.admin)
    assert deleted.status_code == 204


async def test_keyword_search_still_works_when_embeddings_fail(
    desk: Desk, fake_llm: FakeLLM
) -> None:
    fake_llm.mode = "down"
    item = await create(desk, questions=["快递几天能到"], publish=True)
    fake_llm.mode = "normal"

    [row] = await desk.sql(
        "SELECT count(*) AS n, count(embedding) AS embedded FROM kb_chunks WHERE item_id = $1",
        uuid.UUID(item["id"]),
    )
    assert (row["n"], row["embedded"]) == (2, 0)
    [hit] = await search(desk, "快递几天能到")
    assert (hit["item_id"], hit["dense"]) == (item["id"], None)


async def test_csv_import(desk: Desk) -> None:
    csv = (
        "标准问,答案,相似问,分类\n"
        "怎么开发票,在订单详情里申请电子发票。,发票怎么开|能开专票吗,发票\n"
        "缺答案的行,,,\n"
        "支持哪些付款方式,支持微信和支付宝。,,支付\n"
    )

    response = await desk.client.post(
        "/api/v1/kb/import", headers=desk.admin, json={"csv": csv, "publish": True}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"created": 2, "errors": ["第 3 行：标准问和答案不能为空"]}
    [hit] = await search(desk, "能开专票吗")
    assert hit["title"] == "怎么开发票"
    items = (await desk.client.get("/api/v1/kb/items", headers=desk.admin)).json()["items"]
    assert {(i["source"], i["category"]) for i in items} == {("import", "发票"), ("import", "支付")}
    bad = await desk.client.post(
        "/api/v1/kb/import", headers=desk.admin, json={"csv": "a,b\n1,2\n"}
    )
    assert bad.json() == {"created": 0, "errors": ["表头需要包含「标准问」和「答案」两列"]}


async def test_knowledge_is_isolated_between_tenants(desk: Desk) -> None:
    await create(desk, publish=True)
    other = await Desk(desk.app, desk.client, desk.im, desk.settings, desk.database_urls).open(
        "globex"
    )

    assert await search(other, "订单多久能到") == []
    listed = await other.client.get("/api/v1/kb/items", headers=other.admin)
    assert listed.json()["total"] == 0


async def test_reindex_command_rebuilds_chunks(desk: Desk, settings: Settings) -> None:
    item = await create(desk, questions=["快递几天能到"], publish=True)
    await desk.sql("DELETE FROM kb_chunks")

    # 命令行按配置创建大模型客户端；测试配置没有大模型，只重建关键词检索单元。
    counts = await cli.kb_reindex(settings, desk.code)

    assert counts == {desk.code: 1}
    chunks = await desk.sql(
        "SELECT embedding IS NULL AS keyword_only FROM kb_chunks WHERE item_id = $1",
        uuid.UUID(item["id"]),
    )
    assert [c["keyword_only"] for c in chunks] == [True, True]
    [hit] = await search(desk, "快递几天能到")
    assert hit["item_id"] == item["id"]
    with pytest.raises(SystemExit):
        await cli.kb_reindex(settings, "nope")
