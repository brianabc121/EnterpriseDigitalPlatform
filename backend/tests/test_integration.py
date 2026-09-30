"""与企业系统对接（设计文档 §25.8）：接口密钥与权限范围、开放接口（商品同步、订单创建与查询、
状态和收款回传、待办）、事件推送（发件箱、签名、退避重试、死信与重发、平台运营查看）。"""

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.integration import delivery, open_router
from app.modules.orders import sync as order_sync
from app.modules.todos import service as todo_service
from tests.desk import Desk
from tests.factories import bearer, create_platform_admin, platform_login
from tests.fake_openim import FakeOpenIM
from tests.fake_web import FakeWeb
from tests.support import DatabaseUrls

HOOK = "http://erp.example/hooks"
PHONE = "13800002222"
RECEIVER = {"name": "李四", "phone": PHONE, "address": "北京市朝阳区建国路 1 号"}


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def api_key(desk: Desk, *scopes: str, name: str = "ERP") -> str:
    response = await desk.client.post(
        "/api/v1/admin/api-keys", headers=desk.admin, json={"name": name, "scopes": list(scopes)}
    )
    assert response.status_code == 201, response.text
    key: str = response.json()["key"]
    return key


async def product(desk: Desk, key: str, code: str, **fields: Any) -> httpx.Response:
    return await desk.client.put(f"/open/v1/products/{code}", headers=bearer(key), json=fields)


async def open_order(desk: Desk, key: str, **fields: Any) -> httpx.Response:
    body = {
        "customer": {"name": "李四", "phone": PHONE},
        "items": [{"code": "LOCK-X1", "quantity": 2}],
        "receiver": RECEIVER,
        **fields,
    }
    return await desk.client.post("/open/v1/orders", headers=bearer(key), json=body)


async def post_status(desk: Desk, key: str, ref: str, **fields: Any) -> httpx.Response:
    return await desk.client.post(f"/open/v1/orders/{ref}/status", headers=bearer(key), json=fields)


async def outbox(desk: Desk) -> list[str]:
    rows = await desk.sql("SELECT event FROM webhook_events ORDER BY created_at, id")
    return [r["event"] for r in rows]


async def test_api_keys_are_hashed_scoped_and_revocable(
    desk: Desk, app: FastAPI, client: httpx.AsyncClient
) -> None:
    key = await api_key(desk, "orders:read", name="订单同步")
    assert key.startswith("edp_")
    listed = (await client.get("/api/v1/admin/api-keys", headers=desk.admin)).json()["items"]
    assert [(k["name"], k["scopes"], k["display"].endswith("_••••")) for k in listed] == [
        ("订单同步", ["orders:read"], True)
    ]
    # 平台只保存哈希。
    [row] = await desk.sql("SELECT prefix, key_hash FROM api_keys")
    assert key not in json.dumps(dict(row)) and key.split("_")[1] == row["prefix"]
    [audit] = await desk.sql("SELECT detail FROM audit_logs WHERE action = 'api_key.create'")
    assert json.loads(audit["detail"])["scopes"] == ["orders:read"]

    # 没有密钥、密钥不对、权限范围不够。
    assert (await client.get("/open/v1/orders")).status_code == 401
    wrong = bearer(key[:-2] + "xx")
    assert (await client.get("/open/v1/orders", headers=wrong)).status_code == 401
    ok = await client.get("/open/v1/orders", headers=bearer(key))
    assert ok.status_code == 200 and ok.json() == {"items": [], "next_cursor": None}
    denied = await open_order(desk, key)
    assert denied.status_code == 403 and "orders:write" in denied.json()["error"]["message"]
    [used] = await desk.sql("SELECT last_used_at FROM api_keys")
    assert used["last_used_at"] is not None

    # 没有 integration:manage 权限的员工不能管理密钥。
    agent = await desk.agent("amy", online=False)
    assert (await client.get("/api/v1/admin/api-keys", headers=agent.headers)).status_code == 403

    # 撤销后立即失效。
    key_id = listed[0]["id"]
    revoked = await client.post(f"/api/v1/admin/api-keys/{key_id}/revoke", headers=desk.admin)
    assert revoked.status_code == 200 and revoked.json()["revoked_at"] is not None
    assert (await client.get("/open/v1/orders", headers=bearer(key))).status_code == 401

    # 另一个租户的密钥看不到这个租户的订单。
    writer = await api_key(desk, "orders:write", "products:write")
    await product(desk, writer, "LOCK-X1", name="智能门锁 X1", retail_price="1299")
    created = (await open_order(desk, writer)).json()["order"]
    other = await Desk(app, client, desk.im, desk.settings, desk.database_urls).open("globex")
    stranger = await api_key(other, "orders:read")
    missing = await client.get(f"/open/v1/orders/{created['no']}", headers=bearer(stranger))
    assert missing.status_code == 404


async def test_open_api_syncs_products_and_orders_with_status_postback(desk: Desk) -> None:
    key = await api_key(desk, "products:write", "orders:read", "orders:write")

    # 按代码同步商品：新建需要名称；已存在时只改传了的字段。
    assert (await product(desk, key, "LOCK-X1", retail_price="1")).status_code == 422
    first = await product(
        desk, key, "LOCK-X1", name="智能门锁 X1", spec="黑色", retail_price="1299", cost_price="800"
    )
    assert first.status_code == 201 and first.json()["created"] is True
    again = await product(desk, key, "LOCK-X1", retail_price="1199.5")
    assert again.status_code == 200
    assert (again.json()["spec"], again.json()["retail_price"], again.json()["created"]) == (
        "黑色",
        "1199.50",
        False,
    )

    # 创建订单：按手机号找不到客户时新建；进入平台审核；同一个企业系统单号重复创建返回已有的。
    created = await open_order(desk, key, external_no="ERP-1001")
    assert created.status_code == 201, created.text
    order = created.json()["order"]
    assert (order["status"], order["source"], order["external_no"], order["total"]) == (
        "pending_review",
        "api",
        "ERP-1001",
        "2399.00",
    )
    assert order["receiver"] == RECEIVER and order["customer"]["name"] == "李四"
    repeated = await open_order(desk, key, external_no="ERP-1001")
    assert repeated.status_code == 200 and repeated.json()["order"]["id"] == order["id"]
    [customer] = await desk.sql("SELECT source_channel, phone_hash FROM customers")
    assert customer["source_channel"] == "api" and customer["phone_hash"]
    [review] = await desk.sql(
        "SELECT t.status, t.source FROM todos t JOIN todo_types tt ON tt.id = t.type_id"
        " WHERE tt.code = 'order_review'"
    )
    assert (review["status"], review["source"]) == ("open", "rule")

    # 回传：直接发货（跳过确认），带上收款方式；审核待办随之完成。
    shipped = await post_status(
        desk,
        key,
        "ERP-1001",
        status="shipped",
        payment_method="cod",
        shipping_company="顺丰速运",
        tracking_no="SF100",
        notify_customer=False,
    )
    assert shipped.status_code == 200, shipped.text
    body = shipped.json()["order"]
    assert (body["status"], body["payment_method"], body["tracking_no"]) == (
        "shipped",
        "cod",
        "SF100",
    )
    assert body["confirmed_at"] and body["started_at"] and body["shipped_at"]
    [closed] = await desk.sql(
        "SELECT t.status, t.result FROM todos t JOIN todo_types tt ON tt.id = t.type_id"
        " WHERE tt.code = 'order_review'"
    )
    assert (closed["status"], closed["result"]) == ("done", "企业系统已确认订单")
    events = await desk.sql(
        "SELECT type, actor_type, public FROM order_events WHERE type IN"
        " ('confirmed', 'shipped') ORDER BY created_at, id"
    )
    assert [(e["type"], e["actor_type"], e["public"]) for e in events] == [
        ("confirmed", "api", True),
        ("shipped", "api", True),
    ]

    # 收款：同一个流水号只登记一次；状态不能回退；完成后不能再改状态。
    for _ in range(2):
        paid = await post_status(
            desk,
            key,
            order["no"],
            payments=[{"amount": "2399.00", "channel": "cash", "reference_no": "PAY-1"}],
        )
        assert paid.status_code == 200, paid.text
    payments = await desk.sql("SELECT recorded_by_type, reference_no FROM order_payments")
    assert [(p["recorded_by_type"], p["reference_no"]) for p in payments] == [("api", "PAY-1")]
    assert paid.json()["order"]["payment_status"] == "paid"
    back = await post_status(desk, key, order["no"], status="confirmed")
    assert back.status_code == 409 and back.json()["error"]["message"] == "订单状态不能回退"
    done = await post_status(desk, key, order["no"], status="completed", notify_customer=False)
    assert done.json()["order"]["status"] == "completed"
    late = await post_status(desk, key, order["no"], status="cancelled")
    assert late.status_code == 409
    revisions = await desk.sql("SELECT kind, actor_type FROM order_revisions ORDER BY version")
    assert {r["actor_type"] for r in revisions} == {"api"}
    assert [r["kind"] for r in revisions] == ["created", "status", "payment", "status"]

    # 企业系统已确认的订单：需要收款方式；直接记为已确认。
    no_method = await open_order(desk, key, external_no="ERP-1002", status="confirmed")
    assert no_method.status_code == 422 and "payment_method" in no_method.json()["error"]["message"]
    confirmed = await open_order(
        desk, key, external_no="ERP-1002", status="confirmed", payment_method="online"
    )
    assert confirmed.status_code == 201 and confirmed.json()["order"]["status"] == "confirmed"

    # 按更新时间增量同步，分页。
    since = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    page = await desk.client.get(
        "/open/v1/orders", headers=bearer(key), params={"updated_since": since, "limit": 1}
    )
    assert page.status_code == 200 and page.json()["next_cursor"]
    rest = await desk.client.get(
        "/open/v1/orders",
        headers=bearer(key),
        params={"updated_since": since, "limit": 1, "cursor": page.json()["next_cursor"]},
    )
    numbers = [page.json()["items"][0]["no"], rest.json()["items"][0]["no"]]
    assert sorted(numbers) == sorted([order["no"], confirmed.json()["order"]["no"]])
    assert rest.json()["next_cursor"] is None
    by_external = await desk.client.get("/open/v1/orders/ERP-1002", headers=bearer(key))
    assert by_external.json()["status"] == "confirmed"

    # 发件箱：提交、确认、状态变化和收款都写入了推送事件。
    assert await outbox(desk) == [
        "order.created",
        "order.confirmed",
        "order.status_changed",
        "order.payment",
        "order.status_changed",
        "order.created",
        "order.confirmed",
    ]


async def test_open_api_creates_todos_directly_into_the_list(desk: Desk) -> None:
    key = await api_key(desk, "todos:write", "orders:write", "products:write")
    await product(desk, key, "LOCK-X1", name="智能门锁 X1", retail_price="1299")
    order = (await open_order(desk, key, external_no="ERP-2001")).json()["order"]
    body = {
        "type": "callback",
        "title": "回访安装效果",
        "fields": {"best_time": "工作日晚上"},
        "order_no": "ERP-2001",
        "external_ref": "CRM-9",
    }
    created = await desk.client.post("/open/v1/todos", headers=bearer(key), json=body)
    assert created.status_code == 201, created.text
    todo = created.json()
    assert (todo["status"], todo["order_no"], todo["external_ref"]) == (
        "open",
        order["no"],
        "CRM-9",
    )
    [row] = await desk.sql(
        "SELECT source, created_by_type, customer_id FROM todos WHERE no = $1", todo["no"]
    )
    assert (row["source"], row["created_by_type"]) == ("api", "api")
    assert str(row["customer_id"]) == order["customer"]["id"]
    again = await desk.client.post("/open/v1/todos", headers=bearer(key), json=body)
    assert again.status_code == 200 and again.json()["id"] == todo["id"]
    fetched = await desk.client.get("/open/v1/todos/CRM-9", headers=bearer(key))
    assert fetched.json()["no"] == todo["no"]
    system = await desk.client.post(
        "/open/v1/todos", headers=bearer(key), json={"type": "order_review", "title": "x"}
    )
    assert system.status_code == 422


async def test_webhooks_are_signed_retried_dead_lettered_and_resent(
    desk: Desk, app: FastAPI, client: httpx.AsyncClient, fake_web: FakeWeb
) -> None:
    key = await api_key(desk, "orders:write", "products:write", "todos:write")
    await product(desk, key, "LOCK-X1", name="智能门锁 X1", retail_price="1299")
    created = await client.post(
        "/api/v1/admin/webhooks",
        headers=desk.admin,
        json={"name": "ERP", "url": HOOK, "events": ["order.created", "todo.done"]},
    )
    assert created.status_code == 201, created.text
    endpoint = created.json()
    secret = endpoint["secret"]
    assert secret.startswith("whsec_")
    [row] = await desk.sql("SELECT secret_enc FROM webhook_endpoints")
    assert secret not in row["secret_enc"]

    # 第一次推送失败：记下原因，1 分钟后重试；第二次成功。只推送订阅了的事件。
    fake_web.receiver(HOOK, fail=1)
    order = (await open_order(desk, key, status="confirmed", payment_method="cod")).json()["order"]
    first = await delivery.run(desk.ctx)
    assert first == {"dispatched": 1, "succeeded": 0, "retrying": 1, "dead": 0}
    [pending] = await desk.sql("SELECT * FROM webhook_deliveries")
    assert (pending["status"], pending["attempts"], pending["last_status"]) == ("pending", 1, 500)
    assert pending["next_attempt_at"] > datetime.now(UTC) + timedelta(seconds=50)
    await desk.sql("UPDATE webhook_deliveries SET next_attempt_at = now()")
    assert (await delivery.run(desk.ctx))["succeeded"] == 1
    [first_try, second_try] = fake_web.received
    assert first_try.body == second_try.body
    assert first_try.headers["x-edp-delivery"] == second_try.headers["x-edp-delivery"]
    text = second_try.body.decode()
    assert delivery.verify(secret, second_try.headers["x-edp-signature"], text)
    assert not delivery.verify("whsec_other", second_try.headers["x-edp-signature"], text)
    payload = json.loads(text)
    assert (payload["event"], payload["data"]["order"]["no"], payload["actor"]) == (
        "order.created",
        order["no"],
        "api",
    )
    assert payload["data"]["order"]["receiver"]["phone"] == PHONE

    # 多次失败后进入死信；租户管理员和平台运营都能看到并重发。
    fake_web.receiver(HOOK, fail=100)
    todo = (
        await client.post(
            "/open/v1/todos",
            headers=bearer(key),
            json={"type": "callback", "title": "回访", "external_ref": "CRM-1"},
        )
    ).json()
    done = await client.post(
        f"/api/v1/todos/{todo['id']}/done", headers=desk.admin, json={"result": "已回访"}
    )
    assert done.status_code == 200, done.text
    for _ in range(delivery.MAX_ATTEMPTS):
        await desk.sql(
            "UPDATE webhook_deliveries SET next_attempt_at = now() WHERE status = 'pending'"
        )
        await delivery.run(desk.ctx)
    dead = (
        await client.get(
            "/api/v1/admin/webhook-deliveries", headers=desk.admin, params={"status": "dead"}
        )
    ).json()
    assert [(d["event"], d["attempts"], d["last_status"]) for d in dead["items"]] == [
        ("todo.done", delivery.MAX_ATTEMPTS, 500)
    ]
    listed = (await client.get("/api/v1/admin/webhooks", headers=desk.admin)).json()["items"]
    assert (listed[0]["dead"], listed[0]["pending"]) == (1, 0)
    detail = await client.get(
        f"/api/v1/admin/webhook-deliveries/{dead['items'][0]['id']}", headers=desk.admin
    )
    assert json.loads(detail.json()["body"])["data"]["todo"]["external_ref"] == "CRM-1"

    await create_platform_admin(app)
    ops = bearer(await platform_login(client))
    seen = (await client.get("/platform/v1/ops/webhook-deliveries", headers=ops)).json()["items"]
    assert [(d["tenant_code"], d["event"], d["url"]) for d in seen] == [("acme", "todo.done", HOOK)]
    fake_web.receiver(HOOK)
    resent = await client.post(
        "/platform/v1/ops/webhook-deliveries/resend",
        headers=ops,
        json={"ids": [seen[0]["id"]]},
    )
    assert resent.json() == {"done": 1}
    assert (await delivery.run(desk.ctx))["succeeded"] == 1

    # 测试推送；停用的推送地址不再推送（进入死信，重新启用后可以重发）。
    ping = await client.post(f"/api/v1/admin/webhooks/{endpoint['id']}/test", headers=desk.admin)
    assert ping.json()["ok"] is True and ping.json()["status"] == 200
    assert json.loads(fake_web.received[-1].body)["event"] == "ping"
    await client.put(
        f"/api/v1/admin/webhooks/{endpoint['id']}",
        headers=desk.admin,
        json={"name": "ERP", "url": HOOK, "events": ["order.created"], "enabled": False},
    )
    await open_order(desk, key)
    assert (await delivery.run(desk.ctx))["dispatched"] == 0

    # 更换签名密钥：新密钥立即生效。
    rotated = await client.post(
        f"/api/v1/admin/webhooks/{endpoint['id']}/rotate-secret", headers=desk.admin
    )
    assert rotated.json()["secret"] != secret
    actions = await desk.sql(
        "SELECT action FROM audit_logs WHERE action LIKE 'webhook.%' ORDER BY created_at, id"
    )
    assert [a["action"] for a in actions] == [
        "webhook.create",
        "webhook.update",
        "webhook.rotate_secret",
    ]


async def test_an_unexpected_error_fails_only_its_delivery_and_old_events_are_purged(
    desk: Desk, client: httpx.AsyncClient, fake_web: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = await api_key(desk, "orders:write", "products:write")
    await product(desk, key, "LOCK-X1", name="智能门锁 X1", retail_price="1299")
    other = "http://bi.example/hooks"
    for name, url in (("ERP", HOOK), ("BI", other)):
        created = await client.post(
            "/api/v1/admin/webhooks",
            headers=desk.admin,
            json={"name": name, "url": url, "events": ["order.created"]},
        )
        assert created.status_code == 201, created.text
    fake_web.receiver(HOOK)
    await open_order(desk, key, external_no="ERP-1")

    # 投递给 BI 时出了意外的错误：只记成它的一次失败（稍后重试），同一批里 ERP 的推送照常成功。
    post = delivery.post

    async def broken(ctx: Any, url: str, secret: str, **kwargs: Any) -> delivery.Attempt:
        if url == other:
            raise RuntimeError("boom")
        return await post(ctx, url, secret, **kwargs)

    monkeypatch.setattr(delivery, "post", broken)
    assert await delivery.run(desk.ctx) == {
        "dispatched": 2,
        "succeeded": 1,
        "retrying": 1,
        "dead": 0,
    }
    rows = await desk.sql(
        "SELECT d.status, d.attempts, d.last_error FROM webhook_deliveries d"
        " JOIN webhook_endpoints e ON e.id = d.endpoint_id ORDER BY e.name"
    )
    assert [(r["status"], r["attempts"], r["last_error"]) for r in rows] == [
        ("pending", 1, "RuntimeError: boom"),
        ("succeeded", 1, None),
    ]
    assert len(fake_web.received) == 1

    # 分发超过 7 天的发件箱记录被删除，推送记录保留。
    await desk.sql("UPDATE webhook_events SET dispatched_at = now() - interval '8 days'")
    assert await delivery.purge_events(desk.ctx) == 1
    assert await desk.sql("SELECT id FROM webhook_events") == []
    kept = await desk.sql("SELECT event_id FROM webhook_deliveries")
    assert [r["event_id"] for r in kept] == [None, None]


async def test_a_request_racing_another_with_the_same_reference_does_not_fail(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """企业系统并发重试：另一个带同样商品代码、订单号或待办单号的请求在本请求查重之后、写入之前
    抢先建好了记录。本请求不报错：商品改为更新，订单和待办返回先建好的那条。"""
    key = await api_key(desk, "orders:write", "products:write", "todos:write")

    def race(module: Any, name: str, other: Callable[[], Awaitable[httpx.Response]]) -> list[Any]:
        """本请求第一次调用 module.name 时，先让另一个请求完整执行一遍（它自己的调用照常进行）。"""
        original = getattr(module, name)
        winner: list[Any] = []

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not winner:
                winner.append(None)
                response = await other()
                assert response.status_code == 201, response.text
                winner[0] = response.json()
            return await original(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)
        return winner

    # 商品：本请求查到没有这个代码后，另一个请求建好了它；本请求改为更新。
    find = open_router._find
    raced: list[bool] = []

    async def racing_find(session: Any, code: str) -> Any:
        found = await find(session, code)
        if not raced:
            raced.append(True)
            response = await product(desk, key, "LOCK-X1", name="智能门锁 X1", retail_price="1299")
            assert response.status_code == 201, response.text
        return found

    monkeypatch.setattr(open_router, "_find", racing_find)
    updated = await product(desk, key, "LOCK-X1", name="智能门锁 X1", retail_price="1399")
    assert updated.status_code == 200, updated.text
    assert Decimal(updated.json()["retail_price"]) == Decimal("1399")
    assert len(await desk.sql("SELECT id FROM products WHERE code = 'LOCK-X1'")) == 1

    # 订单：返回先建好的订单。
    first_order = race(order_sync, "_customer", lambda: open_order(desk, key, external_no="ERP-9"))
    again = await open_order(desk, key, external_no="ERP-9")
    assert again.status_code == 200, again.text
    assert again.json()["order"]["id"] == first_order[0]["order"]["id"]
    assert len(await desk.sql("SELECT id FROM orders WHERE external_no = 'ERP-9'")) == 1

    # 待办：返回先建好的待办。
    def todo() -> Awaitable[httpx.Response]:
        return desk.client.post(
            "/open/v1/todos",
            headers=bearer(key),
            json={"type": "callback", "title": "回访", "external_ref": "CRM-9"},
        )

    first_todo = race(todo_service, "create", todo)
    same = await todo()
    assert same.status_code == 200, same.text
    assert same.json()["id"] == first_todo[0]["id"]
    assert len(await desk.sql("SELECT id FROM todos WHERE external_ref = 'CRM-9'")) == 1
