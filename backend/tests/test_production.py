"""加工（设计文档 §25.11）：工人领取订单、逐个商品标记完成或缺货、完成订单后进入"待发货"，
客服在待办里收到发货和缺货提醒；主管指派加工人；修改订单时保留加工进度。"""

import json
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.integration.outbox import ORDER_EVENTS
from tests.desk import Agent, Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import call, catalog, customer, new_order

BASE = "/api/v1/production"


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def confirmed_order(
    desk: Desk, products: dict[str, str], customer_id: str, method: str = "cod", **extra: Any
) -> dict[str, Any]:
    lines = [
        {"product_id": products["LOCK-X1"], "quantity": 2},
        {"product_id": products["BELL-D1"], "quantity": 1},
    ]
    order = await new_order(desk, desk.admin, customer_id, lines)
    await call(
        desk,
        desk.admin,
        "POST",
        f"/api/v1/orders/{order['id']}/confirm",
        payment_method=method,
        notify_customer=False,
        **extra,
    )
    return order


async def worker(desk: Desk, name: str) -> Agent:
    return await desk.agent(name, roles=["worker"], online=False)


async def todos_of(desk: Desk, order_id: str) -> list[dict[str, Any]]:
    rows = await desk.sql(
        "SELECT t.title, t.detail, t.status, t.assignee_id, y.code FROM todos t"
        " JOIN todo_types y ON y.id = t.type_id WHERE t.order_id = $1 ORDER BY t.created_at",
        uuid.UUID(order_id),
    )
    return [dict(r) for r in rows]


async def test_worker_claims_marks_items_and_completes_into_awaiting_shipment(desk: Desk) -> None:
    products = await catalog(desk)
    customer_id = await customer(desk)
    order = await confirmed_order(desk, products, customer_id)
    unpaid = await confirmed_order(desk, products, customer_id, method="online")
    wang = await worker(desk, "wang")
    li = await worker(desk, "lisi")

    # 工人只有加工页：进不了订单中心。没有指定仓管时最早创建的工人担任仓管（§25.13）。
    assert (await desk.client.get("/api/v1/orders", headers=wang.headers)).status_code == 403
    me = (await desk.client.get("/api/v1/me", headers=wang.headers)).json()
    # 工人另有个人待办和 AI 助理（§27）。
    worker_only = {"production:work", "task:use", "assistant:use"}
    assert set(me["permissions"]) == worker_only | {"inventory:manage", "warehouse:confirm"}
    me = (await desk.client.get("/api/v1/me", headers=li.headers)).json()
    assert set(me["permissions"]) == worker_only

    # 待领取：货到付款的已确认订单可以开工；在线收款还没收清的不在里面。
    pool = await call(desk, wang.headers, "GET", f"{BASE}/orders?view=pool")
    assert [o["id"] for o in pool["items"]] == [order["id"]]
    card = pool["items"][0]
    assert card["can_claim"] is True and card["customer_name"] == "李女士"
    # 工人看不到金额和收货信息。
    assert not {"total", "receiver", "unit_price", "amount"} & set(card)
    assert not {"unit_price", "amount", "cost_price", "list_price"} & set(card["items"][0])
    early = await desk.client.post(f"{BASE}/orders/{unpaid['id']}/claim", headers=wang.headers)
    assert early.status_code == 422 and "收清全款" in early.text

    # 领取：已确认的订单同时开始处理；别人不能再领。
    claimed = await call(desk, wang.headers, "POST", f"{BASE}/orders/{order['id']}/claim")
    assert (claimed["status"], claimed["worker_name"], claimed["can_work"]) == (
        "fulfilling",
        "Wang",
        True,
    )
    taken = await desk.client.post(f"{BASE}/orders/{order['id']}/claim", headers=li.headers)
    assert taken.status_code == 409 and "Wang" in taken.text
    li_view = await desk.client.post(
        f"{BASE}/orders/{order['id']}/items/{claimed['items'][0]['id']}/done", headers=li.headers
    )
    assert li_view.status_code == 404
    counts = await call(desk, wang.headers, "GET", f"{BASE}/counts")
    assert (counts["pool"], counts["mine"]) == (0, 1)

    # 逐个商品标记完成；还有没标记的商品时不能直接完成，除非一并标记。
    lock, bell = claimed["items"]
    marked = await call(
        desk, wang.headers, "POST", f"{BASE}/orders/{order['id']}/items/{lock['id']}/done"
    )
    assert [i["work_status"] for i in marked["items"]] == ["done", "pending"]
    assert marked["items"][0]["done_by_name"] == "Wang"
    undone = await call(
        desk, wang.headers, "POST", f"{BASE}/orders/{order['id']}/items/{lock['id']}/undo"
    )
    assert undone["done_count"] == 0
    await call(desk, wang.headers, "POST", f"{BASE}/orders/{order['id']}/items/{lock['id']}/done")
    blocked = await desk.client.post(
        f"{BASE}/orders/{order['id']}/complete", headers=wang.headers, json={}
    )
    assert blocked.status_code == 422 and "还有 1 个商品" in blocked.text
    done = await call(
        desk, wang.headers, "POST", f"{BASE}/orders/{order['id']}/complete", mark_all=True
    )
    assert done["processed_at"] is not None and done["can_work"] is False
    assert [i["work_status"] for i in done["items"]] == ["done", "done"]
    assert bell["id"] == done["items"][1]["id"]
    finished = await call(desk, wang.headers, "GET", f"{BASE}/orders?view=done")
    assert [o["no"] for o in finished["items"]] == [order["no"]]

    # 订单中心：进入"待发货"，客服的待办里有发货提醒（交给订单的处理人）。
    center = await call(desk, desk.admin, "GET", "/api/v1/orders?view=awaiting_shipment")
    assert [o["no"] for o in center["items"]] == [order["no"]]
    assert center["items"][0]["worker_name"] == "Wang"
    counted = await call(desk, desk.admin, "GET", "/api/v1/orders/counts")
    assert (counted["awaiting_shipment"], counted["out_of_stock"]) == (1, 0)
    detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{order['id']}")
    assert detail["processed_by_name"] == "Wang"
    assert [i["work_status"] for i in detail["items"]] == ["done", "done"]
    [review, ship] = await todos_of(desk, order["id"])
    assert (ship["code"], ship["status"]) == ("order_ship", "open")
    assert ship["title"] == f"订单 {order['no']} 已加工完成，请发货"
    assert ship["assignee_id"] == (
        uuid.UUID(detail["assignee_id"]) if detail["assignee_id"] else None
    )
    assert "processed" in [e["type"] for e in detail["events"] if e["public"]]
    # 客户的跟踪页：加工完成、等待发货；不显示加工人。
    token = detail["tracking_url"].rsplit("?track=", 1)[1]
    page = (await desk.client.get(f"/api/v1/public/orders/{token}")).json()
    assert "已加工完成，等待发货" in [e["text"] for e in page["events"]]
    assert "Wang" not in json.dumps(page)

    # 客服发货：离开"待发货"，发货提醒随之完成。
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{order['id']}/ship",
               shipping_company="顺丰", tracking_no="SF100", notify_customer=False)  # fmt: skip
    assert (await call(desk, desk.admin, "GET", "/api/v1/orders/counts"))["awaiting_shipment"] == 0
    assert [t["status"] for t in await todos_of(desk, order["id"])] == [review["status"], "done"]

    # 推送：加工完成作为订单更新推送给企业系统。
    assert ORDER_EVENTS["processed"].value == "order.updated"
    [pushed] = await desk.sql(
        "SELECT event, data->>'status' AS status FROM webhook_events"
        " WHERE resource_id = $1 AND data->>'change' = 'processed'",
        uuid.UUID(order["id"]),
    )
    assert (pushed["event"], pushed["status"]) == ("order.updated", "fulfilling")


async def test_shortage_goes_to_its_own_view_and_staff_resolve_it(desk: Desk) -> None:
    products = await catalog(desk)
    customer_id = await customer(desk)
    order = await confirmed_order(desk, products, customer_id)
    wang = await worker(desk, "wang")
    claimed = await call(desk, wang.headers, "POST", f"{BASE}/orders/{order['id']}/claim")
    lock, bell = claimed["items"]
    path = f"{BASE}/orders/{order['id']}/items/{bell['id']}"

    # 登记缺货（缺多少、说明、预计到货日期）：订单进入"缺货"，客服收到"缺货处理"待办。
    short = await call(
        desk, wang.headers, "PUT", f"{path}/shortage", note="供应商断货", restock_date="2026-10-08"
    )
    assert short["shortage"] is True
    assert short["items"][1] | {"id": None, "done_at": None} == bell | {
        "id": None,
        "work_status": "out_of_stock",
        "shortage_qty": None,
        "shortage_note": "供应商断货",
        "restock_date": "2026-10-08",
        "done_at": None,
    }
    too_many = await desk.client.put(f"{path}/shortage", headers=wang.headers, json={"quantity": 5})
    assert too_many.status_code == 422
    center = await call(desk, desk.admin, "GET", "/api/v1/orders?view=out_of_stock")
    assert [o["no"] for o in center["items"]] == [order["no"]] and center["items"][0]["shortage"]
    [_, shortage] = await todos_of(desk, order["id"])
    assert (shortage["code"], shortage["status"]) == ("order_shortage", "open")
    assert shortage["title"] == f"订单 {order['no']} 缺货：可视门铃 D1"
    assert shortage["detail"] == "可视门铃 D1 缺 1/1，预计 2026-10-08 到货，供应商断货"

    # 修改缺货信息：更新同一条待办。缺货时不能完成订单，也不能把缺货的商品标记完成。
    await call(desk, wang.headers, "PUT", f"{path}/shortage", note="改为下周到", restock_date=None)
    [_, again] = await todos_of(desk, order["id"])
    assert again["detail"] == "可视门铃 D1 缺 1/1，改为下周到"
    blocked = await desk.client.post(
        f"{BASE}/orders/{order['id']}/complete", headers=wang.headers, json={"mark_all": True}
    )
    assert blocked.status_code == 422 and "缺货" in blocked.text
    assert (await desk.client.post(f"{path}/done", headers=wang.headers)).status_code == 422

    # 客服登记到货：回到待加工，订单离开"缺货"，缺货待办完成。
    detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{order['id']}")
    assert detail["allowed"]["restock"] is True
    restocked = await call(
        desk, desk.admin, "POST", f"/api/v1/orders/{order['id']}/items/{bell['id']}/restock"
    )
    assert (restocked["shortage"], restocked["items"][1]["work_status"]) == (False, "pending")
    assert (await call(desk, desk.admin, "GET", "/api/v1/orders/counts"))["out_of_stock"] == 0
    assert [t["status"] for t in await todos_of(desk, order["id"])][1] == "done"

    # 又缺货：客服改订单（缺货替换）把门铃换成门锁，缺货随之处理；已完成的商品保持完成。
    await call(desk, wang.headers, "POST", f"{BASE}/orders/{order['id']}/items/{lock['id']}/done")
    await call(desk, wang.headers, "PUT", f"{path}/shortage", quantity=1)
    detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{order['id']}")
    edited = await call(
        desk,
        desk.admin,
        "PATCH",
        f"/api/v1/orders/{order['id']}",
        version=detail["version"],
        items=[
            {"product_id": products["LOCK-X1"], "quantity": 2},
            {"product_id": products["LOCK-X1"], "quantity": 1, "unit_price": "199"},
        ],
        reason="substitution",
    )
    assert [i["work_status"] for i in edited["order"]["items"]] == ["done", "pending"]
    assert edited["order"]["shortage"] is False
    assert [t["status"] for t in await todos_of(desk, order["id"])][1:] == ["done", "done"]
    assert (await call(desk, desk.admin, "GET", "/api/v1/orders/counts"))["out_of_stock"] == 0

    # 取消订单时结束还没完成的提醒。
    substitute = edited["order"]["items"][1]["id"]
    again_short = await desk.client.put(
        f"{BASE}/orders/{order['id']}/items/{substitute}/shortage", headers=wang.headers, json={}
    )
    assert again_short.status_code == 200, again_short.text
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{order['id']}/cancel",
               reason="客户不要了", notify_customer=False)  # fmt: skip
    assert [t["status"] for t in await todos_of(desk, order["id"])][-1] == "cancelled"
    assert (await call(desk, desk.admin, "GET", "/api/v1/orders/counts"))["out_of_stock"] == 0


async def test_supervisor_assigns_workers_and_edits_keep_progress(desk: Desk) -> None:
    products = await catalog(desk)
    customer_id = await customer(desk)
    order = await confirmed_order(desk, products, customer_id)
    wang = await worker(desk, "wang")
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    alice = await desk.agent("alice", online=False)

    workers = await call(desk, boss.headers, "GET", f"{BASE}/workers")
    assert [w["name"] for w in workers["items"]] == ["管理员", "Wang"]
    denied = await desk.client.post(
        f"{BASE}/orders/{order['id']}/assign",
        headers=boss.headers,
        json={"worker_id": str(alice.staff_id)},
    )
    assert denied.status_code == 422 and "加工权限" in denied.text
    forbidden = await desk.client.post(
        f"{BASE}/orders/{order['id']}/assign",
        headers=wang.headers,
        json={"worker_id": str(wang.staff_id)},
    )
    assert forbidden.status_code == 403

    # 主管指派（加工不受订单的数据范围限制）：订单开始处理，工人收到站内信，出现在他的"我的加工"里。
    detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{order['id']}")
    assert detail["allowed"]["assign_worker"] is True
    assigned = await call(
        desk,
        boss.headers,
        "POST",
        f"{BASE}/orders/{order['id']}/assign",
        worker_id=str(wang.staff_id),
    )
    assert (assigned["status"], assigned["worker_name"]) == ("fulfilling", "Wang")
    mine = await call(desk, wang.headers, "GET", f"{BASE}/orders?view=mine")
    assert [o["id"] for o in mine["items"]] == [order["id"]]
    [note] = await desk.sql(
        "SELECT title, link FROM staff_notifications WHERE staff_id = $1", wang.staff_id
    )
    assert note["title"] == f"订单 {order['no']} 交给你加工"
    # 主管没有加工权限：不能打开加工页（在订单中心指派）。
    assert (await desk.client.get(f"{BASE}/counts", headers=boss.headers)).status_code == 403

    # 加工完成后客服加了商品：退回加工，发货提醒取消；改价不影响已完成的商品。
    items = assigned["items"]
    for item in items:
        await call(
            desk, wang.headers, "POST", f"{BASE}/orders/{order['id']}/items/{item['id']}/done"
        )
    await call(desk, wang.headers, "POST", f"{BASE}/orders/{order['id']}/complete", mark_all=False)
    detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{order['id']}")
    repriced = await call(
        desk,
        desk.admin,
        "PATCH",
        f"/api/v1/orders/{order['id']}",
        version=detail["version"],
        items=[
            {"product_id": products["LOCK-X1"], "quantity": 2, "unit_price": "1199"},
            {"product_id": products["BELL-D1"], "quantity": 1},
        ],
        reason="price_adjust",
    )
    assert repriced["order"]["processed_at"] is not None
    assert [i["work_status"] for i in repriced["order"]["items"]] == ["done", "done"]
    more = await call(
        desk,
        desk.admin,
        "PATCH",
        f"/api/v1/orders/{order['id']}",
        version=repriced["order"]["version"],
        items=[
            {"product_id": products["LOCK-X1"], "quantity": 3, "unit_price": "1199"},
            {"product_id": products["BELL-D1"], "quantity": 1},
        ],
        reason="customer_request",
    )
    assert more["order"]["processed_at"] is None
    assert [i["work_status"] for i in more["order"]["items"]] == ["pending", "done"]
    [_, ship] = await todos_of(desk, order["id"])
    assert (ship["code"], ship["status"]) == ("order_ship", "cancelled")
    back = await call(desk, wang.headers, "GET", f"{BASE}/orders?view=mine")
    assert [o["id"] for o in back["items"]] == [order["id"]]

    # 放弃：退回待领取，已完成的商品保留；主管也可以把订单退回待领取。
    released = await call(desk, wang.headers, "POST", f"{BASE}/orders/{order['id']}/release")
    assert (released["worker_id"], released["can_claim"]) == (None, True)
    assert [i["work_status"] for i in released["items"]] == ["pending", "done"]
    await call(
        desk,
        boss.headers,
        "POST",
        f"{BASE}/orders/{order['id']}/assign",
        worker_id=str(wang.staff_id),
    )
    back_to_pool = await call(
        desk, boss.headers, "POST", f"{BASE}/orders/{order['id']}/assign", worker_id=None
    )
    assert back_to_pool["worker_id"] is None
    events = [
        e["type"]
        for e in (await call(desk, desk.admin, "GET", f"/api/v1/orders/{order['id']}"))["events"]
    ]
    for kind in ("worker_assigned", "item_done", "processed", "reprocess", "released"):
        assert kind in events, kind
