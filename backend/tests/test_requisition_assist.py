"""加工时的一键领料（设计文档 §25.17）：领料单的预填写明怎么算的（每个商品的每件用量 × 数量、
已领的），没有配方时按以往领料估算（最近几张只加工这个商品、领料已确认的订单，每件用量取中位数，
至少一半的订单都领过的材料），加工卡片上的"还要领""被退回的领料单""能估算"，以及按以往领料生成
配方。"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.warehouse import usage
from tests.desk import Agent, Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import call, customer
from tests.test_warehouse import adjust, confirmed, goods, material, recipe, worker

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


async def keeper(desk: Desk) -> Agent:
    cang = await desk.agent("cang", online=False)
    await call(desk, desk.admin, "PUT", f"{BASE}/settings", keeper_id=str(cang.staff_id))
    return cang


async def draft(desk: Desk, agent: Agent, order_id: str) -> dict[str, Any]:
    found: dict[str, Any] = await call(
        desk, agent.headers, "GET", f"{BASE}/drafts?kind=requisition&order_id={order_id}"
    )
    return found


async def requisition(
    desk: Desk, agent: Agent, order_id: str, lines: dict[str, float]
) -> dict[str, Any]:
    created: dict[str, Any] = await call(
        desk, agent.headers, "POST", f"{BASE}/documents", 201,
        kind="requisition", order_id=order_id,
        lines=[{"product_id": pid, "quantity": q} for pid, q in lines.items()],
    )  # fmt: skip
    return created


async def past(
    desk: Desk,
    people: tuple[Agent, Agent],
    customer_id: str,
    lines: list[tuple[str, int]],
    used: dict[str, float],
    confirm: bool = True,
) -> str:
    """以往的订单：工人领取、开领料单，仓管确认（confirm=False 时还没确认）。"""
    wang, cang = people
    order = await confirmed(desk, customer_id, lines)
    await call(desk, wang.headers, "POST", f"{PRODUCTION}/orders/{order['id']}/claim")
    doc = await requisition(desk, wang, order["id"], used)
    if confirm:
        await call(desk, cang.headers, "POST", f"{BASE}/documents/{doc['id']}/confirm")
    order_id: str = order["id"]
    return order_id


async def card(desk: Desk, agent: Agent, order_id: str) -> dict[str, Any]:
    found: dict[str, Any] = await call(
        desk, agent.headers, "GET", f"{PRODUCTION}/orders/{order_id}"
    )
    return found


def explained(found: dict[str, Any]) -> dict[str, Any]:
    """每种材料：建议数量、已领，以及怎么算的（商品、每件用量、数量、单位、来源）。"""
    return {
        line["name"]: (
            line["quantity"],
            line["taken"],
            [
                (s["item"], s["per_unit"], s["quantity"], s["unit"], s["amount"], s["basis"])
                for s in line["sources"]
            ],
        )
        for line in found["lines"]
    }


async def test_recipe_draft_explains_lines_and_cards_show_top_ups_and_rejections(
    desk: Desk,
) -> None:
    frame = await material(desk, "AL-6063", "铝合金型材")
    glass = await material(desk, "GL-5", "钢化玻璃", unit="平方米")
    seal = await material(desk, "SEAL-1", "密封条")
    for item, quantity in ((frame, 100), (glass, 20), (seal, 12)):
        await adjust(desk, item["id"], "set", quantity)
    window = await goods(desk, "WIN-01", "铝合金窗", spec="1.2m×1.5m")
    screen = await goods(desk, "NET-01", "纱窗", unit="扇")
    lamp = await goods(desk, "LAMP-01", "吸顶灯", unit="盏", ready_made=True)
    await recipe(desk, window["id"], {frame["id"]: 6.5, glass["id"]: 1.8, seal["id"]: 8})
    await recipe(desk, screen["id"], {seal["id"]: 2})
    cang = await keeper(desk)
    wang = await worker(desk, "wang")
    order = await confirmed(
        desk, await customer(desk), [(window["id"], 2), (screen["id"], 1), (lamp["id"], 1)]
    )
    await call(desk, wang.headers, "POST", f"{PRODUCTION}/orders/{order['id']}/claim")

    # 自动填好的领料单：这次加工的商品（不含现货），每种材料写明怎么算的。
    first = await draft(desk, wang, order["id"])
    assert [
        (i["name"], i["spec"], i["quantity"], i["unit"], i["basis"]) for i in first["items"]
    ] == [
        ("铝合金窗", "1.2m×1.5m", 2, "樘", "recipe"),
        ("纱窗", "", 1, "扇", "recipe"),
    ]
    assert explained(first) == {
        "钢化玻璃": (3.6, 0, [("铝合金窗 1.2m×1.5m", 1.8, 2, "樘", 3.6, "recipe")]),
        "密封条": (
            18,
            0,
            [
                ("铝合金窗 1.2m×1.5m", 8, 2, "樘", 16, "recipe"),
                ("纱窗", 2, 1, "扇", 2, "recipe"),
            ],
        ),
        "铝合金型材": (13, 0, [("铝合金窗 1.2m×1.5m", 6.5, 2, "樘", 13, "recipe")]),
    }
    assert (first["estimated"], first["missing"], first["covered"]) == ([], [], False)
    assert not any(line["estimated"] for line in first["lines"])

    # 密封条不够，先领 10 米：再开时只填还没领的 8 米（写明已领 10）；卡片上"还要领"。
    doc = await requisition(
        desk, wang, order["id"], {frame["id"]: 13, glass["id"]: 3.6, seal["id"]: 10}
    )
    second = await draft(desk, wang, order["id"])
    assert [(ln["name"], ln["quantity"], ln["taken"]) for ln in second["lines"]] == [
        ("密封条", 8, 10)
    ]
    view = await card(desk, wang, order["id"])
    assert (view["requisition_todo"], view["requisition_rejected"]) == (["密封条 8 米"], None)

    # 仓管退回：卡片上显示被退回的领料单和原因（不算已经领料，"还要领"不再列出）。
    await call(desk, cang.headers, "POST", f"{BASE}/documents/{doc['id']}/reject",
               reason="密封条只剩 12 米，一次领完")  # fmt: skip
    view = await card(desk, wang, order["id"])
    rejected = view["requisition_rejected"]
    assert (rejected["no"], rejected["reject_reason"]) == (doc["no"], "密封条只剩 12 米，一次领完")
    assert (view["requisition_ready"], view["requisition_todo"]) == (False, [])

    # 修改后重新提交、仓管确认：领齐了。
    await call(
        desk, wang.headers, "PUT", f"{BASE}/documents/{doc['id']}", note="",
        lines=[{"product_id": pid, "quantity": q}
               for pid, q in ((frame["id"], 13), (glass["id"], 3.6), (seal["id"], 18))],
    )  # fmt: skip
    await call(desk, cang.headers, "POST", f"{BASE}/documents/{doc['id']}/confirm")
    view = await card(desk, wang, order["id"])
    assert (view["requisition_rejected"], view["requisition_todo"]) == (None, [])
    covered = await draft(desk, wang, order["id"])
    assert (covered["lines"], covered["covered"]) == ([], True)

    # 客户加了 1 樘：卡片上列出还要补领的，补领单只填多出来的那一樘。
    detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{order['id']}")
    await call(
        desk, desk.admin, "PATCH", f"/api/v1/orders/{order['id']}",
        version=detail["version"], reason="customer_request",
        items=[{"product_id": window["id"], "quantity": 3},
               {"product_id": screen["id"], "quantity": 1},
               {"product_id": lamp["id"], "quantity": 1}],
    )  # fmt: skip
    view = await card(desk, wang, order["id"])
    assert sorted(view["requisition_todo"]) == sorted(
        ["铝合金型材 6.5 米", "钢化玻璃 1.8 平方米", "密封条 8 米"]
    )
    more = await draft(desk, wang, order["id"])
    assert {ln["name"]: (ln["quantity"], ln["taken"]) for ln in more["lines"]} == {
        "铝合金型材": (6.5, 13),
        "钢化玻璃": (1.8, 3.6),
        "密封条": (8, 18),
    }
    assert more["covered"] is False


async def test_products_without_recipes_are_estimated_from_past_requisitions(desk: Desk) -> None:
    profile = await material(desk, "AL-1", "铝型材")
    screws = await material(desk, "SCR-1", "螺丝", unit="个")
    glue = await material(desk, "GLUE-1", "结构胶", unit="支")
    for item in (profile, screws, glue):
        await adjust(desk, item["id"], "set", 1000)
    door = await goods(desk, "DOOR-01", "防盗门")
    screen = await goods(desk, "NET-01", "纱窗", unit="扇")
    cang = await keeper(desk)
    wang = await worker(desk, "wang")
    customer_id = await customer(desk)
    people = (wang, cang)

    # 以往只做防盗门的订单（每樘：型材 5、6、7 米；螺丝 4、4 个；结构胶只领过一次）。
    await past(desk, people, customer_id, [(door["id"], 2)], {profile["id"]: 10, screws["id"]: 8})
    await past(desk, people, customer_id, [(door["id"], 2)], {profile["id"]: 12, glue["id"]: 1})
    await past(desk, people, customer_id, [(door["id"], 2)], {profile["id"]: 14, screws["id"]: 8})
    # 不算的：同时做了两种商品的订单、还没确认的领料单。
    await past(
        desk, people, customer_id, [(door["id"], 1), (screen["id"], 1)], {profile["id"]: 100}
    )
    await past(desk, people, customer_id, [(door["id"], 1)], {profile["id"]: 50}, confirm=False)

    order = await confirmed(desk, customer_id, [(door["id"], 3)])
    pool = await call(desk, wang.headers, "GET", f"{PRODUCTION}/orders?view=pool")
    [waiting] = [o for o in pool["items"] if o["id"] == order["id"]]
    assert (waiting["requisition_estimated"], waiting["requisition_required"]) == (True, False)
    await call(desk, wang.headers, "POST", f"{PRODUCTION}/orders/{order['id']}/claim")

    found = await draft(desk, wang, order["id"])
    assert [(i["name"], i["basis"], i["orders"]) for i in found["items"]] == [
        ("防盗门", "history", 3)
    ]
    assert (found["estimated"], found["missing"]) == (["防盗门"], [])
    assert explained(found) == {
        "螺丝": (12, 0, [("防盗门", 4, 3, "樘", 12, "history")]),
        "铝型材": (18, 0, [("防盗门", 6, 3, "樘", 18, "history")]),
    }
    assert all(line["estimated"] for line in found["lines"])
    assert {s["orders"] for line in found["lines"] for s in line["sources"]} == {3}

    # 按以往领料生成配方：维护商品库的员工看到每件约用多少（依据 3 个订单）。
    history = await call(desk, desk.admin, "GET", f"{PRODUCTS}/{door['id']}/materials/history")
    assert history["orders"] == 3
    assert [(i["name"], i["quantity"], i["unit"]) for i in history["items"]] == [
        ("螺丝", 4, "个"),
        ("铝型材", 6, "米"),
    ]
    none = await call(desk, desk.admin, "GET", f"{PRODUCTS}/{screen['id']}/materials/history")
    assert none == {"orders": 0, "items": []}
    denied = await desk.client.get(
        f"{PRODUCTS}/{door['id']}/materials/history", headers=(await worker(desk, "zhao")).headers
    )
    assert denied.status_code == 403

    # 这张订单自己的领料不算作它的以往领料：确认后估算的依据还是那 3 个订单（别的订单会算上它）。
    doc = await requisition(desk, wang, order["id"], {profile["id"]: 30, screws["id"]: 12})
    await call(desk, cang.headers, "POST", f"{BASE}/documents/{doc['id']}/confirm")
    again = await draft(desk, wang, order["id"])
    assert ([(i["name"], i["orders"]) for i in again["items"]], again["covered"]) == (
        [("防盗门", 3)],
        True,
    )
    widened = await call(desk, desk.admin, "GET", f"{PRODUCTS}/{door['id']}/materials/history")
    assert (widened["orders"], [i["quantity"] for i in widened["items"]]) == (4, [4, 6.5])

    # 既没有配方、也没有以往领料的商品：列出来，手动添加。
    mixed = await confirmed(desk, customer_id, [(door["id"], 1), (screen["id"], 1)])
    await call(desk, wang.headers, "POST", f"{PRODUCTION}/orders/{mixed['id']}/claim")
    partly = await draft(desk, wang, mixed["id"])
    assert [(i["name"], i["basis"]) for i in partly["items"]] == [
        ("防盗门", "history"),
        ("纱窗", "none"),
    ]
    assert (partly["estimated"], partly["missing"]) == (["防盗门"], ["纱窗"])

    # 保存成配方后按配方领料，不再是估算。
    await recipe(desk, door["id"], {profile["id"]: 6, screws["id"]: 4})
    order2 = await confirmed(desk, customer_id, [(door["id"], 1)])
    await call(desk, wang.headers, "POST", f"{PRODUCTION}/orders/{order2['id']}/claim")
    by_recipe = await draft(desk, wang, order2["id"])
    assert [(i["name"], i["basis"]) for i in by_recipe["items"]] == [("防盗门", "recipe")]
    assert by_recipe["estimated"] == []
    view = await card(desk, wang, order2["id"])
    assert (view["requisition_required"], view["requisition_estimated"]) == (True, False)


async def test_each_product_is_estimated_from_its_own_orders(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每个成品各看自己最近的订单：做得多的商品不会把做得少的挤掉；已经停用的材料不再列出。"""
    monkeypatch.setattr(usage, "CANDIDATES", 2)
    profile = await material(desk, "AL-1", "铝型材")
    paint = await material(desk, "PT-1", "氟碳漆", unit="桶")
    for item in (profile, paint):
        await adjust(desk, item["id"], "set", 1000)
    gate = await goods(desk, "GATE-01", "庭院门")
    door = await goods(desk, "DOOR-01", "防盗门")
    people = (await worker(desk, "wang"), await keeper(desk))
    customer_id = await customer(desk)

    # 庭院门很少做（最早的一张），防盗门最近做了好几张。
    await past(desk, people, customer_id, [(gate["id"], 1)], {profile["id"]: 8, paint["id"]: 2})
    for _ in range(3):
        await past(desk, people, customer_id, [(door["id"], 1)], {profile["id"]: 5})

    gate_order = await confirmed(desk, customer_id, [(gate["id"], 2)])
    door_order = await confirmed(desk, customer_id, [(door["id"], 1)])
    wang = people[0]
    pool = await call(desk, wang.headers, "GET", f"{PRODUCTION}/orders?view=pool")
    estimable = {o["id"]: o["requisition_estimated"] for o in pool["items"]}
    assert (estimable[gate_order["id"]], estimable[door_order["id"]]) == (True, True)
    await call(desk, wang.headers, "POST", f"{PRODUCTION}/orders/{gate_order['id']}/claim")
    found = await draft(desk, wang, gate_order["id"])
    assert {ln["name"]: ln["quantity"] for ln in found["lines"]} == {"铝型材": 16, "氟碳漆": 4}

    # 停用的材料不再按以往领料列出。
    updated = await desk.client.put(
        f"{PRODUCTS}/{paint['id']}",
        headers=desk.admin,
        json={"code": "PT-1", "name": "氟碳漆", "unit": "桶", "status": "off"},
    )
    assert updated.status_code == 200, updated.text
    found = await draft(desk, wang, gate_order["id"])
    assert {ln["name"]: ln["quantity"] for ln in found["lines"]} == {"铝型材": 16}
