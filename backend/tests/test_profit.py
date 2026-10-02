"""盈利报表（设计文档 §30）：期间与比较期、利润表的口径（确认日期、取消、优惠分摊、成本价快照与
补成本价、成本缺失）、回款和未收、每月趋势、毛利分析、收支登记与每月固定、导出、菜单和权限。"""

import io
import uuid
import zipfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.errors import Unprocessable
from app.modules.profit import periods
from app.modules.profit.periods import Period
from tests.desk import Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import call, catalog, customer, new_order

PROFIT = "/api/v1/profit"
TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


def test_periods_shift_by_months() -> None:
    p = periods.previous
    # 本月 1–2 日的上期是上月 1–2 日；整月的上期是上个整月（月底对月底）。
    assert p(Period(date(2026, 10, 1), date(2026, 10, 2))) == Period(
        date(2026, 9, 1), date(2026, 9, 2)
    )
    assert p(Period(date(2026, 9, 1), date(2026, 9, 30))) == Period(
        date(2026, 8, 1), date(2026, 8, 31)
    )
    assert p(Period(date(2026, 3, 1), date(2026, 3, 31))) == Period(
        date(2026, 2, 1), date(2026, 2, 28)
    )
    assert p(Period(date(2026, 7, 1), date(2026, 9, 30))) == Period(
        date(2026, 4, 1), date(2026, 6, 30)
    )
    # 本季度：移 3 个月；不从 1 日开始的期间取紧挨着的同样天数。
    assert p(Period(date(2026, 10, 1), date(2026, 10, 2)), 3) == Period(
        date(2026, 7, 1), date(2026, 7, 2)
    )
    assert p(Period(date(2026, 9, 15), date(2026, 10, 14))) == Period(
        date(2026, 8, 16), date(2026, 9, 14)
    )
    # 去年同期：闰年的 2 月对平年的 2 月。
    assert periods.last_year(Period(date(2024, 2, 1), date(2024, 2, 29))) == Period(
        date(2023, 2, 1), date(2023, 2, 28)
    )
    months = periods.trend_months(date(2026, 10, 2))
    assert (len(months), months[0], months[-1]) == (12, date(2025, 11, 1), date(2026, 10, 1))
    with pytest.raises(Unprocessable):
        periods.check(date(2026, 10, 2), date(2026, 10, 1))
    with pytest.raises(Unprocessable):
        periods.check(date(2023, 1, 1), date(2026, 1, 31))
    assert periods.check(date(2023, 2, 1), date(2026, 1, 31)).start == date(2023, 2, 1)


# ---- 准备数据 ----


def local(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


async def product(desk: Desk, code: str, name: str, retail: str, cost: str | None) -> str:
    body: dict[str, Any] = {"code": code, "name": name, "retail_price": retail}
    if cost is not None:
        body["cost_price"] = cost
    created = await call(desk, desk.admin, "POST", "/api/v1/products", 201, **body)
    return str(created["id"])


async def booked(
    desk: Desk,
    customer_id: str,
    lines: list[dict[str, Any]],
    *,
    when: datetime | None = None,
    **extra: Any,
) -> str:
    """新建并确认一个订单（在线收款）；when 指定确认时间。"""
    order = await new_order(desk, desk.admin, customer_id, lines, **extra)
    await call(
        desk, desk.admin, "POST", f"/api/v1/orders/{order['id']}/confirm",
        payment_method="online", notify_customer=False,
    )  # fmt: skip
    if when is not None:
        await desk.sql(
            "UPDATE orders SET confirmed_at = $2 WHERE id = $1", uuid.UUID(order["id"]), when
        )
    return str(order["id"])


async def entry(desk: Desk, headers: dict[str, str], status: int = 201, **body: Any) -> Any:
    return await call(desk, headers, "POST", f"{PROFIT}/entries", status, **body)


async def get(desk: Desk, path: str, headers: dict[str, str] | None = None, **params: Any) -> Any:
    response = await desk.client.get(
        f"{PROFIT}{path}", headers=headers or desk.admin, params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


def month_range(today: date) -> dict[str, str]:
    """本月整月（结束日是月底，上期就是上个整月）。"""
    first = today.replace(day=1)
    return {"start": first.isoformat(), "end": periods.month_end(first).isoformat()}


async def ledger(desk: Desk) -> dict[str, Any]:
    """本月三笔订单（含优惠、缺成本价、低于成本卖出），草稿和取消的订单，上月和去年同月各一笔；
    本月和上月的收支。"""
    today = datetime.now(TZ).date()
    first = today.replace(day=1)
    previous_month = periods.add_months(first, -1)
    ids = await catalog(desk)
    lock, bell = ids["LOCK-X1"], ids["BELL-D1"]  # 零售价 1299 / 199，成本价 800 / 90
    install = await product(desk, "SVC-1", "安装服务", "300", None)
    li = await customer(desk, "李女士")
    wang = await customer(desk, "王先生")

    # A：两把门锁优惠 98 元，合计 2500，成本 1600。
    a = await booked(desk, li, [{"product_id": lock, "quantity": 2}], discount="98")
    # B：门铃 + 安装服务（没有成本价），合计 499；本月 1 日 00:30（北京时间）确认，算本月。
    b = await booked(
        desk, wang, [{"product_id": bell, "quantity": 1}, {"product_id": install, "quantity": 1}],
        when=local(first, 0, 30),
    )  # fmt: skip
    # C：门铃按 80 元卖出（成本 90），亏 10 元。
    c = await booked(desk, li, [{"product_id": bell, "quantity": 1, "unit_price": "80"}])
    # 草稿不算；确认后又取消的不算。
    await new_order(desk, desk.admin, li, [{"product_id": lock, "quantity": 5}])
    cancelled = await booked(desk, wang, [{"product_id": lock, "quantity": 3}])
    await call(
        desk, desk.admin, "POST", f"/api/v1/orders/{cancelled}/cancel",
        reason="客户不要了", notify_customer=False,
    )  # fmt: skip
    # 上月最后一天 23:30（北京时间，UTC 是当天 15:30）确认的门锁：算上月。
    f = await booked(
        desk, wang, [{"product_id": lock, "quantity": 1}],
        when=local(periods.month_end(previous_month), 23, 30),
    )  # fmt: skip
    # 去年同月的门铃。
    g = await booked(
        desk, li, [{"product_id": bell, "quantity": 1}], when=local(periods.add_months(first, -12))
    )
    # A 收了 1000 元。
    await call(
        desk, desk.admin, "POST", f"/api/v1/orders/{a}/payments", amount="1000", channel="bank"
    )

    # 收支：本月房租（每月固定）、工资、废料收入；上月房租。
    rent = await entry(
        desk, desk.admin, kind="expense", category="房租物业", amount="3000",
        occurred_on=today.isoformat(), recurring=True, note="10 月房租",
    )  # fmt: skip
    await entry(
        desk, desk.admin, kind="expense", category="工资社保", amount="8000",
        occurred_on=today.isoformat(),
    )  # fmt: skip
    await entry(
        desk, desk.admin, kind="income", category="废料收入", amount="200",
        occurred_on=today.isoformat(),
    )  # fmt: skip
    await entry(
        desk, desk.admin, kind="expense", category="房租物业", amount="3000",
        occurred_on=previous_month.isoformat(),
    )  # fmt: skip
    return {
        "today": today,
        "first": first,
        "previous_month": previous_month,
        "lock": lock,
        "bell": bell,
        "install": install,
        "li": li,
        "wang": wang,
        "orders": {"a": a, "b": b, "c": c, "f": f, "g": g},
        "rent": rent,
    }


async def test_statement_follows_orders_costs_and_entries(desk: Desk) -> None:
    data = await ledger(desk)
    summary = await get(desk, "/summary", **month_range(data["today"]))
    assert summary["timezone"] == "Asia/Shanghai"
    current = summary["current"]
    assert {k: current[k] for k in ("orders", "revenue", "cost", "gross_profit")} == {
        "orders": 3,
        "revenue": "3079.00",
        "cost": "1780.00",
        "gross_profit": "1299.00",
    }
    assert current["gross_margin"] == 42.2
    assert (current["other_income"], current["expenses"], current["net_profit"]) == (
        "200.00",
        "11000.00",
        "-9501.00",
    )
    assert current["net_margin"] == -308.6
    assert [(c["category"], c["amount"]) for c in current["expense_by_category"]] == [
        ("工资社保", "8000.00"),
        ("房租物业", "3000.00"),
    ]
    assert [(c["category"], c["amount"]) for c in current["income_by_category"]] == [
        ("废料收入", "200.00")
    ]
    # 上期是上个整月：上月最后一天 23:30 确认的门锁和上月房租。
    previous = summary["previous"]
    assert previous["period"]["start"] == data["previous_month"].isoformat()
    assert (previous["orders"], previous["revenue"], previous["cost"]) == (1, "1299.00", "800.00")
    assert (previous["expenses"], previous["net_profit"]) == ("3000.00", "-2501.00")
    last_year = summary["last_year"]
    assert (last_year["revenue"], last_year["gross_profit"], last_year["net_profit"]) == (
        "199.00",
        "109.00",
        "109.00",
    )
    # 回款：本月登记的收款；本期订单未收：A 还差 1500，B、C 没收。
    assert (summary["collected"], summary["uncollected"]) == ("1000.00", "2079.00")
    # 成本缺失：安装服务没有成本价，按 0 算。
    gap = summary["cost_gap"]
    assert (gap["lines"], gap["revenue"]) == (1, "300.00")
    assert [(p["name"], p["lines"], p["revenue"]) for p in gap["products"]] == [
        ("安装服务", 1, "300.00")
    ]

    # 补上成本价后自动重算；已经下的单的成本价不跟着商品库变。
    await call(
        desk, desk.admin, "PUT", f"/api/v1/products/{data['install']}",
        code="SVC-1", name="安装服务", retail_price="300", cost_price="100",
    )  # fmt: skip
    await call(
        desk, desk.admin, "PUT", f"/api/v1/products/{data['lock']}",
        code="LOCK-X1", name="智能门锁 X1", retail_price="1299", cost_price="999",
    )  # fmt: skip
    summary = await get(desk, "/summary", **month_range(data["today"]))
    assert (summary["current"]["cost"], summary["cost_gap"]["lines"]) == ("1880.00", 0)

    # 默认期间是本月 1 日到今天；指定移 12 个月时上期就是去年同期。
    default = await get(desk, "/summary")
    assert default["current"]["period"] == {
        "start": data["first"].isoformat(),
        "end": data["today"].isoformat(),
    }
    shifted = await get(desk, "/summary", shift=12, **month_range(data["today"]))
    assert shifted["previous"]["period"] == shifted["last_year"]["period"]
    bad = await desk.client.get(
        f"{PROFIT}/summary", headers=desk.admin, params={"start": "2026-10-02", "end": "2026-10-01"}
    )
    assert bad.status_code == 422

    # 每月趋势：截至本月的 12 个月。
    trend = await get(desk, "/trend", end=periods.month_end(data["first"]).isoformat())
    months = trend["months"]
    assert len(months) == 12
    assert months[-1]["month"] == periods.month_key(data["first"])
    assert (months[-1]["revenue"], months[-1]["net_profit"]) == ("3079.00", "-9601.00")
    assert (months[-2]["revenue"], months[-2]["expenses"]) == ("1299.00", "3000.00")


async def test_breakdown_by_product_customer_and_order(desk: Desk) -> None:
    data = await ledger(desk)
    period = month_range(data["today"])
    products = await get(desk, "/breakdown", by="product", **period)
    assert (products["total"], products["gross_profit"]) == (3, "1299.00")
    rows = {r["label"]: r for r in products["items"]}
    assert [r["label"] for r in products["items"]] == ["智能门锁 X1", "安装服务", "可视门铃 D1"]
    lock = rows["智能门锁 X1"]
    # 优惠按行金额分摊：2598 × 2500 ÷ 2598。
    assert (lock["quantity"], lock["revenue"], lock["cost"], lock["gross_profit"]) == (
        2,
        "2500.00",
        "1600.00",
        "900.00",
    )
    assert (lock["avg_price"], lock["unit_cost"], lock["share"]) == ("1250.00", "800.00", 69.3)
    assert lock["detail"].startswith("LOCK-X1")
    assert (rows["安装服务"]["missing_cost"], rows["安装服务"]["cost"]) == (1, "0.00")
    bell = rows["可视门铃 D1"]
    assert (bell["orders"], bell["quantity"], bell["revenue"], bell["gross_profit"]) == (
        2,
        2,
        "279.00",
        "99.00",
    )
    # 分页。
    page = await get(desk, "/breakdown", by="product", limit=2, offset=2, **period)
    assert (page["total"], [r["label"] for r in page["items"]]) == (3, ["可视门铃 D1"])

    # 按订单、毛利率从低到高：先看到亏本的订单。
    orders = await get(desk, "/breakdown", by="order", sort="margin", direction="asc", **period)
    assert [r["order_id"] for r in orders["items"]] == [
        data["orders"]["c"],
        data["orders"]["a"],
        data["orders"]["b"],
    ]
    loss = orders["items"][0]
    assert (loss["gross_profit"], loss["gross_margin"], loss["detail"]) == (
        "-10.00",
        -12.5,
        "李女士",
    )
    assert loss["confirmed_on"] == data["today"].isoformat()
    assert orders["items"][2]["confirmed_on"] == data["first"].isoformat()

    customers = await get(desk, "/breakdown", by="customer", **period)
    assert [(r["label"], r["revenue"], r["gross_profit"]) for r in customers["items"]] == [
        ("李女士", "2580.00", "890.00"),
        ("王先生", "499.00", "409.00"),
    ]
    sources = await get(desk, "/breakdown", by="source", **period)
    assert [(r["label"], r["orders"]) for r in sources["items"]] == [("员工新建", 3)]
    channels = await get(desk, "/breakdown", by="channel", **period)
    assert [(r["label"], r["orders"]) for r in channels["items"]] == [("没有会话", 3)]
    assignees = await get(desk, "/breakdown", by="assignee", sort="revenue", **period)
    assert sum(Decimal(r["revenue"]) for r in assignees["items"]) == Decimal("3079.00")


async def test_entries_recurring_categories_and_audit(desk: Desk) -> None:
    today = datetime.now(TZ).date()
    first = today.replace(day=1)
    previous_month = periods.add_months(first, -1)
    month = {"start": first.isoformat(), "end": today.isoformat()}

    # 校验：金额、类别、日期（最晚到本月最后一天）。
    for body in (
        {"amount": "0"},
        {"category": ""},
        {"category": "一二三四五六七八九十一二三四五六七八九十一"},
        {"occurred_on": (periods.month_end(today) + timedelta(days=1)).isoformat()},
        {"occurred_on": "1999-12-31"},
    ):
        payload = {
            "kind": "expense",
            "category": "房租物业",
            "amount": "100",
            "occurred_on": today.isoformat(),
            **body,
        }
        await entry(desk, desk.admin, 422, **payload)

    # 上个月：每月固定的房租（25 日）、工资（已经在本月手工登记过同样的一笔）、不固定的推广费。
    day = previous_month.replace(day=25)
    rent = await entry(
        desk, desk.admin, kind="expense", category="房租物业", amount="3000",
        occurred_on=day.isoformat(), recurring=True, note="厂房",
    )  # fmt: skip
    await entry(
        desk, desk.admin, kind="expense", category="工资社保", amount="8000",
        occurred_on=day.isoformat(), recurring=True,
    )  # fmt: skip
    await entry(
        desk, desk.admin, kind="expense", category="广告推广", amount="500",
        occurred_on=day.isoformat(),
    )  # fmt: skip
    await entry(
        desk, desk.admin, kind="expense", category="工资社保", amount="8000",
        occurred_on=today.isoformat(),
    )  # fmt: skip
    custom = await entry(
        desk, desk.admin, kind="income", category=" 仓库转租 ", amount="1200.5",
        occurred_on=today.isoformat(),
    )  # fmt: skip
    assert (custom["category"], custom["amount"], custom["kind_label"]) == (
        "仓库转租",
        "1200.50",
        "收入",
    )
    assert custom["created_by_name"]
    # 类别：常用的在前，后面是用过的。
    options = await get(desk, "/categories")
    assert options["expense"][:2] == ["工资社保", "房租物业"]
    assert options["income"][-1] == "仓库转租"

    page = await get(desk, "/entries", **month)
    assert (page["total"], page["expense_total"], page["income_total"]) == (
        2,
        "8000.00",
        "1200.50",
    )
    pending = page["recurring"]
    assert pending == {
        "month": periods.month_key(first),
        "count": 1,
        "expense": "3000.00",
        "income": "0.00",
        "categories": ["房租物业"],
    }

    # 一键登记到本月：同一天，标"每月固定"；再点一次不会重复；不能登记到以后的月份。
    copied = await call(
        desk, desk.admin, "POST", f"{PROFIT}/entries/copy-recurring",
        month=periods.month_key(first),
    )  # fmt: skip
    assert copied["created"] == 1
    [item] = copied["items"]
    assert (item["category"], item["amount"], item["note"], item["recurring"]) == (
        "房租物业",
        "3000.00",
        "厂房",
        True,
    )
    assert (item["occurred_on"], item["copied_from"]) == (
        first.replace(day=min(25, periods.month_end(first).day)).isoformat(),
        rent["id"],
    )
    again = await call(
        desk, desk.admin, "POST", f"{PROFIT}/entries/copy-recurring",
        month=periods.month_key(first),
    )  # fmt: skip
    assert again["created"] == 0
    future = await desk.client.post(
        f"{PROFIT}/entries/copy-recurring",
        headers=desk.admin,
        json={"month": periods.month_key(periods.add_months(first, 1))},
    )
    assert future.status_code == 422
    assert (await get(desk, "/entries", **month))["recurring"] is None

    # 修改、删除，都记审计。
    changed = await call(
        desk, desk.admin, "PUT", f"{PROFIT}/entries/{custom['id']}",
        kind="income", category="仓库转租", amount="1300", occurred_on=today.isoformat(),
        note="10 月",
    )  # fmt: skip
    assert (changed["amount"], changed["note"], changed["updated_by_name"]) == (
        "1300.00",
        "10 月",
        changed["created_by_name"],
    )
    await call(desk, desk.admin, "DELETE", f"{PROFIT}/entries/{custom['id']}", 204)
    missing = await desk.client.put(
        f"{PROFIT}/entries/{custom['id']}",
        headers=desk.admin,
        json={
            "kind": "income",
            "category": "仓库转租",
            "amount": "1",
            "occurred_on": today.isoformat(),
        },
    )
    assert missing.status_code == 404
    actions = [
        (row["action"], row["detail"])
        for row in await desk.sql(
            "SELECT action, detail FROM audit_logs WHERE tenant_id = $1"
            " AND action LIKE 'profit.%' ORDER BY created_at, action",
            desk.tenant_id,
        )
    ]
    names = [a for a, _ in actions]
    assert names.count("profit.entry_create") == 5
    assert names[-3:] == ["profit.entry_copy", "profit.entry_update", "profit.entry_delete"]
    update = next(d for a, d in actions if a == "profit.entry_update")
    assert '"amount": ["1200.50", "1300.00"]' in update

    # 一键登记的房租在本月 25 日（可能晚于今天），按整月筛选。
    whole = {"start": first.isoformat(), "end": periods.month_end(first).isoformat()}
    filtered = await get(desk, "/entries", kind="expense", category="房租物业", **whole)
    assert [e["amount"] for e in filtered["items"]] == ["3000.00"]


async def test_export_menu_and_permissions(desk: Desk) -> None:
    data = await ledger(desk)
    period = month_range(data["today"])

    # 导出 Excel：七个工作表，利润表里有本期的销售收入；记审计。
    response = await desk.client.post(f"{PROFIT}/export", headers=desk.admin, json=period)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as book:
        workbook = book.read("xl/workbook.xml").decode()
        for name in ("利润表", "每月", "按商品", "按客户", "按处理人", "订单明细", "收支明细"):
            assert f'name="{name}"' in workbook
        statement = book.read("xl/worksheets/sheet1.xml").decode()
        assert "一、销售收入" in statement and "<v>3079.0</v>" in statement
        orders = book.read("xl/worksheets/sheet6.xml").decode()
        assert orders.count("<row ") == 4  # 表头 + 3 笔订单
    [audit] = await desk.sql(
        "SELECT detail FROM audit_logs WHERE tenant_id = $1 AND action = 'profit.export'",
        desk.tenant_id,
    )
    assert data["first"].isoformat() in audit["detail"]

    # 管理员的菜单里有"盈利报表"；主管、客服没有，接口 403。
    me = await call(desk, desk.admin, "GET", "/api/v1/me")
    assert "profit" in me["console"]["menus"]
    assert {"profit:view", "profit:manage"} <= set(me["permissions"])
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    assert "profit" not in (await call(desk, boss.headers, "GET", "/api/v1/me"))["console"]["menus"]
    for path in ("/summary", "/trend", "/breakdown", "/entries", "/categories"):
        denied = await desk.client.get(f"{PROFIT}{path}", headers=boss.headers)
        assert denied.status_code == 403, path
    denied = await desk.client.post(f"{PROFIT}/export", headers=boss.headers, json=period)
    assert denied.status_code == 403

    # 自定义角色：只能看（岗位按财务判断，菜单里有盈利报表），不能登记。
    await call(
        desk, desk.admin, "POST", "/api/v1/roles", 201, code="boss_view", name="看利润",
        permissions=["profit:view"],
    )  # fmt: skip
    viewer = await desk.agent("view", roles=["boss_view"], online=False)
    console = (await call(desk, viewer.headers, "GET", "/api/v1/me"))["console"]
    assert console == {"profiles": ["finance"], "menus": ["profit"], "home": None}
    assert (await get(desk, "/summary", viewer.headers, **period))["current"][
        "revenue"
    ] == "3079.00"
    await entry(
        desk, viewer.headers, 403, kind="expense", category="其他", amount="1",
        occurred_on=data["today"].isoformat(),
    )  # fmt: skip
    # 只给记账权限：能登记，看不到报表。
    await call(
        desk, desk.admin, "POST", "/api/v1/roles", 201, code="bookkeeper", name="记账",
        permissions=["profit:manage"],
    )  # fmt: skip
    keeper = await desk.agent("book", roles=["bookkeeper"], online=False)
    await entry(
        desk, keeper.headers, kind="expense", category="其他", amount="1",
        occurred_on=data["today"].isoformat(),
    )  # fmt: skip
    hidden = await desk.client.get(f"{PROFIT}/summary", headers=keeper.headers)
    assert hidden.status_code == 403
