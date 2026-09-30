"""仓库（设计文档 §25.13）：成品和材料、配方、仓管（指定的员工，或最早创建的工人）、领料单和入库单
（仓管确认后才修改库存，可以退回、修改后重新提交、作废），加工时先按配方领料、完成时开入库单，
入库确认后订单进入"待发货"；现货商品直接从成品库存发货；材料不给 AI、不能下单；Excel 的类别、
单位和现货列。"""

import base64
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.xlsx import Column, Sheet, write_workbook
from tests.desk import Agent, Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import call, customer, new_order

BASE = "/api/v1/warehouse"
PRODUCTS = "/api/v1/products"
PRODUCTION = "/api/v1/production"


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def material(
    desk: Desk, code: str, name: str, unit: str = "米", headers: dict[str, str] | None = None
) -> dict[str, Any]:
    body = {"code": code, "name": name, "kind": "material", "unit": unit}
    created: dict[str, Any] = await call(desk, headers or desk.admin, "POST", PRODUCTS, 201, **body)
    return created


async def goods(desk: Desk, code: str, name: str, **fields: Any) -> dict[str, Any]:
    body = {"code": code, "name": name, "retail_price": "100", "unit": "樘", **fields}
    created: dict[str, Any] = await call(desk, desk.admin, "POST", PRODUCTS, 201, **body)
    return created


async def recipe(desk: Desk, product_id: str, lines: dict[str, float]) -> dict[str, Any]:
    body = [{"material_id": m, "quantity": q} for m, q in lines.items()]
    saved: dict[str, Any] = await call(
        desk, desk.admin, "PUT", f"{PRODUCTS}/{product_id}/materials", items=body
    )
    return saved


async def stock_of(desk: Desk, product_id: str) -> tuple[Any, ...]:
    p = await call(desk, desk.admin, "GET", f"{PRODUCTS}/{product_id}")
    return (p["stock"], p["stock_reserved"], p["stock_available"], p["stock_low"])


async def adjust(desk: Desk, product_id: str, mode: str, quantity: float, status: int = 200) -> Any:
    return await call(
        desk, desk.admin, "POST", f"{PRODUCTS}/{product_id}/stock", status,
        mode=mode, quantity=quantity,
    )  # fmt: skip


async def worker(desk: Desk, name: str) -> Agent:
    return await desk.agent(name, roles=["worker"], online=False)


async def permissions(desk: Desk, agent: Agent) -> set[str]:
    me = await call(desk, agent.headers, "GET", "/api/v1/me")
    return set(me["permissions"])


async def confirmed(desk: Desk, customer_id: str, lines: list[tuple[str, int]]) -> dict[str, Any]:
    order = await new_order(
        desk, desk.admin, customer_id, [{"product_id": p, "quantity": q} for p, q in lines]
    )
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{order['id']}/confirm",
               payment_method="cod", notify_customer=False)  # fmt: skip
    return order


async def notifications(desk: Desk, agent: Agent) -> list[tuple[str, str, str]]:
    rows = await desk.sql(
        "SELECT kind, title, link FROM staff_notifications WHERE staff_id = $1 ORDER BY created_at",
        agent.staff_id,
    )
    return [(r["kind"], r["title"], r["link"]) for r in rows]


def sheet(header: list[str], rows: list[list[str]]) -> str:
    data = write_workbook([Sheet("商品", [Column(t) for t in header], rows=rows)])
    return base64.b64encode(data).decode()


async def test_materials_recipes_and_warehouse_keeper(desk: Desk) -> None:
    frame = await material(desk, "AL-6063", "铝合金型材")
    glass = await material(desk, "GL-5", "钢化玻璃", unit="平方米")
    window = await goods(desk, "WIN-01", "铝合金窗")
    # 材料总是管理库存，从 0 开始；成品新建时不管理库存。
    assert (frame["kind"], frame["unit"], frame["stock"]) == ("material", "米", 0)
    assert (window["kind"], window["stock"], window["ready_made"]) == ("goods", None, False)

    # 配方：每一件成品用多少材料（可以是小数）。
    saved = await recipe(desk, window["id"], {frame["id"]: 2.5, glass["id"]: 1.2})
    assert [(i["name"], i["quantity"], i["unit"]) for i in saved["items"]] == [
        ("铝合金型材", 2.5, "米"),
        ("钢化玻璃", 1.2, "平方米"),
    ]
    listed = await call(desk, desk.admin, "GET", f"{PRODUCTS}?kind=goods")
    assert [p["materials"] for p in listed["items"]] == [2]
    for lines, message in (
        ({window["id"]: 1}, "不是材料"),
        ({frame["id"]: 0}, "用量要大于 0"),
        ({frame["id"]: 0.0001}, ""),
    ):
        body = [{"material_id": m, "quantity": q} for m, q in lines.items()]
        bad = await desk.client.put(
            f"{PRODUCTS}/{window['id']}/materials", headers=desk.admin, json={"items": body}
        )
        assert bad.status_code == 422 and message in bad.text
    not_goods = await desk.client.put(
        f"{PRODUCTS}/{frame['id']}/materials", headers=desk.admin, json={"items": []}
    )
    assert not_goods.status_code == 422 and "只有成品有配方" in not_goods.text
    used = await desk.client.delete(f"{PRODUCTS}/{frame['id']}", headers=desk.admin)
    assert used.status_code == 409 and "配方用到这个材料" in used.text

    # 材料不在商品列表和检索里，也不能下单。
    assert [p["code"] for p in listed["items"]] == ["WIN-01"]
    materials = await call(desk, desk.admin, "GET", f"{PRODUCTS}?kind=material")
    assert {p["code"] for p in materials["items"]} == {"AL-6063", "GL-5"}
    found = await call(desk, desk.admin, "GET", f"{PRODUCTS}/search?q=铝合金")
    assert [c["product"]["code"] for c in found["items"]] == ["WIN-01"]
    found = await call(desk, desk.admin, "GET", f"{PRODUCTS}/search?q=铝合金&kind=material")
    assert [c["product"]["code"] for c in found["items"]] == ["AL-6063"]
    customer_id = await customer(desk)
    refused = await desk.client.post(
        "/api/v1/orders",
        headers=desk.admin,
        json={"customer_id": customer_id, "items": [{"product_id": frame["id"], "quantity": 1}]},
    )
    assert refused.status_code == 422 and "生产用的材料，不能下单" in refused.text

    # 库存：材料最多三位小数，成品只能是整数；材料不能"不再管理库存"。
    added = await adjust(desk, frame["id"], "add", 10.5)
    assert added["stock"] == 10.5
    whole = await adjust(desk, window["id"], "add", 1.5, status=422)
    assert "只能是整数" in whole["error"]["message"]
    untrack = await adjust(desk, frame["id"], "untrack", 0, status=422)
    assert "材料总是管理库存" in untrack["error"]["message"]
    [row] = await desk.sql("SELECT stock FROM products WHERE code = 'AL-6063'")
    assert str(row["stock"]) == "10.500"

    # 主管（调整库存）可以维护材料，不能维护成品。
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    await material(desk, "SCREW-4", "不锈钢螺丝", unit="个", headers=boss.headers)
    denied = await desk.client.post(
        PRODUCTS, headers=boss.headers, json={"name": "新成品", "kind": "goods"}
    )
    assert denied.status_code == 403
    denied = await desk.client.put(
        f"{PRODUCTS}/{window['id']}", headers=boss.headers, json={"name": "改名"}
    )
    assert denied.status_code == 403

    # 仓管：没有指定时由最早创建、启用状态的工人担任（另外获得确认单据、调整库存的权限）。
    setting = await call(desk, desk.admin, "GET", f"{BASE}/settings")
    assert (setting["confirm_required"], setting["effective_keeper_id"]) == (True, None)
    wang = await worker(desk, "wang")
    li = await worker(desk, "lisi")
    assert await permissions(desk, wang) == {
        "production:work",
        "inventory:manage",
        "warehouse:confirm",
    }
    assert await permissions(desk, li) == {"production:work"}
    setting = await call(desk, li.headers, "GET", f"{BASE}/settings")
    assert (setting["effective_keeper_name"], setting["fallback"], setting["can_edit"]) == (
        "Wang",
        True,
        False,
    )
    # 管理员指定仓管；停用后又由最早创建的工人担任。
    assert (
        await desk.client.put(
            f"{BASE}/settings", headers=li.headers, json={"keeper_id": str(boss.staff_id)}
        )
    ).status_code == 403
    setting = await call(desk, desk.admin, "PUT", f"{BASE}/settings", keeper_id=str(boss.staff_id))
    assert (setting["keeper_name"], setting["effective_keeper_name"], setting["fallback"]) == (
        "Boss",
        "Boss",
        False,
    )
    assert "warehouse:confirm" in await permissions(desk, boss)
    assert await permissions(desk, wang) == {"production:work"}
    disabled = await desk.client.patch(
        f"/api/v1/staff/{boss.staff_id}", headers=desk.admin, json={"status": "disabled"}
    )
    assert disabled.status_code == 200, disabled.text
    setting = await call(desk, desk.admin, "GET", f"{BASE}/settings")
    assert (setting["keeper_name"], setting["effective_keeper_name"]) == ("Boss", "Wang")
    assert "warehouse:confirm" in await permissions(desk, wang)
    inactive = await desk.client.put(
        f"{BASE}/settings", headers=desk.admin, json={"keeper_id": str(boss.staff_id)}
    )
    assert inactive.status_code == 422
    [audit] = await desk.sql("SELECT detail FROM audit_logs WHERE action = 'warehouse.settings'")
    assert str(boss.staff_id) in audit["detail"]

    # 仓库里的库存（没有价格）：材料的"占用"是待确认的领料单。
    page = await call(desk, wang.headers, "GET", f"{BASE}/items?kind=material")
    by_code = {i["code"]: i for i in page["items"]}
    assert (by_code["AL-6063"]["stock"], by_code["GL-5"]["stock_low"]) == (10.5, True)
    assert "retail_price" not in by_code["AL-6063"]
    assert page["low_stock"] == 2  # 玻璃和螺丝都是 0


async def test_production_requisition_then_receipt_then_ship(desk: Desk) -> None:
    frame = await material(desk, "AL-6063", "铝合金型材")
    glass = await material(desk, "GL-5", "钢化玻璃", unit="平方米")
    await adjust(desk, frame["id"], "set", 10)
    await adjust(desk, glass["id"], "set", 1)
    window = await goods(desk, "WIN-01", "铝合金窗")
    screen = await goods(desk, "SCR-01", "纱窗", unit="扇")
    await recipe(desk, window["id"], {frame["id"]: 2.5, glass["id"]: 1.2})
    cang = await desk.agent("cang", online=False)
    wang = await worker(desk, "wang")
    li = await worker(desk, "lisi")
    await call(desk, desk.admin, "PUT", f"{BASE}/settings", keeper_id=str(cang.staff_id))
    assert "warehouse:confirm" in await permissions(desk, cang)
    assert "warehouse:confirm" not in await permissions(desk, wang)

    customer_id = await customer(desk)
    order = await confirmed(desk, customer_id, [(window["id"], 2), (screen["id"], 1)])
    url = f"{PRODUCTION}/orders/{order['id']}"

    # 待领取：还没领料，按配方算材料不够的只提示。
    pool = await call(desk, wang.headers, "GET", f"{PRODUCTION}/orders?view=pool")
    [card] = pool["items"]
    assert (card["material_short"], card["requisition_required"]) == (["钢化玻璃"], True)
    claimed = await call(desk, wang.headers, "POST", f"{url}/claim")
    assert (claimed["requisition_ready"], claimed["needs_receipt"]) == (False, True)
    win_item = claimed["items"][0]
    blocked = await desk.client.post(f"{url}/items/{win_item['id']}/done", headers=wang.headers)
    assert blocked.status_code == 422 and "请先开领料单" in blocked.text

    # 领料单：按配方预填（订单数量 × 用量），没有配方的商品另外列出。
    draft = await call(
        desk, wang.headers, "GET", f"{BASE}/drafts?kind=requisition&order_id={order['id']}"
    )
    assert [(line["name"], line["quantity"], line["stock"]) for line in draft["lines"]] == [
        ("钢化玻璃", 2.4, 1),
        ("铝合金型材", 5, 10),
    ]
    assert draft["missing"] == ["纱窗"]
    requisition = await call(
        desk, wang.headers, "POST", f"{BASE}/documents", 201,
        kind="requisition", order_id=order["id"], note="第一批",
        lines=[
            {"product_id": line["product_id"], "quantity": line["quantity"],
             "planned": line["planned"]}
            for line in draft["lines"]
        ],
    )  # fmt: skip
    assert (requisition["status"], requisition["no"][:2], requisition["short"]) == (
        "pending",
        "LL",
        ["钢化玻璃"],  # 只提示，可以领
    )
    assert (requisition["can_confirm"], requisition["can_edit"]) == (False, True)
    assert [n[0] for n in await notifications(desk, cang)] == ["warehouse_pending"]
    # 待确认的领料单占用材料（可用 = 现有 − 待领）。
    assert await stock_of(desk, glass["id"]) == (1, 2.4, -1.4, True)
    # 工人不能确认；别的工人看不到这张单。
    denied = await desk.client.post(
        f"{BASE}/documents/{requisition['id']}/confirm", headers=wang.headers, json={}
    )
    assert denied.status_code == 403
    hidden = await desk.client.get(f"{BASE}/documents/{requisition['id']}", headers=li.headers)
    assert hidden.status_code == 404
    again = await call(
        desk, wang.headers, "GET", f"{BASE}/drafts?kind=requisition&order_id={order['id']}"
    )
    assert again["lines"] == []  # 已经领过的不再预填

    # 开了领料单就可以加工；完成加工时开入库单，仓管确认前订单还在加工中。
    await call(desk, wang.headers, "POST", f"{url}/items/{win_item['id']}/done")
    completed = await call(desk, wang.headers, "POST", f"{url}/complete", mark_all=True)
    assert completed["processed_at"] is None and completed["receipt"]["status"] == "pending"
    assert completed["receipt"]["no"].startswith("RK")
    assert [i["work_status"] for i in completed["items"]] == ["done", "done"]
    frozen = await desk.client.post(f"{url}/items/{win_item['id']}/undo", headers=wang.headers)
    assert frozen.status_code == 422 and "已经开了入库单" in frozen.text
    center = await call(desk, desk.admin, "GET", "/api/v1/orders?view=awaiting_shipment")
    assert center["items"] == []

    # 仓管：待确认的单据；按实际数量确认领料（玻璃不够也可以领，库存变成负数）。
    listed = await call(desk, cang.headers, "GET", f"{BASE}/documents?status=pending")
    assert {d["kind"] for d in listed["items"]} == {"requisition", "receipt"}
    counts = await call(desk, cang.headers, "GET", f"{BASE}/counts")
    assert (counts["pending_requisitions"], counts["pending_receipts"]) == (1, 1)
    lines = {line["name"]: line["id"] for line in requisition["lines"]}
    done = await call(
        desk, cang.headers, "POST", f"{BASE}/documents/{requisition['id']}/confirm",
        lines=[{"id": lines["钢化玻璃"], "quantity": 2.5}],
    )  # fmt: skip
    assert (done["status"], done["confirmed_by_name"]) == ("confirmed", "Cang")
    assert [(ln["name"], ln["stock_before"], ln["stock_after"]) for ln in done["lines"]] == [
        ("钢化玻璃", 1, -1.5),
        ("铝合金型材", 10, 5),
    ]
    assert await stock_of(desk, glass["id"]) == (-1.5, 0, -1.5, True)
    history = await call(desk, desk.admin, "GET", f"{PRODUCTS}/{frame['id']}/stock-movements")
    first = history["items"][0]
    assert (first["kind"], first["kind_label"], first["delta"], first["document_no"]) == (
        "requisition",
        "领料",
        -5,
        requisition["no"],
    )
    assert first["order_no"] == order["no"]
    assert ("warehouse_document", f"领料单 {requisition['no']} 仓管已确认") in [
        n[:2] for n in await notifications(desk, wang)
    ]

    # 退回入库单：写明原因，工人修改后重新提交。
    receipt_id = completed["receipt"]["id"]
    no_reason = await desk.client.post(
        f"{BASE}/documents/{receipt_id}/reject", headers=cang.headers, json={"reason": " "}
    )
    assert no_reason.status_code == 422
    await call(desk, cang.headers, "POST", f"{BASE}/documents/{receipt_id}/reject",
               reason="纱窗还没做好")  # fmt: skip
    view = await call(desk, wang.headers, "GET", url)
    assert (view["receipt"]["status"], view["receipt"]["reject_reason"]) == (
        "rejected",
        "纱窗还没做好",
    )
    resubmitted = await call(
        desk, wang.headers, "PUT", f"{BASE}/documents/{receipt_id}",
        lines=[{"product_id": window["id"], "quantity": 2}, {"product_id": screen["id"],
                                                             "quantity": 1}],
    )  # fmt: skip
    assert resubmitted["status"] == "pending" and resubmitted["reject_reason"] is None

    # 仓管确认入库：成品库存增加，订单加工完成、进入"待发货"，客服收到发货提醒。
    await call(desk, cang.headers, "POST", f"{BASE}/documents/{receipt_id}/confirm")
    assert await stock_of(desk, window["id"]) == (2, 2, 0, False)
    detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{order['id']}")
    assert detail["processed_by_name"] == "Wang" and detail["processed_at"] is not None
    assert detail["production_required"] is True
    assert [(d["kind"], d["status"]) for d in detail["documents"]] == [
        ("requisition", "confirmed"),
        ("receipt", "confirmed"),
    ]
    assert [i["stock_available"] for i in detail["items"]] == [0, 0]
    center = await call(desk, desk.admin, "GET", "/api/v1/orders?view=awaiting_shipment")
    assert [o["no"] for o in center["items"]] == [order["no"]]
    todos = await desk.sql(
        "SELECT t.title FROM todos t WHERE t.order_id = $1 AND t.title LIKE '%加工完成%'",
        uuid.UUID(order["id"]),
    )
    assert len(todos) == 1
    events = [e["type"] for e in detail["events"]]
    assert {"requisition", "receipt", "document_confirmed", "document_rejected"} <= set(events)

    # 发货时成品出库。
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{order['id']}/ship",
               shipping_company="顺丰", tracking_no="SF1", notify_customer=False)  # fmt: skip
    assert await stock_of(desk, window["id"]) == (0, 0, 0, False)
    assert await stock_of(desk, screen["id"]) == (0, 0, 0, False)

    # 全部库存记录：按类别筛选，关联单据和订单。
    log = await call(desk, desk.admin, "GET", f"{BASE}/movements?kind=goods")
    assert [(m["product_name"], m["kind"]) for m in log["items"]][:2] == [
        ("纱窗", "order_out"),
        ("铝合金窗", "order_out"),
    ]
    receipts = await call(desk, desk.admin, "GET", f"{BASE}/movements?type=receipt")
    assert {m["document_no"] for m in receipts["items"]} == {resubmitted["no"]}


async def test_ready_made_goods_ship_from_stock_and_keepers_confirm_their_own(desk: Desk) -> None:
    lamp = await goods(desk, "LAMP-01", "吸顶灯", ready_made=True, unit="盏")
    window = await goods(desk, "WIN-01", "铝合金窗")
    frame = await material(desk, "AL-6063", "铝合金型材")
    await recipe(desk, window["id"], {frame["id"]: 2})
    await adjust(desk, lamp["id"], "set", 1)
    wang = await worker(desk, "wang")  # 没有指定仓管：最早创建的工人担任
    li = await worker(desk, "lisi")
    customer_id = await customer(desk)

    # 全是现货的订单不进加工页：确认后占用库存，不够时提示（只提示）；开始处理后就是"待发货"。
    only = await confirmed(desk, customer_id, [(lamp["id"], 2)])
    pool = await call(desk, wang.headers, "GET", f"{PRODUCTION}/orders?view=pool")
    assert pool["items"] == []
    detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{only['id']}")
    assert detail["production_required"] is False
    assert [(i["ready_made"], i["stock_available"], i["stock_short"]) for i in detail["items"]] == [
        (True, -1, True)
    ]
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{only['id']}/start")
    center = await call(desk, desk.admin, "GET", "/api/v1/orders?view=awaiting_shipment")
    assert [o["no"] for o in center["items"]] == [only["no"]]
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{only['id']}/ship",
               shipping_company="顺丰", tracking_no="SF2", notify_customer=False)  # fmt: skip
    assert await stock_of(desk, lamp["id"]) == (-1, 0, -1, True)

    # 现货和需要加工的商品一起下单：只加工需要加工的；仓管（最早的工人）开的单直接确认。
    mixed = await confirmed(desk, customer_id, [(lamp["id"], 1), (window["id"], 1)])
    url = f"{PRODUCTION}/orders/{mixed['id']}"
    card = await call(desk, wang.headers, "POST", f"{url}/claim")
    assert [i["ready_made"] for i in card["items"]] == [True, False]
    requisition = await call(
        desk, wang.headers, "POST", f"{BASE}/documents", 201,
        kind="requisition", order_id=mixed["id"],
        lines=[{"product_id": frame["id"], "quantity": 2}],
    )  # fmt: skip
    assert requisition["status"] == "confirmed"
    assert await stock_of(desk, frame["id"]) == (-2, 0, -2, True)
    done = await call(desk, wang.headers, "POST", f"{url}/complete", mark_all=True)
    assert done["processed_at"] is not None and done["receipt"] is None
    assert [i["work_status"] for i in done["items"]] == ["done", "done"]
    assert await stock_of(desk, window["id"]) == (1, 1, 0, False)

    # 设置为不需要确认：开单即生效。
    await call(desk, desk.admin, "PUT", f"{BASE}/settings", confirm_required=False)
    third = await confirmed(desk, customer_id, [(window["id"], 1)])
    await call(desk, li.headers, "POST", f"{PRODUCTION}/orders/{third['id']}/claim")
    quick = await call(
        desk, li.headers, "POST", f"{BASE}/documents", 201,
        kind="requisition", order_id=third["id"],
        lines=[{"product_id": frame["id"], "quantity": 2}],
    )  # fmt: skip
    assert (quick["status"], quick["confirmed_by_name"]) == ("confirmed", "Lisi")

    # 订单取消时，它还没生效的单据随之作废。
    await call(desk, desk.admin, "PUT", f"{BASE}/settings", confirm_required=True)
    pending = await call(
        desk, li.headers, "POST", f"{BASE}/documents", 201,
        kind="requisition", order_id=third["id"],
        lines=[{"product_id": frame["id"], "quantity": 0.5}],
    )  # fmt: skip
    assert pending["status"] == "pending"
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{third['id']}/cancel",
               reason="客户不要了", notify_customer=False)  # fmt: skip
    voided = await call(desk, desk.admin, "GET", f"{BASE}/documents/{pending['id']}")
    assert (voided["status"], voided["void_reason"]) == ("voided", "订单已取消")


async def test_warehouse_documents_without_orders(desk: Desk) -> None:
    lamp = await goods(desk, "LAMP-01", "吸顶灯", ready_made=True)
    frame = await material(desk, "AL-6063", "铝合金型材")
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    wang = await worker(desk, "wang")
    admin = await call(desk, desk.admin, "GET", "/api/v1/me")
    await call(desk, desk.admin, "PUT", f"{BASE}/settings", keeper_id=admin["id"])

    # 备货生产：仓库直接开单（不关联订单）；仓管（管理员）开的单直接确认。
    receipt = await call(
        desk, desk.admin, "POST", f"{BASE}/documents", 201,
        kind="receipt", note="备货", lines=[{"product_id": lamp["id"], "quantity": 20}],
    )  # fmt: skip
    assert (receipt["status"], receipt["order_id"]) == ("confirmed", None)
    assert (await stock_of(desk, lamp["id"]))[0] == 20

    # 主管能开单、不能确认；工人不能开不关联订单的单。
    requisition = await call(
        desk, boss.headers, "POST", f"{BASE}/documents", 201,
        kind="requisition", lines=[{"product_id": frame["id"], "quantity": 1.25}],
    )  # fmt: skip
    assert requisition["status"] == "pending"
    denied = await desk.client.post(
        f"{BASE}/documents/{requisition['id']}/confirm", headers=boss.headers, json={}
    )
    assert denied.status_code == 403
    refused = await desk.client.post(
        f"{BASE}/documents",
        headers=wang.headers,
        json={"kind": "requisition", "lines": [{"product_id": frame["id"], "quantity": 1}]},
    )
    assert refused.status_code == 403
    for kind, product_id, message in (
        ("requisition", lamp["id"], "领料单只能领材料"),
        ("receipt", frame["id"], "入库单只能入成品"),
        ("receipt", lamp["id"], "只能是整数"),
    ):
        bad = await desk.client.post(
            f"{BASE}/documents",
            headers=desk.admin,
            json={"kind": kind, "lines": [{"product_id": product_id, "quantity": 1.5}]},
        )
        assert bad.status_code == 422 and message in bad.text, bad.text

    # 作废还没生效的单据；已确认的不能作废，也不能修改。
    voided = await call(desk, boss.headers, "POST", f"{BASE}/documents/{requisition['id']}/void",
                        reason="开错了")  # fmt: skip
    assert (voided["status"], voided["voided_by_name"]) == ("voided", "Boss")
    locked = await desk.client.post(
        f"{BASE}/documents/{receipt['id']}/void", headers=desk.admin, json={}
    )
    assert locked.status_code == 409
    listed = await call(desk, boss.headers, "GET", f"{BASE}/documents?kind=receipt")
    assert [d["no"] for d in listed["items"]] == [receipt["no"]]
    mine = await call(desk, boss.headers, "GET", f"{BASE}/documents?mine=true")
    assert [d["no"] for d in mine["items"]] == [requisition["no"]]
    # 工人只看得到自己的单据和自己加工的订单的单据。
    assert (await call(desk, wang.headers, "GET", f"{BASE}/documents"))["items"] == []
    log = await call(desk, desk.admin, "GET", f"{BASE}/movements?q=吸顶灯")
    assert [(m["kind"], m["delta"], m["document_no"]) for m in log["items"]] == [
        ("receipt", 20, receipt["no"])
    ]


async def test_excel_categories_units_and_ready_made(desk: Desk) -> None:
    await goods(desk, "WIN-01", "铝合金窗")
    header = ["名称*", "代码", "类别", "单位", "现货", "库存"]
    content = sheet(
        header,
        [
            ["吸顶灯", "LAMP-01", "成品", "盏", "是", "12"],
            ["铝合金方管", "AL-6061", "材料", "米", "", "120.5"],
            ["钢化玻璃", "GL-5", "", "平方米", "", ""],
            ["纱窗", "SCR-01", "成品", "扇", "否", "1.5"],
            ["铝合金窗", "WIN-01", "材料", "", "", ""],
            ["坏类别", "BAD-01", "半成品", "", "也许", ""],
        ],
    )
    preview = await call(
        desk, desk.admin, "POST", f"{PRODUCTS}/imports", 201,
        filename="仓库.xlsx", content_base64=content, stock_mode="set", default_kind="material",
    )  # fmt: skip
    rows = {r["row"]: r for r in preview["rows"]}
    assert [rows[n]["action"] for n in (2, 3, 4)] == ["create", "create", "create"]
    assert rows[3]["stock_after"] == 120.5
    assert rows[5]["problems"] == ["成品的库存只能填整数"]
    assert rows[6]["problems"] == ["类别不能修改（商品库里这是成品）"]
    assert rows[7]["problems"] == ["类别只能填成品或材料", "现货只能填是或否"]
    await call(desk, desk.admin, "POST", f"{PRODUCTS}/imports/{preview['id']}/confirm")

    lamp, *_ = (await call(desk, desk.admin, "GET", f"{PRODUCTS}?q=吸顶灯"))["items"]
    assert (lamp["kind"], lamp["unit"], lamp["ready_made"], lamp["stock"]) == (
        "goods",
        "盏",
        True,
        12,
    )
    materials = {
        p["code"]: p
        for p in (await call(desk, desk.admin, "GET", f"{PRODUCTS}?kind=material"))["items"]
    }
    # 类别留空的按上传时的选择（材料）；材料没有填库存时从 0 开始。
    assert (materials["AL-6061"]["stock"], materials["GL-5"]["stock"]) == (120.5, 0)
    assert materials["GL-5"]["unit"] == "平方米"

    # 导出：类别、单位、现货列；材料的库存保留小数。
    exported = await desk.client.get(f"{PRODUCTS}/export", headers=desk.admin)
    from app.modules.kb.parsers import parse_sheet

    table = parse_sheet("products.xlsx", exported.content)
    head = table[0]
    by_code = {r[head.index("代码")]: r for r in table[1:]}
    assert by_code["AL-6061"][head.index("类别")] == "材料"
    assert by_code["AL-6061"][head.index("库存")] == "120.5"
    assert by_code["LAMP-01"][head.index("现货")] == "是"

    # 主管（调整库存）导入：可以新增材料；成品只能导入已有商品的库存。
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    content = sheet(
        ["名称*", "代码", "类别", "库存"],
        [
            ["不锈钢螺丝", "SCREW-4", "材料", "500"],
            ["新成品", "NEW-01", "成品", "3"],
            ["吸顶灯", "LAMP-01", "", "15"],
        ],
    )
    preview = await call(
        desk, boss.headers, "POST", f"{PRODUCTS}/imports", 201,
        filename="仓库.xlsx", content_base64=content, stock_mode="set",
    )  # fmt: skip
    rows = {r["row"]: r for r in preview["rows"]}
    assert rows[2]["action"] == "create"
    assert rows[3]["problems"] == ["商品库里没有这个商品（只能导入已有商品的库存）"]
    assert (rows[4]["action"], rows[4]["stock_after"]) == ("update", 15)


async def test_order_edits_void_pending_receipts_and_drafts_skip_received_goods(desk: Desk) -> None:
    window = await goods(desk, "WIN-01", "铝合金窗")
    cang = await desk.agent("cang", online=False)
    wang = await worker(desk, "wang")
    await call(desk, desk.admin, "PUT", f"{BASE}/settings", keeper_id=str(cang.staff_id))
    customer_id = await customer(desk)
    order = await confirmed(desk, customer_id, [(window["id"], 1)])
    url = f"{PRODUCTION}/orders/{order['id']}"

    async def edit(quantity: int) -> dict[str, Any]:
        detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{order['id']}")
        result: dict[str, Any] = await call(
            desk, desk.admin, "PATCH", f"/api/v1/orders/{order['id']}",
            version=detail["version"], reason="customer_request",
            items=[{"product_id": window["id"], "quantity": quantity}],
        )  # fmt: skip
        return result

    # 入库单等仓管确认时客户加了数量：入库单作废，订单回到加工中。
    await call(desk, wang.headers, "POST", f"{url}/claim")
    pending = await call(desk, wang.headers, "POST", f"{url}/complete", mark_all=True)
    receipt_id = pending["receipt"]["id"]
    edited = await edit(2)
    assert [i["work_status"] for i in edited["order"]["items"]] == ["pending"]
    view = await call(desk, wang.headers, "GET", url)
    assert (view["receipt"], view["documents"]) == (None, [])
    voided = await call(desk, cang.headers, "GET", f"{BASE}/documents/{receipt_id}")
    assert (voided["status"], voided["void_reason"]) == ("voided", "订单修改后需要重新加工")

    # 重新完成、仓管确认入库：成品库存 2，订单加工完成。
    again = await call(desk, wang.headers, "POST", f"{url}/complete", mark_all=True)
    assert [line["quantity"] for line in (
        await call(desk, cang.headers, "GET", f"{BASE}/documents/{again['receipt']['id']}")
    )["lines"]] == [2]  # fmt: skip
    await call(desk, cang.headers, "POST", f"{BASE}/documents/{again['receipt']['id']}/confirm")
    assert (await stock_of(desk, window["id"]))[:2] == (2, 2)

    # 加工完成后又加了 1 樘：订单回到加工中，再开入库单时只预填还没入库的 1 樘。
    more = await edit(3)
    assert more["order"]["processed_at"] is None
    draft = await call(
        desk, wang.headers, "GET", f"{BASE}/drafts?kind=receipt&order_id={order['id']}"
    )
    assert [(line["name"], line["quantity"]) for line in draft["lines"]] == [("铝合金窗", 1)]
