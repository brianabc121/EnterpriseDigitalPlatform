"""库存（设计文档 §25.12）：现有、占用和可用库存，手动调整与库存记录，Excel 导入（盘点或入库，
只有代码和数量两列也可以），发货时出库（只扣一次）、已出库的订单被取消时退回，库存不足只提示，
AI 只说有没有现货，企业系统同步库存。"""

import base64
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.xlsx import Column, Sheet, write_workbook
from app.modules.kb.parsers import parse_sheet
from tests.desk import Desk
from tests.factories import bearer
from tests.fake_llm import FakeLLM
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_integration import api_key
from tests.test_order_ai import order_settings, tool_outputs
from tests.test_orders import call, customer, new_order
from tests.test_todos import ai_desk

PRODUCTS = "/api/v1/products"


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def product(desk: Desk, code: str, name: str, **fields: Any) -> dict[str, Any]:
    body = {"code": code, "name": name, "retail_price": "100", **fields}
    created: dict[str, Any] = await call(desk, desk.admin, "POST", PRODUCTS, 201, **body)
    return created


async def adjust(
    desk: Desk,
    product_id: str,
    mode: str,
    quantity: int | None = None,
    status: int = 200,
    **extra: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = await call(
        desk,
        desk.admin,
        "POST",
        f"{PRODUCTS}/{product_id}/stock",
        status,
        mode=mode,
        quantity=quantity,
        **extra,
    )
    return result


async def levels(desk: Desk, product_id: str) -> tuple[Any, ...]:
    p = await call(desk, desk.admin, "GET", f"{PRODUCTS}/{product_id}")
    return (p["stock"], p["stock_reserved"], p["stock_available"], p["stock_low"])


async def movements(desk: Desk, product_id: str) -> list[dict[str, Any]]:
    page = await call(desk, desk.admin, "GET", f"{PRODUCTS}/{product_id}/stock-movements")
    items: list[dict[str, Any]] = page["items"]
    return items


def sheet(header: list[str], rows: list[list[str]]) -> str:
    data = write_workbook([Sheet("商品", [Column(t) for t in header], rows=rows)])
    return base64.b64encode(data).decode()


async def upload(desk: Desk, headers: dict[str, str], content: str, mode: str) -> dict[str, Any]:
    body: dict[str, Any] = await call(
        desk,
        headers,
        "POST",
        f"{PRODUCTS}/imports",
        201,
        filename="库存.xlsx",
        content_base64=content,
        stock_mode=mode,
    )
    return body


async def test_adjust_stock_with_movements_and_permissions(desk: Desk) -> None:
    lock = await product(desk, "LOCK-X1", "智能门锁 X1")
    # 没填库存的商品不管理库存。
    assert await levels(desk, lock["id"]) == (None, 0, None, False)

    # 坐席看得到库存，但不能调整；主管默认可以。
    alice = await desk.agent("alice", online=False)
    denied = await desk.client.post(
        f"{PRODUCTS}/{lock['id']}/stock", headers=alice.headers, json={"mode": "add", "quantity": 1}
    )
    assert denied.status_code == 403
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    await call(
        desk, boss.headers, "POST", f"{PRODUCTS}/{lock['id']}/stock", mode="set", quantity=10,
        note="期初盘点",
    )  # fmt: skip

    # 入库、出库（不能超过现有库存）、盘点，每次都写库存记录。
    await adjust(desk, lock["id"], "add", 5, note="到货")
    too_many = await adjust(desk, lock["id"], "remove", 20, status=422)
    assert "不能超过现有库存 15" in too_many["error"]["message"]
    await adjust(desk, lock["id"], "remove", 3, note="样品")
    assert await levels(desk, lock["id"]) == (12, 0, 12, False)
    history = await movements(desk, lock["id"])
    assert [(m["kind"], m["delta"], m["stock_before"], m["stock_after"]) for m in history] == [
        ("adjust_remove", -3, 15, 12),
        ("adjust_add", 5, 10, 15),
        ("adjust_set", 10, None, 10),
    ]
    assert (history[0]["kind_label"], history[0]["note"], history[0]["actor_name"]) == (
        "出库",
        "样品",
        "管理员",
    )
    assert history[-1]["actor_name"] == "Boss"
    listed = await call(desk, alice.headers, "GET", f"{PRODUCTS}/{lock['id']}/stock-movements")
    assert listed["total"] == 3

    # 预警值：可用库存不高于预警值时算库存不足。
    updated = await call(
        desk, desk.admin, "PUT", f"{PRODUCTS}/{lock['id']}",
        code="LOCK-X1", name="智能门锁 X1", retail_price="100", stock_alert=12,
    )  # fmt: skip
    assert (updated["stock_alert"], updated["stock_low"]) == (12, True)
    low = await call(desk, desk.admin, "GET", f"{PRODUCTS}?stock=low")
    assert ([p["code"] for p in low["items"]], low["low_stock"]) == (["LOCK-X1"], 1)
    # 修改商品时不传预警值：保持原值。
    kept = await call(
        desk, desk.admin, "PUT", f"{PRODUCTS}/{lock['id']}",
        code="LOCK-X1", name="智能门锁 X1", retail_price="100",
    )  # fmt: skip
    assert kept["stock_alert"] == 12

    # 不再管理库存。
    await adjust(desk, lock["id"], "untrack")
    assert await levels(desk, lock["id"]) == (None, 0, None, False)
    assert (await movements(desk, lock["id"]))[0]["kind"] == "untrack"
    actions = [r["action"] for r in await desk.sql("SELECT action FROM audit_logs")]
    assert actions.count("product.stock") == 4  # 失败的出库不记


async def test_orders_reserve_stock_and_ship_takes_it_out_once(desk: Desk) -> None:
    # 现货商品下单即占用库存（需要加工的商品在入库之后才占用，见 test_warehouse）。
    lock = await product(desk, "LOCK-X1", "智能门锁 X1", ready_made=True)
    bell = await product(desk, "BELL-D1", "可视门铃 D1")
    await adjust(desk, lock["id"], "set", 3)
    customer_id = await customer(desk)

    async def order(quantity: int, confirm: bool = True) -> dict[str, Any]:
        lines = [{"product_id": lock["id"], "quantity": quantity}]
        if quantity == 2:
            lines.append({"product_id": bell["id"], "quantity": 1})
        created = await new_order(desk, desk.admin, customer_id, lines)
        if confirm:
            await call(desk, desk.admin, "POST", f"/api/v1/orders/{created['id']}/confirm",
                       payment_method="cod", notify_customer=False)  # fmt: skip
        return created

    async def lines(order_id: str) -> list[tuple[Any, Any]]:
        detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{order_id}")
        return [(i["stock_available"], i["stock_short"]) for i in detail["items"]]

    # 已确认的订单占用库存；不管理库存的商品不提示。
    first = await order(2)
    assert await levels(desk, lock["id"]) == (3, 2, 1, False)
    assert await lines(first["id"]) == [(1, False), (None, False)]
    # 待审核的订单还没占用：按可用库存判断，不够时提示（不拦截确认）。
    second = await order(2, confirm=False)
    assert await lines(second["id"]) == [(1, True), (None, False)]
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{second['id']}/confirm",
               payment_method="cod", notify_customer=False)  # fmt: skip
    # 按确认先后占用：第一张够，第二张占不到。
    assert await levels(desk, lock["id"]) == (3, 4, -1, True)
    assert await lines(first["id"]) == [(-1, False), (None, False)]
    assert await lines(second["id"]) == [(-1, True), (None, False)]
    listed = await call(desk, desk.admin, "GET", f"{PRODUCTS}?stock=low")
    assert [p["code"] for p in listed["items"]] == ["LOCK-X1"]

    # 工人的加工页：只提示库存不足，不给数量。
    wang = await desk.agent("wang", roles=["worker"], online=False)
    pool = await call(desk, wang.headers, "GET", "/api/v1/production/orders?view=pool")
    short = {o["no"]: [i["stock_short"] for i in o["items"]] for o in pool["items"]}
    assert short == {first["no"]: [False, False], second["no"]: [True, False]}
    assert "stock_available" not in pool["items"][0]["items"][0]

    # 发货时出库（只扣管理库存的商品）；完成时不再扣。
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{first['id']}/start")
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{first['id']}/ship",
               shipping_company="顺丰", tracking_no="SF1", notify_customer=False)  # fmt: skip
    assert await levels(desk, lock["id"]) == (1, 2, -1, True)
    [out] = [m for m in await movements(desk, lock["id"]) if m["kind"] == "order_out"]
    assert (out["delta"], out["stock_before"], out["stock_after"], out["order_no"]) == (
        -2,
        3,
        1,
        first["no"],
    )
    assert await movements(desk, bell["id"]) == []
    assert await lines(first["id"]) == [(None, False), (None, False)]
    detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{first['id']}")
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{first['id']}/payments",
               amount=detail["total"], channel="cash")  # fmt: skip
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{first['id']}/complete",
               notify_customer=False)  # fmt: skip
    assert await levels(desk, lock["id"]) == (1, 2, -1, True)

    # 取消还没发货的订单：释放占用，不改现有库存。
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{second['id']}/cancel",
               reason="客户不要了", notify_customer=False)  # fmt: skip
    assert await levels(desk, lock["id"]) == (1, 0, 1, False)

    # 没有发货环节：完成时出库。
    await order_settings(desk, shipping_enabled=False)
    third = await order(1)
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{third['id']}/start")
    detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{third['id']}")
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{third['id']}/payments",
               amount=detail["total"], channel="cash")  # fmt: skip
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{third['id']}/complete",
               notify_customer=False)  # fmt: skip
    assert await levels(desk, lock["id"]) == (0, 0, 0, True)


async def test_erp_ship_and_cancel_return_stock_and_sync_levels(desk: Desk) -> None:
    key = await api_key(desk, "products:write", "orders:read", "orders:write")
    headers = bearer(key)
    # 企业系统同步盘点数：新建商品同时设库存；数量没变时不写库存记录；null 表示不再管理。
    created = await desk.client.put(
        "/open/v1/products/LOCK-X1",
        headers=headers,
        json={"name": "智能门锁 X1", "retail_price": "100", "stock": 5, "stock_alert": 1},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["stock"], body["stock_available"], body["stock_alert"]) == (5, 5, 1)
    again = await desk.client.put("/open/v1/products/LOCK-X1", headers=headers, json={"stock": 5})
    assert again.status_code == 200
    lock_id = body["id"]
    history = await movements(desk, lock_id)
    assert [(m["kind"], m["stock_after"], m["actor_name"]) for m in history] == [
        ("api_set", 5, "企业系统")
    ]

    # 企业系统回传发货：出库；发货后又取消（拒收）：按出库记录退回。
    customer_id = await customer(desk)
    order = await new_order(desk, desk.admin, customer_id, [{"product_id": lock_id, "quantity": 2}])
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{order['id']}/confirm",
               payment_method="cod", notify_customer=False)  # fmt: skip

    async def status(**fields: Any) -> None:
        response = await desk.client.post(
            f"/open/v1/orders/{order['no']}/status",
            headers=headers,
            json={"notify_customer": False, **fields},
        )
        assert response.status_code == 200, response.text

    await status(status="shipped", shipping_company="顺丰", tracking_no="SF9")
    assert await levels(desk, lock_id) == (3, 0, 3, False)
    await status(status="cancelled", cancel_reason="客户拒收")
    assert await levels(desk, lock_id) == (5, 0, 5, False)
    kinds = [(m["kind"], m["delta"]) for m in await movements(desk, lock_id)]
    assert kinds[:2] == [("order_return", 2), ("order_out", -2)]
    [row] = await desk.sql("SELECT stock_out_at FROM orders WHERE id = $1", uuid.UUID(order["id"]))
    assert row["stock_out_at"] is None

    untracked = await desk.client.put(
        "/open/v1/products/LOCK-X1", headers=headers, json={"stock": None}
    )
    assert untracked.json()["stock"] is None
    assert (await movements(desk, lock_id))[0]["kind"] == "untrack"


async def test_excel_import_sets_or_adds_stock(desk: Desk) -> None:
    lock = await product(desk, "LOCK-X1", "智能门锁 X1")
    bell = await product(desk, "BELL-D1", "可视门铃 D1")
    await adjust(desk, bell["id"], "set", 5)

    # 商品表格（盘点）：表格里的数就是现有库存；新商品同时建立库存；库存和预警值只能填数字，
    # 成品只能填整数。
    content = sheet(
        ["名称*", "代码", "库存", "库存预警"],
        [
            ["智能门锁 X1", "LOCK-X1", "10", "2"],
            ["可视门铃 D1", "BELL-D1", "8", ""],
            ["安装配件包", "KIT-01", "3", ""],
            ["坏数据", "BAD-01", "1", "abc"],
            ["半个", "BAD-02", "1.5", ""],
        ],
    )
    preview = await upload(desk, desk.admin, content, "set")
    assert (preview["stock_mode"], preview["stock_rows"], preview["stock_ignored"]) == (
        "set",
        3,
        False,
    )
    rows = {r["row"]: r for r in preview["rows"]}
    assert [(rows[n]["stock_before"], rows[n]["stock_after"]) for n in (2, 3, 4)] == [
        (None, 10),
        (5, 8),
        (None, 3),
    ]
    assert rows[5]["problems"] == ["库存预警只能填数字"]
    assert rows[6]["problems"] == ["成品的库存只能填整数"]
    done = await call(desk, desk.admin, "POST", f"{PRODUCTS}/imports/{preview['id']}/confirm")
    assert (done["created"], done["updated"], done["skipped"]) == (1, 2, 2)
    assert (await levels(desk, lock["id"]))[0] == 10
    assert (await levels(desk, bell["id"]))[0] == 8
    items = {p["code"]: p for p in (await call(desk, desk.admin, "GET", PRODUCTS))["items"]}
    assert (items["KIT-01"]["stock"], items["LOCK-X1"]["stock_alert"]) == (3, 2)
    [imported] = [m for m in await movements(desk, lock["id"]) if m["kind"] == "import_set"]
    assert imported["import_id"] == preview["id"] and "第 2 行" in imported["note"]

    # 只有"代码"和"数量"两列（入库）：加到现有库存上；代码不存在的跳过。
    content = sheet(["代码", "数量"], [["LOCK-X1", "5"], ["BELL-D1", "2"], ["NOPE", "1"]])
    preview = await upload(desk, desk.admin, content, "add")
    rows = {r["row"]: r for r in preview["rows"]}
    assert [(rows[n]["action"], rows[n]["stock_after"]) for n in (2, 3)] == [
        ("update", 15),
        ("update", 10),
    ]
    assert rows[4]["problems"] == ["名称必填（商品库里没有代码为 NOPE 的商品）"]
    # 预览之后又有入库：确认时按最新的库存计算。
    await adjust(desk, lock["id"], "add", 1)
    done = await call(desk, desk.admin, "POST", f"{PRODUCTS}/imports/{preview['id']}/confirm")
    assert {r["row"]: r["stock_after"] for r in done["rows"] if r["result"] == "update"} == {
        2: 16,
        3: 10,
    }
    assert (await levels(desk, lock["id"]))[0] == 16
    # 只有代码和数量的表格不会清空名称等其他字段。
    unchanged = await call(desk, desk.admin, "GET", f"{PRODUCTS}/{lock['id']}")
    assert unchanged["name"] == "智能门锁 X1"
    result = await desk.client.get(f"{PRODUCTS}/imports/{preview['id']}/result", headers=desk.admin)
    notes = {r[0]: r[4] for r in parse_sheet("result.xlsx", result.content)[1:]}
    assert notes["2"] == "库存 11 → 16"

    # 没有调整库存的权限（只能维护商品库）：库存列不导入，其他列照常。
    role = await desk.client.post(
        "/api/v1/roles",
        headers=desk.admin,
        json={"code": "catalog", "name": "商品管理员", "permissions": ["product:manage"]},
    )
    assert role.status_code == 201, role.text
    dan = await desk.agent("dan", roles=["catalog"], online=False)
    content = sheet(["名称*", "代码", "库存"], [["智能门锁 X1 新款", "LOCK-X1", "99"]])
    preview = await upload(desk, dan.headers, content, "set")
    assert (preview["stock_rows"], preview["stock_ignored"]) == (0, True)
    await call(desk, dan.headers, "POST", f"{PRODUCTS}/imports/{preview['id']}/confirm")
    after = await call(desk, desk.admin, "GET", f"{PRODUCTS}/{lock['id']}")
    assert (after["name"], after["stock"]) == ("智能门锁 X1 新款", 16)

    # 导出带库存列，改完可以直接按盘点再导入。
    exported = await desk.client.get(f"{PRODUCTS}/export", headers=desk.admin)
    header, *body = parse_sheet("products.xlsx", exported.content)
    by_code = {r[header.index("代码")]: r for r in body}
    assert by_code["LOCK-X1"][header.index("库存")] == "16"
    assert by_code["LOCK-X1"][header.index("库存预警")] == "2"


async def test_ai_says_in_stock_without_numbers(
    desk: Desk, app: FastAPI, fake_llm: FakeLLM
) -> None:
    await ai_desk(desk, app)
    # 现货商品：管理库存的说有没有现货；需要加工的（即使管理库存）不提（§25.13）。
    black = await product(desk, "LOCK-X1-B", "智能门锁 X1", spec="黑色", ready_made=True)
    silver = await product(desk, "LOCK-X1-S", "智能门锁 X1", spec="银色", ready_made=True)
    gold = await product(desk, "LOCK-X1-G", "智能门锁 X1", spec="金色")
    await adjust(desk, black["id"], "set", 37)
    await adjust(desk, silver["id"], "set", 0)
    await adjust(desk, gold["id"], "set", 5)

    fake_llm.tool_plan = [("search_products", {"query": "智能门锁 X1"})]
    response = await desk.client.post(
        "/api/v1/ai/test", headers=desk.admin, json={"question": "智能门锁 X1 有货吗"}
    )
    assert response.status_code == 200, response.text
    output = tool_outputs(fake_llm)[-1]
    lines = {line.split("：")[0]: line for line in output.splitlines()[1:]}
    black_line = next(v for k, v in lines.items() if "黑色" in k)
    silver_line = next(v for k, v in lines.items() if "银色" in k)
    gold_line = next(v for k, v in lines.items() if "金色" in k)
    assert black_line.endswith("；有现货") and silver_line.endswith("；暂时缺货")
    assert "现货" not in gold_line and "缺货" not in gold_line
    assert "37" not in output
    system = fake_llm.requests[-1]["messages"][0]["content"]
    assert "不要说具体数量" in system


async def test_inventory_managers_import_stock_of_existing_products_only(desk: Desk) -> None:
    lock = await product(desk, "LOCK-X1", "智能门锁 X1")
    await adjust(desk, lock["id"], "set", 4)
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    alice = await desk.agent("alice", online=False)

    # 主管能调整库存、不能维护商品库：可以下载模板、导入导出，但只导入已有商品的库存。
    denied = await desk.client.get(f"{PRODUCTS}/template", headers=alice.headers)
    assert denied.status_code == 403
    assert (await desk.client.get(f"{PRODUCTS}/template", headers=boss.headers)).status_code == 200
    assert (await desk.client.get(f"{PRODUCTS}/export", headers=boss.headers)).status_code == 200
    content = sheet(
        ["名称*", "代码", "建议零售价", "库存"],
        [
            ["智能门锁 X1 改名", "LOCK-X1", "999", "6"],
            ["新商品", "NEW-01", "10", "3"],
            ["智能门锁 X1", "LOCK-X1", "", ""],
        ],
    )
    preview = await upload(desk, boss.headers, content, "add")
    rows = {r["row"]: r for r in preview["rows"]}
    assert (rows[2]["action"], rows[2]["stock_before"], rows[2]["stock_after"]) == ("update", 4, 10)
    assert rows[3]["problems"] == ["商品库里没有这个商品（只能导入已有商品的库存）"]
    assert rows[4]["problems"] == ["代码与第 2 行重复"]
    done = await call(desk, boss.headers, "POST", f"{PRODUCTS}/imports/{preview['id']}/confirm")
    assert (done["created"], done["updated"]) == (0, 1)
    after = await call(desk, desk.admin, "GET", f"{PRODUCTS}/{lock['id']}")
    # 只改了库存：名称和价格保持原值。
    assert (after["stock"], after["name"], after["retail_price"]) == (10, "智能门锁 X1", "100.00")
    [movement] = [m for m in await movements(desk, lock["id"]) if m["kind"] == "import_add"]
    assert movement["actor_name"] == "Boss"
