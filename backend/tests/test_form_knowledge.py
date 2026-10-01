"""表单填写知识库（设计文档 §25.18）：每次提交表单都判断一次要不要更新表单知识——叫法（录入行的
输入、客户的说法）、用量（仓管确认的领料单）、搭配（一起开的商品）；开单时联想、一键领料、AI 下单
用上学到的知识；知识库页面的列表、依据、变化记录、手工添加和修改、确认、停用、学习记录和设置。"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.products import service as product_service
from tests.desk import Agent, Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import call, customer, new_order
from tests.test_requisition_assist import draft, keeper, past
from tests.test_warehouse import adjust, confirmed, goods, material, recipe, worker

FORM_KB = "/api/v1/form-kb"


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


def line(product: dict[str, Any], quantity: int = 1, **entry: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"product_id": product["id"], "quantity": quantity}
    if entry:
        body["entry"] = entry
    return body


async def suggestions(
    desk: Desk, agent: Agent, q: str = "", with_: tuple[str, ...] = ()
) -> list[tuple[str, str | None, str, str | None]]:
    response = await desk.client.get(
        "/api/v1/products/suggest",
        headers=agent.headers,
        params={"q": q, "with": list(with_)},
    )
    assert response.status_code == 200, response.text
    return [
        (
            f"{i['product']['name']} {i['product']['spec']}".strip(),
            i["field"],
            i["match"],
            i["note"],
        )
        for i in response.json()["items"]
    ]


async def entries(desk: Desk, headers: dict[str, str] | None = None, **params: Any) -> list[Any]:
    response = await desk.client.get(
        f"{FORM_KB}/entries", headers=headers or desk.admin, params={"limit": 100, **params}
    )
    assert response.status_code == 200, response.text
    items: list[Any] = response.json()["items"]
    return items


async def records(desk: Desk, **params: Any) -> list[Any]:
    response = await desk.client.get(
        f"{FORM_KB}/submissions", headers=desk.admin, params={"limit": 100, **params}
    )
    assert response.status_code == 200, response.text
    items: list[Any] = response.json()["items"]
    return items


def aliases(found: list[Any]) -> dict[tuple[str, str], str]:
    return {(e["text"], e["product"]["code"]): e["status"] for e in found if e["kind"] == "alias"}


async def test_aliases_learned_from_entry_are_used_in_suggestions_and_ai(desk: Desk) -> None:
    big = await goods(desk, "WIN-02", "铝合金窗", spec="1.5m×1.8m")
    small = await goods(desk, "WIN-01", "铝合金窗", spec="1.2m×1.5m")
    screen = await goods(desk, "NET-01", "纱窗", unit="扇")
    alice = await desk.agent("alice", roles=["tenant_admin"], online=False)
    bob = await desk.agent("bob", roles=["tenant_admin"], online=False)
    customer_id = await customer(desk)

    # 第一次：输入「大窗」没找到，换成「铝合金窗」后选了第二个候选——记下来，还在观察中。
    trace = {"query": "铝合金窗", "missed": "大窗", "rank": 1}
    first = await new_order(desk, alice.headers, customer_id, [line(big, **trace)])
    await desk.flush()
    assert aliases(await entries(desk)) == {
        ("大窗", "WIN-02"): "observing",
        ("铝合金窗", "WIN-02"): "observing",
    }
    [record] = await records(desk)
    assert (record["form"], record["event"], record["record_no"]) == (
        "order",
        "created",
        first["no"],
    )
    assert {(r["action"], r["kind"]) for r in record["result"]} == {("created", "alias")}

    # 第二次（另一位客服）：同样的说法——两张不同的单据，开始生效。
    await new_order(desk, bob.headers, customer_id, [line(big, **trace)])
    await desk.flush()
    assert aliases(await entries(desk)) == {
        ("大窗", "WIN-02"): "active",
        ("铝合金窗", "WIN-02"): "active",
    }
    latest = (await records(desk))[0]
    assert "叫法：输入「大窗」→ 铝合金窗 1.5m×1.8m（最近 2 次里 2 次选了它，开始生效）" in [
        r["text"] for r in latest["result"]
    ]

    # 开单时：输入「大窗」，学到的商品排在第一个，标"学到的"；输入「铝合金窗」时学到的规格在前。
    assert (await suggestions(desk, alice, "大窗"))[0] == (
        "铝合金窗 1.5m×1.8m",
        "learned",
        "exact",
        None,
    )
    assert [s[0] for s in await suggestions(desk, alice, "铝合金窗")][:2] == [
        "铝合金窗 1.5m×1.8m",
        "铝合金窗 1.2m×1.5m",
    ]

    # 采用了学到的推荐：记一次"用到"。
    await new_order(
        desk,
        alice.headers,
        customer_id,
        [line(big, query="大窗", match="learned", rank=0)],
    )
    await desk.flush()
    [used] = [e for e in await entries(desk, kind="alias") if e["text"] == "大窗"]
    assert (used["hits"], used["evidence"]) == (1, 3)
    assert [r["action"] for r in (await records(desk))[0]["result"]] == ["hit"]

    # 系统已经会了的不学：输入代码（不带横线）、排在第一个。
    await new_order(desk, alice.headers, customer_id, [line(small, query="win01", rank=0)])
    await desk.flush()
    assert ("win01", "WIN-01") not in aliases(await entries(desk))
    assert (await records(desk))[0]["result"] == []

    # 订单里客户的说法"对应到商品库"：两次之后生效。
    for _ in range(2):
        unmatched = await new_order(
            desk, alice.headers, customer_id, [{"raw_text": "小纱窗", "quantity": 1}]
        )
        await call(
            desk, desk.admin, "PATCH", f"/api/v1/orders/{unmatched['id']}",
            version=unmatched["version"], reason="other",
            items=[line(screen, missed="小纱窗", via="map")],
        )  # fmt: skip
    await desk.flush()
    assert aliases(await entries(desk))[("小纱窗", "NET-01")] == "active"
    assert (await suggestions(desk, alice, "小纱窗"))[0][:2] == ("纱窗", "learned")

    # AI 下单时的商品匹配：说法和叫法一致的直接对应，包含叫法的作为候选。
    async with desk.ctx.db.tenant_session(desk.tenant_id) as session:
        exact = await product_service.search(None, session, desk.tenant_id, "大窗")
        assert (exact[0].product.code, exact[0].exact) == ("WIN-02", True)
        within = await product_service.search(None, session, desk.tenant_id, "要两樘大窗")
        assert within[0].product.code == "WIN-02"

    # 依据：哪张单据、谁、当时的内容；变化记录。
    [known] = [e for e in await entries(desk, kind="alias") if e["text"] == "大窗"]
    detail = await call(desk, desk.admin, "GET", f"{FORM_KB}/entries/{known['id']}")
    assert [e["detail"] for e in detail["evidence_items"]][-2:] == [
        "输入「大窗」没找到，换了说法选了 铝合金窗 1.5m×1.8m",
        "输入「大窗」没找到，换了说法选了 铝合金窗 1.5m×1.8m",
    ]
    assert {e["actor_name"] for e in detail["evidence_items"]} >= {"Alice", "Bob"}
    assert [log["action"] for log in detail["log"]][::-1] == [
        "created",
        "strengthened",
        "activated",
    ]


async def test_learned_alias_is_replaced_when_people_pick_another_product(desk: Desk) -> None:
    big = await goods(desk, "WIN-02", "铝合金窗", spec="1.5m×1.8m")
    small = await goods(desk, "WIN-01", "铝合金窗", spec="1.2m×1.5m")
    alice = await desk.agent("alice", roles=["tenant_admin"], online=False)
    customer_id = await customer(desk)

    async def pick(product: dict[str, Any]) -> None:
        await new_order(desk, alice.headers, customer_id, [line(product, query="窗户", rank=1)])
        await desk.flush()

    for _ in range(2):
        await pick(small)
    assert aliases(await entries(desk))[("窗户", "WIN-01")] == "active"
    # 后来大家都选另一个规格：新的占三分之二以上时换成它，原来的回到观察中。
    for _ in range(3):
        await pick(big)
    assert aliases(await entries(desk))[("窗户", "WIN-01")] == "active"
    await pick(big)
    assert aliases(await entries(desk)) == {
        ("窗户", "WIN-01"): "observing",
        ("窗户", "WIN-02"): "active",
    }
    texts = [r["text"] for r in (await records(desk))[0]["result"]]
    assert "叫法：输入「窗户」→ 铝合金窗 1.2m×1.5m（最近 6 次里 2 次选了它，回到观察中）" in texts
    assert (await suggestions(desk, alice, "窗户"))[0][:2] == ("铝合金窗 1.5m×1.8m", "learned")


async def test_usage_learned_from_confirmed_requisitions(desk: Desk) -> None:
    frame = await material(desk, "AL-1", "铝型材")
    screws = await material(desk, "SCR-1", "螺丝", unit="个")
    glue = await material(desk, "GLUE-1", "结构胶", unit="支")
    for item in (frame, screws, glue):
        await adjust(desk, item["id"], "set", 1000)
    window = await goods(desk, "WIN-01", "铝合金窗")
    door = await goods(desk, "DOOR-01", "防盗门")
    await recipe(desk, window["id"], {frame["id"]: 6.5})
    people = (await worker(desk, "wang"), await keeper(desk))
    wang = people[0]
    customer_id = await customer(desk)

    # 铝合金窗：配方只有型材 6.5 米；每次都补领螺丝（每樘 12 个），型材实际每樘 7 米。
    for _ in range(2):
        await past(
            desk, people, customer_id, [(window["id"], 2)], {frame["id"]: 14, screws["id"]: 24}
        )
    await desk.flush()
    usage = {
        (e["product"]["name"], e["related"]["name"]): e for e in await entries(desk, kind="usage")
    }
    assert (usage[("铝合金窗", "螺丝")]["status"], usage[("铝合金窗", "螺丝")]["value"]) == (
        "active",
        12,
    )
    assert usage[("铝合金窗", "螺丝")]["basis"] == "extra"
    # 型材多领了，但还不到 3 张订单：先不提示。
    assert ("铝合金窗", "铝型材") not in usage

    await past(desk, people, customer_id, [(window["id"], 2)], {frame["id"]: 14, screws["id"]: 24})
    await desk.flush()
    [deviation] = [
        e
        for e in await entries(desk, kind="usage", review=True)
        if e["related"]["name"] == "铝型材"
    ]
    assert (deviation["status"], deviation["review"], deviation["basis"]) == (
        "observing",
        "recipe",
        "deviation",
    )
    assert deviation["review_note"] == "最近 3 张订单每樘实际约 7 米，配方是 6.5 米"
    assert deviation["sentence"] == "铝合金窗 每樘用 铝型材 7 米（配方 6.5 米）"

    # 一键领料：配方不自动改（仍按 6.5 米）；常补领的螺丝作为"学到的"一行补上。
    order = await confirmed(desk, customer_id, [(window["id"], 1)])
    await call(desk, wang.headers, "POST", f"/api/v1/production/orders/{order['id']}/claim")
    found = await draft(desk, wang, order["id"])
    assert {
        ln["name"]: (ln["quantity"], [(s["basis"], s["orders"]) for s in ln["sources"]])
        for ln in found["lines"]
    } == {"铝型材": (6.5, [("recipe", None)]), "螺丝": (12, [("learned", 3)])}

    # 维护商品库的员工把学到的用量写进配方：之后按配方 7 米预填。
    updated = await call(
        desk, desk.admin, "POST", f"{FORM_KB}/entries/{deviation['id']}/apply-recipe"
    )
    assert (updated["review"], updated["status"], updated["log"][0]["action"]) == (
        None,
        "observing",
        "recipe",
    )
    assert updated["log"][0]["note"] == "写进配方：每樘 7 米（原来 6.5 米）"
    found = await draft(desk, wang, order["id"])
    assert {ln["name"]: ln["quantity"] for ln in found["lines"]} == {"铝型材": 7, "螺丝": 12}

    # 防盗门没有配方：按以往领料估算；停用的材料不列；知识库里固定的用量优先（标"知识库"）。
    for _ in range(2):
        await past(desk, people, customer_id, [(door["id"], 2)], {frame["id"]: 10, glue["id"]: 2})
    await desk.flush()
    usage = {
        (e["product"]["name"], e["related"]["name"]): e for e in await entries(desk, kind="usage")
    }
    assert usage[("防盗门", "铝型材")]["basis"] == "estimate"
    assert usage[("防盗门", "铝型材")]["status"] == "active"
    await call(
        desk, desk.admin, "POST", f"{FORM_KB}/entries/{usage[('防盗门', '结构胶')]['id']}/disable"
    )
    edited = await call(
        desk, desk.admin, "PUT", f"{FORM_KB}/entries/{usage[('防盗门', '铝型材')]['id']}", value=6
    )
    assert (edited["locked"], edited["value"], edited["log"][0]["action"]) == (True, 6, "edited")
    order = await confirmed(desk, customer_id, [(door["id"], 3)])
    await call(desk, wang.headers, "POST", f"/api/v1/production/orders/{order['id']}/claim")
    found = await draft(desk, wang, order["id"])
    assert [(i["name"], i["basis"]) for i in found["items"]] == [("防盗门", "manual")]
    assert {
        ln["name"]: (ln["quantity"], [s["basis"] for s in ln["sources"]]) for ln in found["lines"]
    } == {"铝型材": (18, ["manual"])}


async def test_companions_and_learning_records(desk: Desk) -> None:
    window = await goods(desk, "WIN-01", "铝合金窗")
    screen = await goods(desk, "NET-01", "纱窗", unit="扇")
    lock = await goods(desk, "LOCK-01", "门锁", unit="把")
    alice = await desk.agent("alice", roles=["tenant_admin"], online=False)
    customer_id = await customer(desk)

    # 每次提交后立即判断（与实时消费进程相同）。
    for lines in (
        [line(window)],
        [line(window), line(screen)],
        [line(window), line(screen)],
        [line(window), line(screen)],
        [line(lock)],
    ):
        await new_order(desk, alice.headers, customer_id, lines)
        await desk.flush()
    pairs = {
        (e["product"]["name"], e["related"]["name"]): (e["status"], e["evidence"])
        for e in await entries(desk, kind="companion")
    }
    assert pairs == {("铝合金窗", "纱窗"): ("active", 3), ("纱窗", "铝合金窗"): ("active", 3)}

    # 录入行空着、单上已经有铝合金窗：先列出常一起开的纱窗，再列最近用过的。
    found = await suggestions(desk, alice, with_=(window["id"],))
    assert found[0] == ("纱窗", None, "companion", "和 铝合金窗 一起开过 3/4 次")
    assert [s[2] for s in found[1:]] == ["recent", "recent"]

    # 采用了推荐：记一次"用到"。
    await new_order(
        desk, alice.headers, customer_id, [line(window), line(screen, match="companion")]
    )
    await desk.flush()
    [pair] = [
        e for e in await entries(desk, kind="companion") if e["product"]["name"] == "铝合金窗"
    ]
    assert pair["hits"] == 1

    # 学习记录：每次提交一条；只看有更新的。
    every = await records(desk)
    assert len(every) == 6
    assert every[-1]["result"] == []
    changed = await records(desk, changed=True)
    assert {r["record_no"] for r in changed} < {r["record_no"] for r in every}
    assert all(r["status"] == "done" for r in every)


async def test_manual_knowledge_review_permissions_and_settings(desk: Desk) -> None:
    big = await goods(desk, "WIN-02", "铝合金窗", spec="1.5m×1.8m")
    small = await goods(desk, "WIN-01", "铝合金窗", spec="1.2m×1.5m")
    alice = await desk.agent("alice", roles=["tenant_admin"], online=False)
    zhou = await desk.agent("zhou", roles=["supervisor"], online=False)
    clerk = await desk.agent("clerk", online=False)
    customer_id = await customer(desk)

    # 客服能查看，不能维护；主管可以手工添加（立即生效、固定）。
    denied = await desk.client.post(
        f"{FORM_KB}/entries",
        headers=clerk.headers,
        json={"kind": "alias", "text": "小窗", "product_id": small["id"]},
    )
    assert denied.status_code == 403
    added = await call(
        desk, zhou.headers, "POST", f"{FORM_KB}/entries", 201,
        kind="alias", text="小窗", product_id=small["id"],
    )  # fmt: skip
    assert (added["status"], added["source"], added["locked"]) == ("active", "manual", True)
    assert added["sentence"] == "输入「小窗」→ 铝合金窗 1.2m×1.5m"
    assert [e["id"] for e in await entries(desk, clerk.headers)] == [added["id"]]
    assert (await suggestions(desk, alice, "小窗"))[0][:2] == ("铝合金窗 1.2m×1.5m", "learned")

    # 后来两次输入「小窗」都选了另一个规格：固定的不自动换，标"待确认"。
    for _ in range(2):
        await new_order(desk, alice.headers, customer_id, [line(big, query="小窗", rank=1)])
    await desk.flush()
    [review] = await entries(desk, review=True)
    assert (review["id"], review["review"]) == (added["id"], "conflict")
    assert review["review_note"] == "最近 2 次输入「小窗」，2 次选了 铝合金窗"
    adopted = await call(
        desk, zhou.headers, "POST", f"{FORM_KB}/entries/{added['id']}/confirm", decision="adopt"
    )
    assert adopted["status"] == "disabled"
    assert aliases(await entries(desk)) == {
        ("小窗", "WIN-01"): "disabled",
        ("小窗", "WIN-02"): "active",
    }

    # 学到的不能删除（只能停用），手工添加的可以。
    learned = [e for e in await entries(desk) if e["source"] == "learned"]
    refused = await desk.client.delete(
        f"{FORM_KB}/entries/{learned[0]['id']}", headers=zhou.headers
    )
    assert refused.status_code == 422
    removed = await desk.client.delete(f"{FORM_KB}/entries/{added['id']}", headers=zhou.headers)
    assert removed.status_code == 204

    # 关掉自动生效：达到条件的标"待确认"，确认后才用上。
    response = await desk.client.put(
        f"{FORM_KB}/settings",
        headers=clerk.headers,
        json={"auto_activate": False, "learn_aliases": True, "learn_usage": True,
              "learn_companions": True},
    )  # fmt: skip
    assert response.status_code == 403
    await call(
        desk, zhou.headers, "PUT", f"{FORM_KB}/settings",
        auto_activate=False, learn_aliases=True, learn_usage=True, learn_companions=True,
    )  # fmt: skip
    for _ in range(2):
        await new_order(desk, alice.headers, customer_id, [line(big, missed="落地窗")])
    await desk.flush()
    [pending] = [e for e in await entries(desk, review=True) if e["text"] == "落地窗"]
    assert (pending["status"], pending["review"]) == ("observing", "activate")
    assert all(found[1] != "learned" for found in await suggestions(desk, alice, "落地窗"))
    summary = await call(desk, zhou.headers, "GET", f"{FORM_KB}/summary")
    assert (summary["review"], summary["pending"]) == (1, 0)
    confirmed_entry = await call(
        desk,
        zhou.headers,
        "POST",
        f"{FORM_KB}/entries/{pending['id']}/confirm",
        decision="activate",
    )
    assert (confirmed_entry["status"], confirmed_entry["log"][0]["action"]) == (
        "active",
        "confirmed",
    )
    assert confirmed_entry["log"][0]["actor_name"] == "Zhou"
    assert (await suggestions(desk, alice, "落地窗"))[0][:2] == ("铝合金窗 1.5m×1.8m", "learned")

    # 关掉学习叫法：之后的输入不再学。
    await call(
        desk, zhou.headers, "PUT", f"{FORM_KB}/settings",
        auto_activate=True, learn_aliases=False, learn_usage=True, learn_companions=True,
    )  # fmt: skip
    await new_order(desk, alice.headers, customer_id, [line(big, missed="大窗户")])
    await desk.flush()
    assert all(e["text"] != "大窗户" for e in await entries(desk, kind="alias"))
