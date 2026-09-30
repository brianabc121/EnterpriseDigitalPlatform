"""待办与订单报表（设计文档 §24.10、§25.10）、积压与推送失败的状态指标（§19.3），以及 AI 生成的
待办、订单计入用量（§7.3）。"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.config import Settings
from app.core.dates import today
from app.modules.integration import delivery
from app.modules.todos import service as todo_service
from app.modules.todos.models import ActorType, TodoSource, TodoType
from app.modules.usage.service import rollup_day
from app.observability import state
from tests.desk import Desk
from tests.factories import bearer
from tests.fake_openim import FakeOpenIM
from tests.fake_web import FakeWeb
from tests.support import DatabaseUrls
from tests.test_usage import usage_rows

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


async def ai_todo(desk: Desk, title: str) -> str:
    """AI 接待中登记的待办（进入待确认）。"""
    async with desk.ctx.db.tenant_session(desk.tenant_id) as session:
        type_ = await session.scalar(select(TodoType).where(TodoType.code == "callback"))
        assert type_ is not None
        todo = await todo_service.create(
            session,
            desk.ctx.keys,
            todo_service.Draft(
                type=type_,
                title=title,
                source=TodoSource.AI_CHAT,
                created_by_type=ActorType.AI,
            ),
            now=datetime.now(UTC),
        )
        await session.commit()
        return str(todo.id)


async def post(desk: Desk, path: str, body: dict[str, Any] | None = None) -> Any:
    response = await desk.client.post(path, headers=desk.admin, json=body or {})
    assert response.status_code in (200, 201), response.text
    return response.json()


async def test_todo_report_counts_timeliness_and_ai_quality(desk: Desk) -> None:
    direct = await ai_todo(desk, "回电 A")
    edited = await ai_todo(desk, "回电 B")
    rejected = await ai_todo(desk, "回电 C")
    await post(desk, f"/api/v1/todos/{direct}/confirm")
    await post(desk, f"/api/v1/todos/{edited}/confirm", {"title": "回电 B（改）"})
    await post(desk, f"/api/v1/todos/{rejected}/discard", {"reason": "duplicate"})
    await ai_todo(desk, "回电 D")  # 仍待确认
    types = (await desk.client.get("/api/v1/todo-types", headers=desk.admin)).json()["items"]
    quote = next(t for t in types if t["code"] == "callback")
    staff = await post(
        desk,
        "/api/v1/todos",
        {
            "type_id": quote["id"],
            "title": "员工新建",
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    await post(desk, f"/api/v1/todos/{staff['id']}/done", {"result": "已处理"})
    await post(desk, f"/api/v1/todos/{direct}/cancel", {"reason": "客户不需要了"})

    response = await desk.client.get("/api/v1/reports/todos", headers=desk.admin)
    assert response.status_code == 200, response.text
    report = response.json()
    # 待认领只算已进入列表、还没有处理人的（不含待确认的"回电 D"）。
    [pool] = await desk.sql(
        "SELECT count(*) AS n FROM todos"
        " WHERE status IN ('open', 'in_progress', 'waiting') AND assignee_id IS NULL"
    )
    assert report["totals"] | {"oldest_unclaimed_minutes": None} == {
        "created": 5,
        "done": 1,
        "cancelled": 1,
        "rejected": 1,
        "pending_now": 1,
        "overdue_now": 0,
        "unclaimed_now": pool["n"],
        "oldest_unclaimed_minutes": None,
    }
    assert {b["key"]: b["count"] for b in report["by_source"]} == {"ai_chat": 4, "staff": 1}
    assert report["by_type"][0]["label"] == "回电 / 回访"
    assert report["ai"] | {"reject_reasons": None} == {
        "ai_created": 4,
        "confirmed_direct": 1,
        "confirmed_modified": 1,
        "rejected": 1,
        "reject_reasons": None,
        "cancelled_after_confirm": 1,
    }
    assert report["ai"]["reject_reasons"] == [
        {"key": "duplicate", "label": "重复", "count": 1, "amount": None}
    ]
    assert report["timeliness"]["on_time_rate"] == 100.0
    assert report["timeliness"]["avg_confirm_minutes"] is not None
    assert report["daily"][-1]["created"] == 5 and report["daily"][-1]["done"] == 1

    # AI 生成的待办计入用量。
    day = today(TZ)
    await rollup_day(desk.ctx.db, day, TZ)
    assert (await usage_rows(desk, day))["ai_todos"] == 4


async def test_order_report_covers_ai_business_payments_and_security(desk: Desk) -> None:
    key = await post(
        desk,
        "/api/v1/admin/api-keys",
        {"name": "ERP", "scopes": ["orders:write", "products:write"]},
    )
    headers = bearer(key["key"])
    for code, price in (("LOCK-X1", "1000"), ("EYE-C2", "300")):
        response = await desk.client.put(
            f"/open/v1/products/{code}",
            headers=headers,
            json={"name": code, "retail_price": price},
        )
        assert response.status_code == 201, response.text

    async def order(code: str, quantity: int, **fields: Any) -> dict[str, Any]:
        response = await desk.client.post(
            "/open/v1/orders",
            headers=headers,
            json={
                "customer": {"name": "客户", "phone": "13800003333"},
                "items": [{"code": code, "quantity": quantity}],
                "receiver": {"name": "客户", "phone": "13800003333", "address": "上海市"},
                **fields,
            },
        )
        assert response.status_code == 201, response.text
        result: dict[str, Any] = response.json()["order"]
        return result

    first = await order("LOCK-X1", 2)
    second = await order("EYE-C2", 1)
    third = await order(
        "LOCK-X1",
        1,
        status="confirmed",
        payment_method="credit",
        credit_due_date=today(TZ).isoformat(),
    )
    # 前两个当作 AI 采集提交的订单：一个确认后改过价（AI 识别错误），一个被取消。
    await desk.sql(
        "UPDATE orders SET created_by_type = 'ai', session_id = NULL WHERE id = ANY($1::uuid[])",
        [uuid.UUID(first["id"]), uuid.UUID(second["id"])],
    )
    await post(desk, f"/api/v1/orders/{first['id']}/confirm", {"payment_method": "cod"})
    detail = (await desk.client.get(f"/api/v1/orders/{first['id']}", headers=desk.admin)).json()
    item = detail["items"][0]
    updated = await desk.client.patch(
        f"/api/v1/orders/{first['id']}",
        headers=desk.admin,
        json={
            "version": detail["version"],
            "items": [{"product_id": item["product_id"], "quantity": 2, "unit_price": "900"}],
            "reason": "ai_error",
        },
    )
    assert updated.status_code == 200, updated.text
    await post(desk, f"/api/v1/orders/{second['id']}/cancel", {"reason": "客户不要了"})
    # 暂欠订单的约定付款日期已过。
    await desk.sql(
        "UPDATE orders SET credit_due_date = current_date - 3 WHERE id = $1", uuid.UUID(third["id"])
    )
    for kind in ("price_probe", "price_probe", "reply_blocked"):
        await desk.sql(
            "INSERT INTO ai_security_events (id, tenant_id, kind) VALUES ($1, $2, $3)",
            uuid.uuid4(),
            desk.tenant_id,
            kind,
        )
    await desk.sql(
        "INSERT INTO product_gaps (id, tenant_id, term, count) VALUES ($1, $2, '扫地机器人', 4)",
        uuid.uuid4(),
        desk.tenant_id,
    )

    response = await desk.client.get("/api/v1/reports/orders", headers=desk.admin)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["ai"] | {"modify_reasons": None} == {
        "intent_sessions": 0,  # 这两个订单没有关联会话
        "submitted": 2,
        "approval_rate": 50.0,
        "completed_rate": 0.0,
        "modified_rate": 50.0,
        "modify_reasons": None,
    }
    assert [(b["key"], b["count"]) for b in report["ai"]["modify_reasons"]] == [("ai_error", 1)]
    business = report["business"]
    assert (business["orders"], business["amount"]) == (3, "2800.00")
    assert {b["key"]: b["count"] for b in business["by_source"]} == {"api": 2}
    assert business["top_products"][0] | {"amount": None} == {
        "key": "LOCK-X1",
        "label": "LOCK-X1",
        "count": 3,
        "amount": None,
    }
    assert [(b["label"], b["count"]) for b in business["cancel_reasons"]] == [("客户不要了", 1)]
    assert [(b["label"], b["count"]) for b in business["product_gaps"]] == [("扫地机器人", 4)]
    assert business["avg_review_minutes"] is not None
    payments = report["payments"]
    assert {b["key"]: b["count"] for b in payments["by_method"]} == {"cod": 1, "credit": 1}
    assert (payments["receivable"], payments["overdue_receivable"], payments["overdue_orders"]) == (
        "2800.00",
        "1000.00",
        1,
    )
    assert report["security"] == {"price_probes": 2, "replies_blocked": 1}
    assert report["now"]["pending_review"] == 0

    # AI 提交的订单计入用量。
    day = today(TZ)
    await rollup_day(desk.ctx.db, day, TZ)
    assert (await usage_rows(desk, day))["ai_orders"] == 2

    # 没有报表权限的员工看不到。
    agent = await desk.agent("amy", online=False)
    denied = await desk.client.get("/api/v1/reports/orders", headers=agent.headers)
    assert denied.status_code == 403


async def test_state_metrics_cover_backlogs_and_failed_pushes(
    desk: Desk, fake_web: FakeWeb
) -> None:
    tenant = str(desk.tenant_id)

    async def metrics() -> dict[tuple[str, str], float]:
        values: dict[tuple[str, str], float] = {}
        for family in await state.collect(desk.ctx):
            for s in family.samples:
                if s.labels.get("tenant") == tenant:
                    values[(s.name, s.labels.get("state", ""))] = s.value
        return values

    # 待确认的待办不算待认领；进入列表、没有处理人、过了截止时间的算待认领和逾期。
    await ai_todo(desk, "回电 A")
    pooled = await ai_todo(desk, "回电 B")
    now = await metrics()
    assert now[("edp_todos_pending", "")] == 2
    assert ("edp_todos_unclaimed_oldest_seconds", "") not in now
    await desk.sql(
        "UPDATE todos SET status = 'open', assignee_id = NULL,"
        " due_at = now() - interval '1 hour' WHERE id = $1",
        uuid.UUID(pooled),
    )

    # 一个待审核的订单、一个暂欠逾期的订单；两条推送失败，其中一条已停止重试。
    key = await post(
        desk,
        "/api/v1/admin/api-keys",
        {"name": "ERP", "scopes": ["orders:write", "products:write"]},
    )
    headers = bearer(key["key"])
    await desk.client.put(
        "/open/v1/products/LOCK-X1", headers=headers, json={"name": "门锁", "retail_price": "100"}
    )
    hook = "http://erp.example/hooks"
    await post(
        desk, "/api/v1/admin/webhooks", {"name": "ERP", "url": hook, "events": ["order.created"]}
    )
    fake_web.receiver(hook, fail=100)
    for extra in ({}, {"status": "confirmed", "payment_method": "credit"}):
        response = await desk.client.post(
            "/open/v1/orders",
            headers=headers,
            json={
                "customer": {"name": "客户", "phone": "13800004444"},
                "items": [{"code": "LOCK-X1", "quantity": 1}],
                "receiver": {"name": "客户", "phone": "13800004444", "address": "上海市"},
                **extra,
                **({"credit_due_date": today(TZ).isoformat()} if extra else {}),
            },
        )
        assert response.status_code == 201, response.text
    await desk.sql(
        "UPDATE orders SET credit_due_date = current_date - 3 WHERE status = 'confirmed'"
    )
    assert (await delivery.run(desk.ctx))["retrying"] == 2
    await desk.sql(
        "UPDATE webhook_deliveries SET status = 'dead', next_attempt_at = NULL"
        " WHERE id = (SELECT id FROM webhook_deliveries ORDER BY created_at, id LIMIT 1)"
    )

    now = await metrics()
    assert now[("edp_todos_pending", "")] == 1
    assert now[("edp_todos_overdue", "")] == 1
    assert now[("edp_todos_unclaimed_oldest_seconds", "")] >= 0
    assert now[("edp_orders_pending_review", "")] == 1
    assert now[("edp_orders_pending_review_oldest_seconds", "")] >= 0
    assert now[("edp_orders_receivable_overdue", "")] == 1
    assert now[("edp_webhook_deliveries", "retrying")] == 1
    assert now[("edp_webhook_deliveries", "dead")] == 1
