"""修改历史（设计文档 §25.14）：订单、领料单和入库单、待办、成品和材料的每一次新建、修改、删除都有
版本；同一次操作合成一个版本，内容没变的操作不记；查看时计算和上一个版本的差异；能看到记录的员工能看
它的历史，全部修改历史和已删除记录的历史只给有 audit:read 的员工；客户数据删除时历史一起删除。"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.history import service as history_service
from tests.desk import Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import call, customer, new_order
from tests.test_warehouse import goods, material, recipe

HISTORY = "/api/v1/history"
WAREHOUSE = "/api/v1/warehouse"


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


def fields(version: dict[str, Any]) -> dict[str, str]:
    return {f["label"]: f["value"] for f in version["document"]["fields"]}


def rows(version: dict[str, Any], table: str) -> list[dict[str, str]]:
    [found] = [t for t in version["document"]["tables"] if t["key"] == table]
    return [r["cells"] for r in found["rows"]]


def changed(version: dict[str, Any]) -> dict[str, tuple[str, str]]:
    return {c["label"]: (c["before"], c["after"]) for c in version["changes"]["fields"]}


async def test_order_versions_changes_and_admin_feed(desk: Desk) -> None:
    lock = await goods(desk, "LOCK-1", "智能门锁", retail_price="1000", unit="把")
    customer_id = await customer(desk)
    order = await new_order(
        desk, desk.admin, customer_id, [{"product_id": lock["id"], "quantity": 1}]
    )
    edited = await call(
        desk, desk.admin, "PATCH", f"/api/v1/orders/{order['id']}",
        version=order["version"], items=[{"product_id": lock["id"], "quantity": 3}],
        reason="customer_request", note="客户加购",
    )  # fmt: skip
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{order['id']}/confirm",
               payment_method="cod", notify_customer=False)  # fmt: skip
    # 通知客户不改变订单，不记版本。
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{order['id']}/notify", text="您好")

    history = await call(desk, desk.admin, "GET", f"{HISTORY}/order/{order['id']}")
    assert (history["label"], history["complete"], history["deleted"]) == (order["no"], True, False)
    versions = history["versions"]
    assert [(v["seq"], v["action_label"], v["actor_name"]) for v in versions] == [
        (3, "确认", "管理员"),
        (2, "修改", "管理员"),
        (1, "新建", "管理员"),
    ]
    confirm, edit, create = versions
    assert (edit["reason"], edit["note"]) == ("客户要求", "客户加购")
    [items] = [t for t in edit["changes"]["tables"] if t["key"] == "items"]
    assert [(r["cells"]) for r in items["changed"]] == [
        [
            {"key": "quantity", "before": "1", "after": "3"},
            {"key": "amount", "before": "¥1,000.00", "after": "¥3,000.00"},
        ]
    ]
    assert changed(edit)["合计"] == ("¥1,000.00", "¥3,000.00")
    assert edit["summary"].startswith("智能门锁 数量 1 → 3")
    assert changed(confirm)["状态"] == ("待审核", "已确认")
    assert changed(confirm)["收款方式"] == ("待定", "货到付款")
    assert create["changes"] == {"fields": [], "tables": []} and create["summary"] == ""
    # 收货信息只有掩码。
    assert fields(create)["联系电话"] != "13800001111" and "*" in fields(create)["联系电话"]
    assert rows(create, "items") == [
        {"name": "智能门锁", "spec": "", "quantity": "1", "unit_price": "¥1,000.00",
         "amount": "¥1,000.00", "work": "待加工"}
    ]  # fmt: skip
    assert edited["order"]["version"] == 2

    # 看不到这个订单的坐席也看不到它的历史；全部修改历史只给有 audit:read 的员工。
    alice = await desk.agent("alice")
    hidden = await desk.client.get(f"{HISTORY}/order/{order['id']}", headers=alice.headers)
    assert hidden.status_code == 404
    denied = await desk.client.get(HISTORY, headers=alice.headers)
    assert denied.status_code == 403
    feed = await call(desk, desk.admin, "GET", f"{HISTORY}?type=order")
    assert [(i["label"], i["action"], i["type_label"]) for i in feed["items"]] == [
        (order["no"], "confirm", "订单"),
        (order["no"], "update", "订单"),
        (order["no"], "create", "订单"),
    ]
    assert feed["items"][1]["summary"].startswith("智能门锁 数量 1 → 3")
    page = await call(desk, desk.admin, "GET", f"{HISTORY}?type=order&limit=2")
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = await call(desk, desk.admin, "GET", f"{HISTORY}?type=order&cursor={page['next_cursor']}")
    assert [i["action"] for i in rest["items"]] == ["create"] and rest["next_cursor"] is None
    only_changes = await call(
        desk, desk.admin, "GET", f"{HISTORY}?type=order&action=change&q={order['no']}"
    )
    assert [i["action"] for i in only_changes["items"]] == ["confirm", "update"]
    # 按单号搜索也能找到订单的审核待办（标题里有单号）：确认订单时自动完成。
    related = await call(desk, desk.admin, "GET", f"{HISTORY}?q={order['no']}")
    assert [
        (i["type_label"], i["action"]) for i in related["items"] if i["type_label"] == "待办"
    ] == [
        ("待办", "done"),
        ("待办", "create"),
    ]


async def test_requisition_versions_from_submit_to_confirm(desk: Desk) -> None:
    frame = await material(desk, "AL-6063", "铝合金型材")
    glass = await material(desk, "GL-5", "钢化玻璃", unit="平方米")
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    admin = await call(desk, desk.admin, "GET", "/api/v1/me")
    await call(desk, desk.admin, "PUT", f"{WAREHOUSE}/settings", keeper_id=admin["id"])

    doc = await call(
        desk, boss.headers, "POST", f"{WAREHOUSE}/documents", 201,
        kind="requisition", note="样品", lines=[{"product_id": frame["id"], "quantity": 2}],
    )  # fmt: skip
    await call(desk, desk.admin, "POST", f"{WAREHOUSE}/documents/{doc['id']}/reject",
               reason="少了玻璃")  # fmt: skip
    both = [
        {"product_id": frame["id"], "quantity": 2},
        {"product_id": glass["id"], "quantity": 1.5},
    ]
    await call(desk, boss.headers, "PUT", f"{WAREHOUSE}/documents/{doc['id']}", note="样品",
               lines=both)  # fmt: skip
    lines = (await call(desk, desk.admin, "GET", f"{WAREHOUSE}/documents/{doc['id']}"))["lines"]
    frame_line = next(line["id"] for line in lines if line["name"] == "铝合金型材")
    await call(desk, desk.admin, "POST", f"{WAREHOUSE}/documents/{doc['id']}/confirm",
               lines=[{"id": frame_line, "quantity": 1.75}])  # fmt: skip

    # 开单人能看自己单据的历史。
    history = await call(desk, boss.headers, "GET", f"{HISTORY}/requisition/{doc['id']}")
    confirm, update, reject, create = history["versions"]
    assert [v["action_label"] for v in history["versions"]] == [
        "确认",
        "修改后重新提交",
        "退回",
        "开单",
    ]
    assert (create["actor_name"], reject["actor_name"], confirm["actor_name"]) == (
        "Boss",
        "管理员",
        "管理员",
    )
    assert reject["reason"] == "少了玻璃" and changed(reject)["状态"] == ("待确认", "已退回")
    [lines_change] = update["changes"]["tables"]
    assert [rows(update, "lines")[0]["name"], len(lines_change["added"])] == ["铝合金型材", 1]
    assert update["summary"].startswith("明细新增 钢化玻璃")
    adjusted = {r["key"]: r["cells"] for r in confirm["changes"]["tables"][0]["changed"]}
    assert {c["key"]: (c["before"], c["after"]) for c in adjusted[frame["id"]]} == {
        "quantity": ("2", "1.75"),
        "stock": ("", "0 → -1.75"),
    }
    assert "铝合金型材 数量 2 → 1.75" in confirm["summary"]
    # 看不到单据的工人看不到历史。
    wang = await desk.agent("wang", roles=["worker"], online=False)
    hidden = await desk.client.get(f"{HISTORY}/requisition/{doc['id']}", headers=wang.headers)
    assert hidden.status_code == 404

    # 仓管开的单直接生效：开单和确认是同一次操作，只有一个版本。
    direct = await call(
        desk, desk.admin, "POST", f"{WAREHOUSE}/documents", 201,
        kind="requisition", lines=[{"product_id": frame["id"], "quantity": 1}],
    )  # fmt: skip
    [only] = (await call(desk, desk.admin, "GET", f"{HISTORY}/requisition/{direct['id']}"))[
        "versions"
    ]
    assert (only["action"], only["actions_label"]) == ("create", "开单、确认")
    assert fields(only)["状态"] == "已确认"


async def test_todo_versions_mask_sensitive_fields(desk: Desk) -> None:
    kinds = (await call(desk, desk.admin, "GET", "/api/v1/todo-types"))["items"]
    callback = next(t for t in kinds if t["code"] == "callback")
    customer_id = await customer(desk)
    todo = await call(
        desk, desk.admin, "POST", "/api/v1/todos", 201,
        type_id=callback["id"], title="回电确认安装时间", customer_id=customer_id,
        fields={"phone": "13800001111"},
    )  # fmt: skip
    await call(desk, desk.admin, "PATCH", f"/api/v1/todos/{todo['id']}",
               title="回电确认安装日期", fields={"best_time": "周六上午"})  # fmt: skip
    await call(desk, desk.admin, "POST", f"/api/v1/todos/{todo['id']}/comments", text="已电话")
    await call(desk, desk.admin, "POST", f"/api/v1/todos/{todo['id']}/done", result="约好周六")

    history = await call(desk, desk.admin, "GET", f"{HISTORY}/todo/{todo['id']}")
    assert history["label"] == f"{todo['no']} 回电确认安装日期"
    done, update, create = history["versions"]
    assert [v["action_label"] for v in history["versions"]] == ["完成", "修改", "新建"]
    assert changed(update) == {
        "标题": ("回电确认安装时间", "回电确认安装日期"),
        "方便接听的时间": ("", "周六上午"),
    }
    assert changed(done)["状态"] == ("待处理", "已完成")
    assert changed(done)["处理结果"] == ("", "约好周六")
    phone = fields(create)["回电号码"]
    assert phone != "13800001111" and "*" in phone
    # 客户数据删除时，待办和它的历史一起删除。
    await call(desk, desk.admin, "POST", f"/api/v1/customers/{customer_id}/erase",
               confirm_name="李女士", reason="客户要求删除")  # fmt: skip
    gone = await desk.sql("SELECT count(*) AS n FROM record_versions WHERE record_type = 'todo'")
    assert gone[0]["n"] == 0


async def test_product_versions_bom_delete_and_cost(desk: Desk) -> None:
    frame = await material(desk, "AL-6063", "铝合金型材")
    window = await goods(desk, "WIN-01", "铝合金窗", cost_price="600")
    # 商品有 status 字段，和 call() 的 status 参数同名，直接用 client。
    updated = await desk.client.put(
        f"/api/v1/products/{window['id']}",
        headers=desk.admin,
        json={**_writable(window), "retail_price": "1800"},
    )
    assert updated.status_code == 200, updated.text
    await recipe(desk, window["id"], {frame["id"]: 2.5})
    await call(desk, desk.admin, "POST", f"/api/v1/products/{window['id']}/stock",
               mode="set", quantity=3)  # 开始管理库存；库存数量本身不在历史里  # fmt: skip
    history = await call(desk, desk.admin, "GET", f"{HISTORY}/goods/{window['id']}")
    stock_setting, bom, update, create = history["versions"]
    assert [v["action_label"] for v in history["versions"]] == [
        "库存设置",
        "修改配方",
        "修改",
        "新建",
    ]
    assert changed(update) == {"建议零售价": ("¥100.00", "¥1,800.00")}
    assert changed(stock_setting) == {"管理库存": ("否", "是")}
    assert rows(bom, "materials") == [{"name": "铝合金型材", "quantity": "2.5 米"}]
    assert bom["summary"] == "配方（每一件的用量）新增 铝合金型材"
    assert fields(create)["成本价"] == "¥600.00"

    # 没有"查看成本价"权限的员工看不到成本价。
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    seen = await call(desk, boss.headers, "GET", f"{HISTORY}/goods/{window['id']}")
    assert "成本价" not in fields(seen["versions"][0])

    # 删除：保存删除前的内容；记录删除后只有 audit:read 能看它的历史。
    await call(desk, desk.admin, "PUT", f"/api/v1/products/{window['id']}/materials", items=[])
    await call(desk, desk.admin, "DELETE", f"/api/v1/products/{window['id']}", 204)
    history = await call(desk, desk.admin, "GET", f"{HISTORY}/goods/{window['id']}")
    deleted = history["versions"][0]
    assert (history["deleted"], deleted["action_label"], fields(deleted)["名称"]) == (
        True,
        "删除",
        "铝合金窗",
    )
    hidden = await desk.client.get(f"{HISTORY}/goods/{window['id']}", headers=boss.headers)
    assert hidden.status_code == 404
    feed = await call(desk, desk.admin, "GET", f"{HISTORY}?action=delete")
    assert [(i["label"], i["type_label"]) for i in feed["items"]] == [
        ("铝合金窗（WIN-01）", "成品")
    ]
    materials = await call(desk, desk.admin, "GET", f"{HISTORY}?type=material")
    assert [i["action"] for i in materials["items"]] == ["create"]


async def test_history_failure_does_not_fail_the_operation(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 写版本时数据库报错：只放弃这一次的版本（回滚到保存点），业务操作照常提交。
    def broken(session: Session, entry: Any) -> None:
        session.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(history_service, "_write", broken)
    lock = await goods(desk, "LOCK-9", "智能门锁")
    detail = await call(desk, desk.admin, "GET", f"/api/v1/products/{lock['id']}")
    assert detail["name"] == "智能门锁"
    missing = await desk.client.get(f"{HISTORY}/goods/{lock['id']}", headers=desk.admin)
    assert missing.status_code == 404


def _writable(product: dict[str, Any]) -> dict[str, Any]:
    keys = ("code", "name", "model", "spec", "category", "image_url", "retail_price", "remark",
            "aliases", "status", "unit", "ready_made", "cost_price")  # fmt: skip
    return {k: product[k] for k in keys if k in product}
