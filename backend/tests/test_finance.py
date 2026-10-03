"""应收账款与财务岗位（设计文档 §28）：应收的定义（到期日按收款方式、逾期天数、账龄分段）、汇总、
列表、按客户、对账单、导出、跟进与催收、财务岗位的权限与菜单、每日逾期提醒。"""

import csv
import io
import uuid
from datetime import date as date_type
from datetime import datetime, time, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.csvfile import BOM
from app.modules.finance import jobs
from app.modules.todos import sla
from tests.desk import Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import call, catalog, customer, new_order

FINANCE = "/api/v1/finance"
FINANCE_PERMISSIONS = {
    "assistant:use",
    "customer:read",
    "dashboard:view",
    "finance:manage",
    "finance:view",
    "opportunity:read",
    "opportunity:read_all",
    "order:payment",
    "order:read",
    "profit:manage",
    "profit:view",
    "task:use",
    "token:view",
}


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def confirmed(
    desk: Desk, customer_id: str, product_id: str, quantity: int = 1, **confirm: Any
) -> dict[str, Any]:
    order = await new_order(
        desk, desk.admin, customer_id, [{"product_id": product_id, "quantity": quantity}]
    )
    result = await call(
        desk, desk.admin, "POST", f"/api/v1/orders/{order['id']}/confirm",
        notify_customer=False, **confirm,
    )  # fmt: skip
    order_out: dict[str, Any] = result["order"]
    return order_out


async def shipped(desk: Desk, order_id: str) -> None:
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{order_id}/start")
    await call(
        desk, desk.admin, "POST", f"/api/v1/orders/{order_id}/ship",
        shipping_company="顺丰", tracking_no="SF1", notify_customer=False,
    )  # fmt: skip


async def pay(desk: Desk, order_id: str, amount: str) -> dict[str, Any]:
    paid: dict[str, Any] = await call(
        desk, desk.admin, "POST", f"/api/v1/orders/{order_id}/payments", amount=amount,
        channel="bank",
    )  # fmt: skip
    return paid


async def backdate(desk: Desk, order_id: str, column: str, days: int) -> None:
    assert column in ("confirmed_at", "shipped_at")
    await desk.sql(
        f"UPDATE orders SET {column} = {column} - make_interval(days => $2) WHERE id = $1",
        uuid.UUID(order_id),
        days,
    )


async def summary(desk: Desk, headers: dict[str, str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = await call(
        desk, headers or desk.admin, "GET", f"{FINANCE}/receivables/summary"
    )
    return result


async def receivables(desk: Desk, **params: Any) -> dict[str, Any]:
    response = await desk.client.get(f"{FINANCE}/receivables", headers=desk.admin, params=params)
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return page


async def ledger(desk: Desk) -> dict[str, Any]:
    """两位客户、七个订单：暂欠逾期 100 天、在线收款确认了 3 天没收、货到付款发货 40 天没收、
    货到付款今天发货、预付定金收了定金等加工（未到期）、一个收清的、一个草稿。"""
    products = await catalog(desk)
    li = await customer(desk, "李女士")
    wang = await customer(desk, "王先生")
    await call(desk, desk.admin, "PATCH", f"/api/v1/customers/{wang}", company="王记五金")
    today = date_type.fromisoformat((await summary(desk))["today"])
    lock, bell = products["LOCK-X1"], products["BELL-D1"]

    credit = await confirmed(
        desk, li, lock, 2, payment_method="credit", credit_due_date=today.isoformat()
    )
    await desk.sql(
        "UPDATE orders SET credit_due_date = $2 WHERE id = $1",
        uuid.UUID(credit["id"]),
        today - timedelta(days=100),
    )
    online = await confirmed(desk, li, bell, payment_method="online")
    await backdate(desk, online["id"], "confirmed_at", 3)
    cod_today = await confirmed(desk, li, bell, payment_method="cod")
    await shipped(desk, cod_today["id"])

    deposit = await confirmed(desk, wang, lock, payment_method="deposit", deposit_amount="300")
    await pay(desk, deposit["id"], "300")
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{deposit['id']}/start")
    cod = await confirmed(desk, wang, bell, 2, payment_method="cod")
    await shipped(desk, cod["id"])
    await backdate(desk, cod["id"], "shipped_at", 40)
    paid = await confirmed(desk, wang, lock, payment_method="online")
    await pay(desk, paid["id"], "1299")
    draft = await new_order(
        desk, desk.admin, li, [{"product_id": bell, "quantity": 1}], submit=False
    )
    return {
        "today": today,
        "li": li,
        "wang": wang,
        "credit": credit["id"],
        "online": online["id"],
        "cod_today": cod_today["id"],
        "deposit": deposit["id"],
        "cod": cod["id"],
        "paid": paid["id"],
        "draft": draft["id"],
    }


async def test_receivables_follow_the_payment_rules(desk: Desk) -> None:
    ids = await ledger(desk)
    today = ids["today"]

    # 汇总：五笔未收清，三笔逾期，一笔今天到期；本月已收 300 + 1299。
    totals = await summary(desk)
    assert totals["open"] == {"amount": "4393.00", "count": 5}
    assert totals["overdue"] == {"amount": "3195.00", "count": 3}
    assert totals["due_today"] == {"amount": "199.00", "count": 1}
    assert totals["due_soon"] == {"amount": "0", "count": 0}
    assert totals["received_this_month"] == "1599.00"
    assert [(b["bucket"], b["count"], b["amount"]) for b in totals["buckets"]] == [
        ("current", 2, "1198.00"),
        ("d1_30", 1, "199.00"),
        ("d31_60", 1, "398.00"),
        ("d61_90", 0, "0"),
        ("d90_plus", 1, "2598.00"),
    ]

    # 按到期日排序：逾期最久的在前，没有到期日（尾款等加工完成）的在最后。
    page = await receivables(desk)
    assert page["total"] == 5
    assert [r["id"] for r in page["items"]] == [
        ids["credit"], ids["cod"], ids["online"], ids["cod_today"], ids["deposit"]
    ]  # fmt: skip
    by_id = {r["id"]: r for r in page["items"]}
    credit = by_id[ids["credit"]]
    assert (credit["overdue_days"], credit["bucket"], credit["outstanding"]) == (
        100, "d90_plus", "2598.00",
    )  # fmt: skip
    assert credit["due_date"] == (today - timedelta(days=100)).isoformat()
    assert (credit["age_days"], credit["customer_name"], credit["status"]) == (
        0, "李女士", "confirmed",
    )  # fmt: skip
    online = by_id[ids["online"]]
    assert (online["overdue_days"], online["age_days"], online["bucket"]) == (3, 3, "d1_30")
    assert online["due_date"] == (today - timedelta(days=3)).isoformat()
    cod = by_id[ids["cod"]]
    assert (cod["overdue_days"], cod["bucket"], cod["status"], cod["outstanding"]) == (
        40, "d31_60", "shipped", "398.00",
    )  # fmt: skip
    assert cod["customer_company"] == "王记五金"
    deposit = by_id[ids["deposit"]]
    assert (deposit["due_date"], deposit["overdue_days"], deposit["bucket"]) == (
        None, 0, "current",
    )  # fmt: skip
    assert (deposit["paid_amount"], deposit["outstanding"]) == ("300.00", "999.00")
    assert by_id[ids["cod_today"]]["due_date"] == today.isoformat()

    # 范围、账龄分段、排序和关键字。
    assert [r["id"] for r in (await receivables(desk, view="overdue"))["items"]] == [
        ids["credit"], ids["cod"], ids["online"],
    ]  # fmt: skip
    assert (await receivables(desk, view="due_today"))["total"] == 1
    assert (await receivables(desk, view="not_due"))["total"] == 2
    assert (await receivables(desk, bucket="d31_60"))["total"] == 1
    assert [r["id"] for r in (await receivables(desk, sort="outstanding"))["items"]][:2] == [
        ids["credit"], ids["deposit"],
    ]  # fmt: skip
    assert (await receivables(desk, q="李"))["total"] == 3
    assert (await receivables(desk, q="五金"))["total"] == 2
    assert (await receivables(desk, payment_method="cod"))["total"] == 2
    assert (await receivables(desk, customer_id=ids["wang"]))["total"] == 2

    # 按客户汇总。
    customers = await call(desk, desk.admin, "GET", f"{FINANCE}/receivables/customers")
    assert customers["total"] == 2
    li, wang = customers["items"]
    assert (li["customer_name"], li["outstanding"], li["overdue"], li["orders"]) == (
        "李女士", "2996.00", "2797.00", 3,
    )  # fmt: skip
    assert (li["earliest_due"], li["max_overdue_days"], li["last_paid_at"]) == (
        (today - timedelta(days=100)).isoformat(), 100, None,
    )  # fmt: skip
    assert (wang["company"], wang["outstanding"], wang["overdue"], wang["orders"]) == (
        "王记五金", "1397.00", "398.00", 2,
    )  # fmt: skip
    assert wang["max_overdue_days"] == 40 and wang["last_paid_at"] is not None
    only = await call(
        desk, desk.admin, "GET", f"{FINANCE}/receivables/customers?overdue_only=true&q=王"
    )
    assert [c["customer_name"] for c in only["items"]] == ["王先生"]

    # 最近登记的收款。
    recent = await call(desk, desk.admin, "GET", f"{FINANCE}/receivables/recent-payments")
    assert [(p["amount"], p["customer_name"], p["kind"]) for p in recent["items"]] == [
        ("1299.00", "王先生", "payment"),
        ("300.00", "王先生", "payment"),
    ]
    assert recent["items"][0]["recorded_by_name"]

    # 对账单：期初、期间的订单和收款、期末；期间之后的对账单只有期初。
    statement = await call(
        desk, desk.admin, "GET",
        f"{FINANCE}/customers/{ids['wang']}/statement"
        f"?from={(today - timedelta(days=120)).isoformat()}&to={today.isoformat()}",
    )  # fmt: skip
    assert (statement["opening"], statement["closing"]) == ("0", "1397.00")
    assert (statement["orders_amount"], statement["received"], statement["refunded"]) == (
        "2996.00", "1599.00", "0",
    )  # fmt: skip
    assert [(line["kind"], line["amount"], line["balance"]) for line in statement["lines"]] == [
        ("order", "1299.00", "1299.00"),
        ("order", "398.00", "1697.00"),
        ("order", "1299.00", "2996.00"),
        ("payment", "300.00", "2696.00"),
        ("payment", "1299.00", "1397.00"),
    ]
    assert statement["lines"][3]["description"] == "银行转账"
    assert len(statement["open_orders"]) == 2 and statement["generated_by"]
    tomorrow = (today + timedelta(days=1)).isoformat()
    later = await call(
        desk, desk.admin, "GET",
        f"{FINANCE}/customers/{ids['wang']}/statement?from={tomorrow}&to={tomorrow}",
    )  # fmt: skip
    assert (later["opening"], later["lines"], later["closing"]) == ("1397.00", [], "1397.00")

    # 导出当前筛选（逾期的三笔），记审计。
    response = await desk.client.post(
        f"{FINANCE}/receivables/export", headers=desk.admin, json={"view": "overdue"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].startswith('attachment; filename="receivables-')
    rows = list(csv.reader(io.StringIO(response.text.removeprefix(BOM))))
    assert rows[0][:4] == ["订单号", "客户", "公司", "收款方式"]
    assert [(r[1], r[3], r[7], r[10]) for r in rows[1:]] == [
        ("李女士", "暂欠", "2598.00", "100"),
        ("王先生", "货到付款", "398.00", "40"),
        ("李女士", "在线收款", "199.00", "3"),
    ]
    [audit] = await desk.sql(
        "SELECT detail::text AS detail FROM audit_logs WHERE action = 'finance.export'"
    )
    assert '"rows": 3' in audit["detail"]


async def test_follow_up_collection_and_the_daily_reminder(desk: Desk) -> None:
    ids = await ledger(desk)
    today = ids["today"]
    path = f"{FINANCE}/receivables"

    # 跟进：承诺付款日和备注；只写备注时承诺日不变；什么都没填不行。先给在线收款的订单记一个
    # 已经过了的承诺日。
    await call(
        desk, desk.admin, "POST", f"{path}/{ids['online']}/followup",
        promise_date=(today - timedelta(days=2)).isoformat(),
    )  # fmt: skip
    followed = await call(
        desk, desk.admin, "POST", f"{path}/{ids['credit']}/followup",
        promise_date=(today + timedelta(days=5)).isoformat(), note="客户说下周付",
    )  # fmt: skip
    assert followed["promise_date"] == (today + timedelta(days=5)).isoformat()
    assert followed["follow_up_note"] == "客户说下周付" and followed["followed_up_at"]
    assert followed["promise_overdue"] is False
    again = await call(
        desk, desk.admin, "POST", f"{path}/{ids['credit']}/followup", note="再次电话"
    )
    assert (again["promise_date"], again["follow_up_note"]) == (
        followed["promise_date"],
        "再次电话",
    )
    empty = await desk.client.post(f"{path}/{ids['credit']}/followup", headers=desk.admin, json={})
    assert empty.status_code == 422
    assert (await receivables(desk, view="promised"))["total"] == 2
    overdue_promise = await receivables(desk, view="promise_overdue")
    assert [r["id"] for r in overdue_promise["items"]] == [ids["online"]]
    assert overdue_promise["items"][0]["promise_overdue"] is True
    detail = await call(desk, desk.admin, "GET", f"/api/v1/orders/{ids['credit']}")
    assert [e["type"] for e in detail["events"] if e["type"].startswith("collection")] == [
        "collection_followup", "collection_followup",
    ]  # fmt: skip
    customers = await call(desk, desk.admin, "GET", f"{FINANCE}/receivables/customers")
    assert customers["items"][0]["follow_up_note"] == "再次电话"

    # 催收：生成一条催收待办交给订单处理人（管理员自己）；同时只能有一条；收清后自动完成。
    me = await call(desk, desk.admin, "GET", "/api/v1/me")
    collected = await call(desk, desk.admin, "POST", f"{path}/{ids['cod']}/collect")
    todo = collected["collection_todo"]
    assert (todo["status"], todo["assignee_id"]) == ("open", me["id"])
    [row] = await desk.sql("SELECT title, detail FROM todos WHERE id = $1", uuid.UUID(todo["id"]))
    assert row["title"].startswith("催收：订单 ") and "398.00 元未收" in row["detail"]
    assert f"到期日 {(today - timedelta(days=40)):%Y-%m-%d}" in row["detail"]
    duplicate = await desk.client.post(f"{path}/{ids['cod']}/collect", headers=desk.admin, json={})
    assert duplicate.status_code == 409
    mei = await desk.agent("mei", roles=["agent"], online=False)
    to_mei = await call(
        desk, desk.admin, "POST", f"{path}/{ids['online']}/collect",
        assignee_id=str(mei.staff_id),
    )  # fmt: skip
    assert to_mei["collection_todo"]["assignee_name"] == "Mei"
    await pay(desk, ids["cod"], "398")
    [done] = await desk.sql("SELECT status FROM todos WHERE id = $1", uuid.UUID(todo["id"]))
    assert done["status"] == "done"
    assert (await receivables(desk, customer_id=ids["wang"]))["total"] == 1
    gone = await desk.client.post(f"{path}/{ids['cod']}/collect", headers=desk.admin, json={})
    assert gone.status_code == 404

    # 每日提醒：租户时区 9 点后给有 finance:manage 的人（管理员）发一次；坐席不收；当天不再发。
    tz = sla.tz_of(None)
    morning = datetime.combine(today, time(10, 0), tzinfo=tz)
    assert await jobs.run_overdue_reminders(desk.ctx, now=morning) == 1
    notes = await call(desk, desk.admin, "GET", "/api/v1/notifications")
    [note] = [n for n in notes["items"] if n["kind"] == "finance_overdue"]
    assert note["title"] == "有 2 笔应收逾期，合计 ¥2,797.00"
    assert note["body"] == "最久逾期 100 天，请及时跟进。"
    assert note["link"] == "/receivables?view=overdue"
    mei_notes = await call(desk, mei.headers, "GET", "/api/v1/notifications")
    assert not [n for n in mei_notes["items"] if n["kind"] == "finance_overdue"]
    assert await jobs.run_overdue_reminders(desk.ctx, now=morning) == 0
    early = datetime.combine(today + timedelta(days=1), time(8, 0), tzinfo=tz)
    assert await jobs.run_overdue_reminders(desk.ctx, now=early) == 0


async def test_the_finance_position_is_the_admin_by_default_or_a_custom_role(desk: Desk) -> None:
    ids = await ledger(desk)

    # 管理员：菜单里有"应收账款"（默认由管理员担任财务）。
    admin = await call(desk, desk.admin, "GET", "/api/v1/me")
    assert admin["console"]["profiles"] == ["admin"]
    assert "receivables" in admin["console"]["menus"]
    assert {"finance:view", "finance:manage"} <= set(admin["permissions"])

    # 每个岗位的默认权限：财务是单独的一组。
    profiles = await call(desk, desk.admin, "GET", "/api/v1/roles/profile-permissions")
    finance = next(p for p in profiles["items"] if p["profile"] == "finance")
    assert (finance["label"], set(finance["permissions"])) == ("财务", FINANCE_PERMISSIONS)
    keeper = next(p for p in profiles["items"] if p["profile"] == "keeper")
    assert "warehouse:confirm" in keeper["permissions"]

    # 系统角色"财务"（员工导图分支新增）：岗位"财务"，权限就是岗位的默认权限；财务登录后只看到
    # 和收款、盈利有关的菜单，能看到全部订单和应收。
    roles = await call(desk, desk.admin, "GET", "/api/v1/roles")
    role = next(r for r in roles["items"] if r["code"] == "finance")
    assert (role["name"], role["is_system"], role["console"]) == ("财务", True, "finance")
    assert set(role["permissions"]) == FINANCE_PERMISSIONS
    cai = await desk.agent("cai", roles=["finance"], online=False)
    me = await call(desk, cai.headers, "GET", "/api/v1/me")
    assert me["console"] == {
        "profiles": ["finance"],
        "menus": [
            "dashboard",
            "orders",
            "receivables",
            "tasks",
            "customers",
            "opportunities",
            "assistant",
            "profit",
            "tokens",
        ],
        "home": None,
    }
    assert (await call(desk, cai.headers, "GET", "/api/v1/orders"))["total"] == 7
    assert (await summary(desk, cai.headers))["open"]["count"] == 5
    statement = await call(desk, cai.headers, "GET", f"{FINANCE}/customers/{ids['li']}/statement")
    assert statement["customer_name"] == "李女士"
    paid = await call(
        desk, cai.headers, "POST", f"/api/v1/orders/{ids['online']}/payments",
        amount="199", channel="cash",
    )  # fmt: skip
    assert paid["payment_status"] == "paid"
    # 财务默认不能改价、取消：没有 order:review。
    cancel = await desk.client.post(
        f"/api/v1/orders/{ids['credit']}/cancel",
        headers=cai.headers,
        json={"reason": "试一下", "notify_customer": False},
    )
    assert cancel.status_code == 403

    # 没有财务权限的坐席进不了应收账款；自定义角色不选岗位时有 finance:view 的算财务。
    mei = await desk.agent("mei", roles=["agent"], online=False)
    denied = await desk.client.get(f"{FINANCE}/receivables/summary", headers=mei.headers)
    assert denied.status_code == 403
    cashier = await call(
        desk, desk.admin, "POST", "/api/v1/roles", 201, code="teller", name="收银",
        permissions=["finance:view", "order:read"],
    )  # fmt: skip
    assert (cashier["console"], cashier["console_auto"]) == ("finance", True)
    both = await call(
        desk, desk.admin, "POST", "/api/v1/roles", 201, code="agent_cashier", name="客服兼出纳",
        permissions=["workbench:use", "finance:view", "order:read"],
    )  # fmt: skip
    assert both["console"] == "agent"
