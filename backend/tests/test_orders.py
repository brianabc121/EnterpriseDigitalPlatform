"""订单（设计文档 §25.4–§25.7）：新建与审核、改价留痕与版本、收款方式与收款、发货与完成、
取消与退款、暂欠与催收、跟踪链接与"我的订单"、收货信息、数据范围、客户的个人信息请求。"""

import csv
import io
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.orders import jobs
from tests.desk import Agent, Desk, Visitor
from tests.factories import ADMIN_PASSWORD, STAFF_PASSWORD
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls

RECEIVER = {"name": "王先生", "phone": "13800001111", "address": "上海市浦东新区世纪大道 100 号"}


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def catalog(desk: Desk) -> dict[str, str]:
    ids = {}
    for code, name, retail, cost in (
        ("LOCK-X1", "智能门锁 X1", "1299", "800"),
        ("BELL-D1", "可视门铃 D1", "199", "90"),
    ):
        response = await desk.client.post(
            "/api/v1/products",
            headers=desk.admin,
            json={"code": code, "name": name, "retail_price": retail, "cost_price": cost},
        )
        assert response.status_code == 201, response.text
        ids[code] = response.json()["id"]
    return ids


async def serving(desk: Desk, agent: Agent) -> tuple[Visitor, str, str]:
    """访客咨询，由坐席接待：返回访客、客户 ID 和会话 ID。"""
    visitor = await desk.visitor()
    await desk.say(visitor, "我想买门锁")
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == agent.staff_id
    return visitor, str(chat["customer_id"]), str(chat["id"])


async def call(
    desk: Desk, headers: dict[str, str], method: str, path: str, status: int = 200, **body: Any
) -> Any:
    response = await desk.client.request(method, path, headers=headers, json=body or None)
    assert response.status_code == status, (path, response.text)
    return response.json() if response.content else None


async def new_order(
    desk: Desk, headers: dict[str, str], customer_id: str, lines: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    body = {"customer_id": customer_id, "items": lines, "receiver": RECEIVER, **extra}
    order: dict[str, Any] = await call(desk, headers, "POST", "/api/v1/orders", 201, **body)
    return order


async def customer(desk: Desk, name: str = "李女士") -> str:
    response = await desk.client.post(
        "/api/v1/customers", headers=desk.admin, json={"display_name": name}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def test_review_price_change_revisions_and_cash_on_delivery(desk: Desk) -> None:
    products = await catalog(desk)
    alice = await desk.agent("alice")
    visitor, customer_id, session_id = await serving(desk, alice)
    base = "/api/v1/orders"

    # 缺少收货信息时不能提交审核；先保存草稿。
    incomplete = await desk.client.post(
        base,
        headers=alice.headers,
        json={
            "customer_id": customer_id,
            "session_id": session_id,
            "items": [{"product_id": products["LOCK-X1"], "quantity": 2}],
        },
    )
    assert incomplete.status_code == 422
    assert "收货人、联系电话、收货地址" in incomplete.text
    draft = await new_order(
        desk,
        alice.headers,
        customer_id,
        [{"product_id": products["LOCK-X1"], "quantity": 2}],
        session_id=session_id,
        submit=False,
        payment_method="cod",
    )
    assert (draft["status"], draft["total"], draft["no"][:2]) == ("draft", "2598.00", "SO")
    assert draft["receiver"] == {
        "name": "王**",
        "phone": "138****1111",
        "address": "上海市浦东新****",
    }
    assert draft["items"][0]["cost_price"] is None  # 坐席看不到成本价
    order_id = draft["id"]
    [row] = await desk.sql("SELECT receiver::text AS receiver FROM orders")
    assert "13800001111" not in row["receiver"] and "世纪大道" not in row["receiver"]

    # 坐席不能改价（没有 order:price）。
    denied = await desk.client.patch(
        f"{base}/{order_id}",
        headers=alice.headers,
        json={
            "version": draft["version"],
            "items": [{"product_id": products["LOCK-X1"], "quantity": 2, "unit_price": "999"}],
            "reason": "customer_request",
        },
    )
    assert denied.status_code == 403

    # 提交审核：生成"订单审核"待办，交给能审核订单的小艾自己（不提醒）。
    submitted = await call(desk, alice.headers, "POST", f"{base}/{order_id}/submit")
    assert submitted["status"] == "pending_review"
    [review] = await desk.sql(
        "SELECT t.status, t.assignee_id, t.notified_at, tt.code FROM todos t"
        " JOIN todo_types tt ON tt.id = t.type_id"
    )
    assert (review["code"], review["status"], review["assignee_id"]) == (
        "order_review",
        "open",
        alice.staff_id,
    )
    assert review["notified_at"] is not None

    # 管理员改价：必须选择原因；版本号过期时要刷新重试；修改记录保留差异和原因。
    no_reason = await desk.client.patch(
        f"{base}/{order_id}",
        headers=desk.admin,
        json={
            "version": submitted["version"],
            "items": [{"product_id": products["LOCK-X1"], "quantity": 2, "unit_price": "1199"}],
        },
    )
    assert no_reason.status_code == 422
    changed = await call(
        desk,
        desk.admin,
        "PATCH",
        f"{base}/{order_id}",
        version=submitted["version"],
        items=[{"product_id": products["LOCK-X1"], "quantity": 2, "unit_price": "1199"}],
        reason="price_adjust",
        note="老客户优惠",
    )
    order = changed["order"]
    assert (order["total"], order["modified"], order["version"]) == ("2398.00", True, 3)
    stale = await desk.client.patch(
        f"{base}/{order_id}",
        headers=desk.admin,
        json={"version": submitted["version"], "customer_note": "周末送货"},
    )
    assert stale.status_code == 409
    [edit] = [r for r in order["revisions"] if r["kind"] == "edit"]
    assert (edit["reason"], edit["note"], edit["actor_name"]) == (
        "price_adjust",
        "老客户优惠",
        "管理员",
    )
    assert edit["changes"]["items"]["changed"] == [
        {
            "name": "智能门锁 X1",
            "spec": "",
            "unit_price": {"from": "1299.00", "to": "1199.00"},
        }
    ]
    first = await call(desk, alice.headers, "GET", f"{base}/{order_id}/revisions/1")
    assert first["snapshot"]["items"][0]["unit_price"] == "1299.00"
    assert first["snapshot"]["receiver"]["phone"] == "138****1111"

    # 小艾确认（货到付款）：告知客户（带跟踪链接），订单审核待办自动完成。
    confirmed = await call(
        desk, alice.headers, "POST", f"{base}/{order_id}/confirm", payment_method="cod"
    )
    assert (confirmed["order"]["status"], confirmed["notice"]["status"]) == ("confirmed", "sent")
    await desk.flush()
    notice = desk.notices(visitor)[-1]
    assert "已确认" in notice and "2398.00" in notice and "货到付款" in notice
    token = notice.rsplit("?track=", 1)[1]
    [review] = await desk.sql("SELECT status, result FROM todos")
    assert (review["status"], review["result"]) == ("done", "订单已确认")

    # 处理中 → 发货 → 完成（货到付款要先登记收款）。
    await call(desk, alice.headers, "POST", f"{base}/{order_id}/start")
    shipped = await call(
        desk,
        alice.headers,
        "POST",
        f"{base}/{order_id}/ship",
        shipping_company="顺丰速运",
        tracking_no="SF1234567890",
    )
    assert shipped["order"]["status"] == "shipped"
    await desk.flush()
    assert "顺丰速运 SF1234567890" in desk.notices(visitor)[-1]
    unpaid = await desk.client.post(
        f"{base}/{order_id}/complete", headers=alice.headers, json={"notify_customer": False}
    )
    assert unpaid.status_code == 422
    assert "登记收款" in unpaid.text
    over = await desk.client.post(
        f"{base}/{order_id}/payments",
        headers=alice.headers,
        json={"amount": "3000", "channel": "cash"},
    )
    assert over.status_code == 422
    paid = await call(
        desk, alice.headers, "POST", f"{base}/{order_id}/payments", amount="2398", channel="cash"
    )
    assert (paid["payment_status"], paid["outstanding"]) == ("paid", "0.00")
    done = await call(
        desk, alice.headers, "POST", f"{base}/{order_id}/complete", notify_customer=False
    )
    assert done["order"]["status"] == "completed"
    kinds = [r["kind"] for r in done["order"]["revisions"]]
    assert kinds == ["created", "status", "edit", "status", "status", "status", "payment", "status"]
    actions = {r["action"] for r in await desk.sql("SELECT action FROM audit_logs")}
    assert {"order.update", "order.payment"} <= actions

    # 客户的跟踪页：进度、商品和金额、物流、掩码的收货信息；没有成本价、内部备注和员工信息。
    tracking = await desk.client.get(f"/api/v1/public/orders/{token}")
    assert tracking.status_code == 200, tracking.text
    page = tracking.json()
    assert (page["status"], page["total"], page["payment_method"], page["payment_status"]) == (
        "completed",
        "2398.00",
        "货到付款",
        "已收清",
    )
    assert all(step["done"] for step in page["steps"])
    assert page["receiver"]["phone"] == "138****1111"
    assert "cost" not in json.dumps(page) and "alice" not in json.dumps(page)
    assert [e["text"] for e in page["events"]][:3] == [
        "订单已提交，等待客服确认",
        "订单内容已更新",
        "订单已确认",
    ]
    assert (await desk.client.get("/api/v1/public/orders/" + "x" * 32)).status_code == 404
    # "联系客服"：下单时所在的网页渠道。
    [web] = await desk.sql("SELECT public_key FROM channel_accounts WHERE type = 'web'")
    assert page["contact_url"].endswith(f"/?key={web['public_key']}")

    # Widget 的"我的订单"。
    mine = await desk.client.get(
        "/api/v1/visitor/orders", headers={"X-Visitor-Token": visitor.visitor_token}
    )
    assert mine.json()["enabled"] is True
    [item] = mine.json()["items"]
    assert (item["no"], item["status_label"]) == (order["no"], "已完成")
    assert item["tracking_url"].endswith(token)


async def test_payment_method_rules(desk: Desk) -> None:
    products = await catalog(desk)
    customer_id = await customer(desk)
    alice = await desk.agent("alice", online=False)
    x1 = [{"product_id": products["LOCK-X1"], "quantity": 1}]

    # 在线收款：收清全款后才能开始处理。
    online = await new_order(desk, desk.admin, customer_id, x1)
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{online['id']}/confirm",
               payment_method="online", notify_customer=False)  # fmt: skip
    early = await desk.client.post(f"/api/v1/orders/{online['id']}/start", headers=desk.admin)
    assert early.status_code == 422 and "收清全款" in early.text
    await call(desk, desk.admin, "POST", f"/api/v1/orders/{online['id']}/payments",
               amount="1299", channel="wechat")  # fmt: skip
    started = await call(desk, desk.admin, "POST", f"/api/v1/orders/{online['id']}/start")
    assert started["status"] == "fulfilling"

    # 预付定金：确认时填写定金；收到定金才能开始处理；发货前收清尾款。
    deposit = await new_order(desk, desk.admin, customer_id, x1)
    path = f"/api/v1/orders/{deposit['id']}"
    missing = await desk.client.post(
        f"{path}/confirm", headers=desk.admin, json={"payment_method": "deposit"}
    )
    assert missing.status_code == 422
    await call(desk, desk.admin, "POST", f"{path}/confirm", payment_method="deposit",
               deposit_amount="300", notify_customer=False)  # fmt: skip
    assert (await desk.client.post(f"{path}/start", headers=desk.admin)).status_code == 422
    paid = await call(desk, desk.admin, "POST", f"{path}/payments", amount="300", channel="bank")
    assert paid["payment_status"] == "deposit"
    await call(desk, desk.admin, "POST", f"{path}/start")
    blocked = await desk.client.post(
        f"{path}/ship",
        headers=desk.admin,
        json={"shipping_company": "顺丰", "tracking_no": "SF1", "notify_customer": False},
    )
    assert blocked.status_code == 422 and "尾款" in blocked.text
    await call(desk, desk.admin, "POST", f"{path}/payments", amount="999", channel="bank")
    await call(desk, desk.admin, "POST", f"{path}/ship", shipping_company="顺丰",
               tracking_no="SF1", notify_customer=False)  # fmt: skip
    finished = await call(desk, desk.admin, "POST", f"{path}/complete", notify_customer=False)
    assert (finished["order"]["status"], finished["order"]["payment_status"]) == (
        "completed",
        "paid",
    )

    # 暂欠：需要有 order:credit 权限的员工同意；到期未收清生成催收待办，收清后自动完成。
    credit = await new_order(desk, desk.admin, customer_id, x1)
    path = f"/api/v1/orders/{credit['id']}"
    denied = await desk.client.post(
        f"{path}/confirm",
        headers=alice.headers,
        json={"payment_method": "credit", "credit_due_date": date.today().isoformat()},
    )
    assert denied.status_code in (403, 404)
    past = await desk.client.post(
        f"{path}/confirm",
        headers=desk.admin,
        json={"payment_method": "credit", "credit_due_date": "2020-01-01"},
    )
    assert past.status_code == 422
    due = (datetime.now(UTC) + timedelta(days=1)).date()
    confirmed = await call(desk, desk.admin, "POST", f"{path}/confirm", payment_method="credit",
                           credit_due_date=due.isoformat(), notify_customer=False)  # fmt: skip
    assert confirmed["order"]["credit_approved_by_name"] == "管理员"
    await call(desk, desk.admin, "POST", f"{path}/start")
    completed = await call(desk, desk.admin, "POST", f"{path}/complete", notify_customer=False)
    assert (completed["order"]["status"], completed["order"]["outstanding"]) == (
        "completed",
        "1299.00",
    )
    receivable = await call(desk, desk.admin, "GET", "/api/v1/orders?view=receivable")
    assert [o["no"] for o in receivable["items"]] == [credit["no"]]

    assert await jobs.run_collections(desk.ctx) == 0  # 还没到期
    later = datetime.now(UTC) + timedelta(days=3)
    assert await jobs.run_collections(desk.ctx, now=later) == 1
    assert await jobs.run_collections(desk.ctx, now=later) == 0  # 每个订单只生成一次
    [todo] = await desk.sql(
        "SELECT t.status, t.title, t.order_id, tt.code FROM todos t"
        " JOIN todo_types tt ON tt.id = t.type_id WHERE tt.code = 'collection'"
    )
    assert (todo["status"], str(todo["order_id"])) == ("open", credit["id"])
    assert todo["title"] == f"催收：订单 {credit['no']}"
    overdue = await call(desk, desk.admin, "GET", "/api/v1/orders/counts")
    assert overdue["receivable"] == 1
    await call(desk, desk.admin, "POST", f"{path}/payments", amount="1299", channel="alipay")
    [todo] = await desk.sql(
        "SELECT t.status FROM todos t JOIN todo_types tt ON tt.id = t.type_id"
        " WHERE tt.code = 'collection'"
    )
    assert todo["status"] == "done"


async def test_discount_limit_needs_approval_permission(desk: Desk) -> None:
    products = await catalog(desk)
    customer_id = await customer(desk)
    role = await desk.client.post(
        "/api/v1/roles",
        headers=desk.admin,
        json={
            "code": "pricer",
            "name": "报价员",
            "permissions": [
                "order:read",
                "order:create",
                "order:review",
                "order:price",
                "customer:read_all",
            ],
        },
    )
    assert role.status_code == 201, role.text
    bob = await desk.agent("bob", roles=["pricer"], online=False)
    order = await new_order(
        desk, bob.headers, customer_id, [{"product_id": products["LOCK-X1"], "quantity": 1}]
    )
    # 优惠超过上限（默认 30%）需要 order:credit；上限以内可以改。
    too_much = await desk.client.patch(
        f"/api/v1/orders/{order['id']}",
        headers=bob.headers,
        json={
            "version": order["version"],
            "items": [{"product_id": products["LOCK-X1"], "quantity": 1, "unit_price": "800"}],
            "reason": "price_adjust",
        },
    )
    assert too_much.status_code == 403 and "优惠超过上限" in too_much.text
    ok = await call(
        desk,
        bob.headers,
        "PATCH",
        f"/api/v1/orders/{order['id']}",
        version=order["version"],
        items=[{"product_id": products["LOCK-X1"], "quantity": 1, "unit_price": "1000"}],
        reason="price_adjust",
    )
    assert ok["order"]["total"] == "1000.00"
    # 下架的商品不能下单；已有订单使用的商品不能删除。
    off = await desk.client.put(
        f"/api/v1/products/{products['BELL-D1']}",
        headers=desk.admin,
        json={"code": "BELL-D1", "name": "可视门铃 D1", "status": "off"},
    )
    assert off.status_code == 200
    blocked = await desk.client.post(
        "/api/v1/orders",
        headers=bob.headers,
        json={
            "customer_id": customer_id,
            "items": [{"product_id": products["BELL-D1"], "quantity": 1}],
        },
    )
    assert blocked.status_code == 422 and "已下架" in blocked.text
    used = await desk.client.delete(f"/api/v1/products/{products['LOCK-X1']}", headers=desk.admin)
    assert used.status_code == 409


async def test_cancel_refund_link_reveal_scope_and_erasure(desk: Desk) -> None:
    products = await catalog(desk)
    alice = await desk.agent("alice")
    _, customer_id, session_id = await serving(desk, alice)
    carol = await desk.agent("carol", online=False)
    order = await new_order(
        desk,
        desk.admin,
        customer_id,
        [{"product_id": products["LOCK-X1"], "quantity": 1}],
        session_id=session_id,
    )
    path = f"/api/v1/orders/{order['id']}"
    # 数据范围：接待这位客户的小艾能看到，其他坐席看不到。
    assert (await desk.client.get(path, headers=alice.headers)).status_code == 200
    assert (await desk.client.get(path, headers=carol.headers)).status_code == 404
    listed = await call(desk, carol.headers, "GET", "/api/v1/orders")
    assert listed["total"] == 0

    await call(desk, desk.admin, "POST", f"{path}/confirm", payment_method="online",
               notify_customer=False)  # fmt: skip
    await call(desk, desk.admin, "POST", f"{path}/payments", amount="1299", channel="wechat")
    cancelled = await call(desk, desk.admin, "POST", f"{path}/cancel", reason="客户不要了")
    assert cancelled["order"]["status"] == "cancelled"
    [event] = [e for e in cancelled["order"]["events"] if e["type"] == "cancelled"]
    assert event["payload"]["refund_needed"] is True
    refunded = await call(
        desk,
        desk.admin,
        "POST",
        f"{path}/payments",
        kind="refund",
        amount="1299",
        channel="wechat",
    )
    assert refunded["payment_status"] == "refunded"
    payment_id = refunded["payments"][0]["id"]
    voided = await call(desk, desk.admin, "POST", f"{path}/payments/{payment_id}/void",
                        reason="重复登记")  # fmt: skip
    assert voided["payments"][0]["void_reason"] == "重复登记"

    # 重新生成跟踪链接：旧链接立即失效。
    old = cancelled["order"]["tracking_url"].rsplit("?track=", 1)[1]
    assert (await desk.client.get(f"/api/v1/public/orders/{old}")).status_code == 200
    renewed = await call(desk, desk.admin, "POST", f"{path}/tracking-link")
    assert (await desk.client.get(f"/api/v1/public/orders/{old}")).status_code == 404
    new = renewed["tracking_url"].rsplit("?track=", 1)[1]
    assert (await desk.client.get(f"/api/v1/public/orders/{new}")).status_code == 200

    # 查看完整的收货信息：需要查看敏感信息的权限，记审计日志。
    assert (await desk.client.post(f"{path}/reveal", headers=alice.headers)).status_code == 403
    revealed = await call(desk, desk.admin, "POST", f"{path}/reveal")
    assert revealed["receiver"] == RECEIVER
    [audit] = await desk.sql("SELECT detail FROM audit_logs WHERE action = 'order.view_receiver'")
    assert json.loads(audit["detail"])["fields"] == ["address", "name", "phone"]

    # 个人信息查询包含订单（收货信息明文）；删除请求默认清空订单里的个人信息，保留金额。
    data = await call(desk, desk.admin, "POST", f"/api/v1/customers/{customer_id}/personal-data",
                      reason="客户申请")  # fmt: skip
    assert data["orders"][0]["receiver"] == RECEIVER
    [chat_customer] = await desk.sql("SELECT display_name FROM customers WHERE id = $1",
                                     uuid.UUID(customer_id))  # fmt: skip
    erased = await call(
        desk,
        desk.admin,
        "POST",
        f"/api/v1/customers/{customer_id}/erase",
        confirm_name=chat_customer["display_name"],
        reason="客户申请",
    )
    assert erased["orders"] == 1
    [row] = await desk.sql(
        "SELECT o.receiver::text AS receiver, o.customer_id, o.total, o.tracking_expires_at,"
        " (SELECT count(*) FROM order_revisions r WHERE r.order_id = o.id"
        "  AND r.snapshot->'receiver' <> '{}'::jsonb) AS with_receiver"
        " FROM orders o"
    )
    assert (row["receiver"], row["customer_id"], str(row["total"])) == ("{}", None, "1299.00")
    assert row["with_receiver"] == 0
    assert (await desk.client.get(f"/api/v1/public/orders/{new}")).status_code == 404


def csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))


async def test_export_needs_the_password_masks_receivers_and_cost_by_permission(
    desk: Desk,
) -> None:
    products = await catalog(desk)
    customer_id = await customer(desk)
    order = await new_order(
        desk, desk.admin, customer_id, [{"product_id": products["LOCK-X1"], "quantity": 2}]
    )
    role = await desk.client.post(
        "/api/v1/roles",
        headers=desk.admin,
        json={
            "code": "exporter",
            "name": "导出员",
            "permissions": ["order:read", "order:export", "customer:read_all"],
        },
    )
    assert role.status_code == 201, role.text
    carol = await desk.agent("carol", roles=["exporter"], online=False)
    alice = await desk.agent("alice", online=False)
    body = {"password": STAFF_PASSWORD, "view": "pending_review"}

    # 坐席默认没有导出权限；密码不对不能导出。
    denied = await desk.client.post("/api/v1/orders/export", headers=alice.headers, json=body)
    assert denied.status_code == 403
    wrong = await desk.client.post(
        "/api/v1/orders/export", headers=carol.headers, json={**body, "password": "wrong"}
    )
    assert wrong.status_code == 422

    # 没有查看敏感信息、成本价的权限：收货信息是掩码，没有成本合计。
    masked = await desk.client.post("/api/v1/orders/export", headers=carol.headers, json=body)
    assert masked.status_code == 200, masked.text
    assert masked.headers["content-type"].startswith("text/csv")
    header, row = csv_rows(masked.text)
    assert "成本合计" not in header
    values = dict(zip(header, row, strict=True))
    assert (values["订单号"], values["状态"], values["合计"]) == (order["no"], "待审核", "2598.00")
    assert (values["收货人"], values["联系电话"]) == ("王**", "138****1111")

    # 管理员：收货信息明文，另有成本合计（成本价 800 × 2）。
    full = await desk.client.post(
        "/api/v1/orders/export", headers=desk.admin, json={**body, "password": ADMIN_PASSWORD}
    )
    header, row = csv_rows(full.text)
    values = dict(zip(header, row, strict=True))
    assert (values["收货人"], values["联系电话"], values["成本合计"]) == (
        "王先生",
        "13800001111",
        "1600.00",
    )
    audits = await desk.sql(
        "SELECT detail FROM audit_logs WHERE action = 'order.export' ORDER BY created_at"
    )
    assert [
        (json.loads(a["detail"])["plaintext"], json.loads(a["detail"])["cost"]) for a in audits
    ] == [(False, False), (True, True)]
    # 筛选条件与订单中心相同：没有符合的订单时只有表头。
    empty = await desk.client.post(
        "/api/v1/orders/export",
        headers=desk.admin,
        json={"password": ADMIN_PASSWORD, "view": "receivable"},
    )
    assert len(csv_rows(empty.text)) == 1
