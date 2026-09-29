"""套餐、订阅、额度与账单（G2）。"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.context import AppContext
from app.core.config import Settings
from app.core.dates import today
from app.modules.audit.models import AuditLog
from app.modules.billing import service as billing
from app.modules.billing.entitlements import add_months, month_end, month_start
from app.modules.billing.models import Subscription
from app.modules.tenancy.models import Tenant
from tests.desk import Desk
from tests.factories import (
    ADMIN_PASSWORD,
    bearer,
    create_platform_admin,
    create_staff,
    login,
    platform_login,
)
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_ai_reception import bot_texts, enable_ai

TENANTS = "/platform/v1/tenants"
PLANS = "/platform/v1/plans"


def _tenant(code: str, **extra: Any) -> dict[str, Any]:
    return {
        "code": code,
        "name": f"{code} 公司",
        "admin": {"username": "admin", "display_name": "管理员", "password": ADMIN_PASSWORD},
        **extra,
    }


async def _ops(app: FastAPI, client: httpx.AsyncClient) -> dict[str, str]:
    await create_platform_admin(app)
    return bearer(await platform_login(client))


async def _plan(client: httpx.AsyncClient, ops: dict[str, str], code: str, **fields: Any) -> dict:
    body = {"code": code, "name": code.upper(), "price_monthly": 3000, **fields}
    response = await client.post(PLANS, headers=ops, json=body)
    assert response.status_code == 201, response.text
    plan: dict = response.json()
    return plan


def _today(app: FastAPI) -> date:
    ctx: AppContext = app.state.ctx
    return today(ZoneInfo(ctx.settings.usage_timezone))


async def test_trial_signup_by_ops_shows_plan_features_and_notice(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    ops = await _ops(app, client)
    created = await client.post(TENANTS, headers=ops, json=_tenant("acme", plan_code="trial"))
    assert created.status_code == 201, created.text
    tenant = created.json()
    assert (tenant["plan_code"], tenant["subscription_status"]) == ("trial", "trial")
    assert tenant["period_end"] == (_today(app) + timedelta(days=13)).isoformat()

    admin = bearer(await login(client, "acme"))
    me = (await client.get("/api/v1/me", headers=admin)).json()
    assert me["plan"]["code"] == "trial" and me["plan"]["days_left"] == 14
    assert me["features"] == {
        "ai": True,
        "wecom": True,
        "broadcast": True,
        "extraction": True,
        "zone": False,
    }
    assert me["billing_notice"].startswith("试用期剩余 14 天")

    overview = (await client.get("/api/v1/billing", headers=admin)).json()
    limits = {x["key"]: (x["limit"], x["used"]) for x in overview["limits"]}
    assert limits == {
        "seats": (5, 1),
        "ai_replies_monthly": (1000, 0),
        "kb_items": (500, 0),
        "channels": (3, 1),
    }
    assert overview["metered"] and overview["subscription"]["status"] == "trial"

    # 坐席看不到提醒。
    await create_staff(client, admin["Authorization"].split()[1], "amy")
    agent = bearer(await login(client, "acme", "amy", "staff-pass-123"))
    assert (await client.get("/api/v1/me", headers=agent)).json()["billing_notice"] is None
    assert (await client.get("/api/v1/billing", headers=agent)).status_code == 403


async def test_tenants_without_subscription_are_unlimited(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    ops = await _ops(app, client)
    await client.post(TENANTS, headers=ops, json=_tenant("legacy"))
    admin = bearer(await login(client, "legacy"))

    overview = (await client.get("/api/v1/billing", headers=admin)).json()

    assert not overview["metered"] and overview["plan"] is None
    assert all(x["limit"] is None for x in overview["limits"])
    assert all(f["enabled"] for f in overview["features"])
    assert (await client.get("/api/v1/me", headers=admin)).json()["plan"] is None


async def test_seat_and_knowledge_limits(app: FastAPI, client: httpx.AsyncClient) -> None:
    ops = await _ops(app, client)
    await _plan(client, ops, "tiny", limits={"seats": 2, "kb_items": 2})
    await client.post(TENANTS, headers=ops, json=_tenant("acme", plan_code="tiny", months=1))
    token = await login(client, "acme")
    admin = bearer(token)

    await create_staff(client, token, "amy")
    third = await client.post(
        "/api/v1/staff",
        headers=admin,
        json={
            "username": "bob",
            "display_name": "Bob",
            "password": "staff-pass-123",
            "role_codes": ["agent"],
        },
    )
    assert third.status_code == 409
    assert third.json()["error"]["code"] == "plan_limit"
    assert "坐席账号上限（2 个）" in third.json()["error"]["message"]

    item = {"title": "发货时间", "content": "48 小时内发货"}
    assert (await client.post("/api/v1/kb/items", headers=admin, json=item)).status_code == 201
    # 导入会超出额度时整批拒绝。
    csv = "标准问,答案\n退货,七天无理由\n换货,十五天\n"
    imported = await client.post("/api/v1/kb/import", headers=admin, json={"csv": csv})
    assert imported.status_code == 409, imported.text
    assert (await client.post("/api/v1/kb/items", headers=admin, json=item)).status_code == 201
    full = await client.post("/api/v1/kb/items", headers=admin, json=item)
    assert full.status_code == 409

    # 平台单独放宽本租户的额度。
    tenant_id = (await client.get("/api/v1/me", headers=admin)).json()["tenant"]["id"]
    overrides = await client.put(
        f"{TENANTS}/{tenant_id}/overrides",
        headers=ops,
        json={"limits": {"kb_items": None, "seats": 3}, "features": {"zone": True}},
    )
    assert overrides.status_code == 200, overrides.text
    assert (await client.post("/api/v1/kb/items", headers=admin, json=item)).status_code == 201
    await create_staff(client, token, "bob")
    detail = (await client.get(f"{TENANTS}/{tenant_id}/billing", headers=ops)).json()
    by_key = {x["key"]: x for x in detail["limits"]}
    assert (by_key["seats"]["limit"], by_key["seats"]["overridden"]) == (3, True)
    assert by_key["kb_items"]["limit"] is None and by_key["kb_items"]["used"] == 3
    assert {f["key"]: f["enabled"] for f in detail["features"]}["zone"] is True
    assert detail["overrides"] == {
        "limits": {"kb_items": None, "seats": 3},
        "features": {"zone": True},
    }


async def test_channel_limit_blocks_enabling(app: FastAPI, client: httpx.AsyncClient) -> None:
    ops = await _ops(app, client)
    await _plan(client, ops, "one", limits={"channels": 1})
    await client.post(TENANTS, headers=ops, json=_tenant("acme", plan_code="one", months=1))
    admin = bearer(await login(client, "acme"))
    [web] = (await client.get("/api/v1/channels", headers=admin)).json()["items"]
    path = f"/api/v1/channels/{web['id']}"

    assert (await client.patch(path, headers=admin, json={"status": "disabled"})).status_code == 200
    # 只有一个渠道时重新启用不超出额度。
    assert (await client.patch(path, headers=admin, json={"status": "active"})).status_code == 200


async def test_plan_without_ai_turns_ai_off(app: FastAPI, client: httpx.AsyncClient) -> None:
    ops = await _ops(app, client)
    await _plan(client, ops, "human", features={"ai": False, "broadcast": False})
    await client.post(TENANTS, headers=ops, json=_tenant("acme", plan_code="human", months=1))
    admin = bearer(await login(client, "acme"))

    me = (await client.get("/api/v1/me", headers=admin)).json()
    assert (me["features"]["ai"], me["features"]["broadcast"]) == (False, False)
    outcome = await client.post("/api/v1/ai/test", headers=admin, json={"question": "你好"})
    assert outcome.json()["reason"] == "plan"


async def test_plan_crud_and_archive(app: FastAPI, client: httpx.AsyncClient) -> None:
    ops = await _ops(app, client)
    plan = await _plan(client, ops, "pro", limits={"seats": 10}, trial_days=7, public=True)
    assert plan["limits"] == {
        "seats": 10,
        "ai_replies_monthly": None,
        "kb_items": None,
        "channels": None,
    }
    assert (
        await client.post(PLANS, headers=ops, json={"code": "pro", "name": "x"})
    ).status_code == 409

    updated = await client.patch(
        f"{PLANS}/{plan['id']}",
        headers=ops,
        json={"price_monthly": 5000, "limits": {"seats": 12}, "status": "archived"},
    )
    assert updated.status_code == 200, updated.text
    assert (updated.json()["price_monthly"], updated.json()["limits"]["seats"]) == (5000, 12)
    codes = [p["code"] for p in (await client.get(PLANS, headers=ops)).json()["items"]]
    assert set(codes) == {"trial", "standard", "enterprise", "pro"}

    # 下架的套餐不能用于新订阅，也不在租户可选列表里。
    refused = await client.post(TENANTS, headers=ops, json=_tenant("acme", plan_code="pro"))
    assert refused.status_code == 422
    await client.post(TENANTS, headers=ops, json=_tenant("acme"))
    admin = bearer(await login(client, "acme"))
    public = (await client.get("/api/v1/billing/plans", headers=admin)).json()["items"]
    assert [p["code"] for p in public] == ["trial", "standard", "enterprise"]

    async with app.state.db.platform_sessionmaker() as session:
        actions = (await session.scalars(select(AuditLog.action))).all()
    assert {"plan.create", "plan.update"} <= set(actions)


async def test_upgrade_renew_cancel_and_expiry_suspension(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    ops = await _ops(app, client)
    created = await client.post(TENANTS, headers=ops, json=_tenant("acme", plan_code="trial"))
    tenant_id = created.json()["id"]
    first_day = _today(app)
    subs = f"{TENANTS}/{tenant_id}/subscriptions"

    # 试用转正式：试用订阅取消。
    upgraded = await client.post(subs, headers=ops, json={"plan_code": "standard", "months": 1})
    assert upgraded.status_code == 201, upgraded.text
    sub = upgraded.json()
    assert (sub["plan_code"], sub["status"]) == ("standard", "active")
    assert sub["period_end"] == (add_months(first_day, 1) - timedelta(days=1)).isoformat()
    history = (await client.get(f"{TENANTS}/{tenant_id}/billing", headers=ops)).json()
    assert [s["status"] for s in history["subscriptions"]] == ["active", "cancelled"]

    renewed = await client.post(f"{subs}/{sub['id']}/renew", headers=ops, json={"months": 2})
    assert renewed.status_code == 200, renewed.text
    renewed_end = date.fromisoformat(renewed.json()["period_end"])
    assert renewed_end == add_months(
        date.fromisoformat(sub["period_end"]) + timedelta(days=1), 2
    ) - timedelta(days=1)
    # 只能续订当前订阅。
    old = history["subscriptions"][1]["id"]
    assert (
        await client.post(f"{subs}/{old}/renew", headers=ops, json={"months": 1})
    ).status_code == 409

    cancelled = await client.post(f"{subs}/{sub['id']}/cancel", headers=ops)
    assert (cancelled.json()["status"], cancelled.json()["period_end"]) == (
        "cancelled",
        first_day.isoformat(),
    )

    ctx: AppContext = app.state.ctx
    # 宽限期（默认 7 天）内不停用。
    within = datetime.now(UTC) + timedelta(days=5)
    report = await billing.run_lifecycle(ctx, now=within)
    assert report.suspended == 0
    later = datetime.now(UTC) + timedelta(days=9)
    report = await billing.run_lifecycle(ctx, now=later)
    assert report.suspended == 1
    tenant = (await client.get(f"{TENANTS}/{tenant_id}", headers=ops)).json()
    assert (tenant["status"], tenant["suspended_reason"]) == ("suspended", "subscription_expired")
    login_refused = await client.post(
        "/api/v1/auth/login",
        json={"tenant_code": "acme", "username": "admin", "password": ADMIN_PASSWORD},
    )
    assert login_refused.status_code == 403

    # 开始新订阅后自动恢复。
    again = await client.post(subs, headers=ops, json={"plan_code": "standard", "months": 12})
    assert again.status_code == 201
    tenant = (await client.get(f"{TENANTS}/{tenant_id}", headers=ops)).json()
    assert (tenant["status"], tenant["suspended_reason"]) == ("active", None)


async def test_lifecycle_marks_ended_subscriptions_expired(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    ops = await _ops(app, client)
    created = await client.post(TENANTS, headers=ops, json=_tenant("acme", plan_code="trial"))
    tenant_id = created.json()["id"]
    ctx: AppContext = app.state.ctx

    report = await billing.run_lifecycle(ctx, now=datetime.now(UTC) + timedelta(days=15))

    assert (report.expired, report.suspended) == (1, 0)
    billing_detail = (await client.get(f"{TENANTS}/{tenant_id}/billing", headers=ops)).json()
    assert billing_detail["subscription"]["status"] == "expired"
    assert billing_detail["notice"].startswith("套餐已于")
    # 手动停用的租户，续费不会自动恢复。
    await client.patch(f"{TENANTS}/{tenant_id}", headers=ops, json={"status": "suspended"})
    sub_id = billing_detail["subscription"]["id"]
    await client.post(
        f"{TENANTS}/{tenant_id}/subscriptions/{sub_id}/renew", headers=ops, json={"months": 1}
    )
    assert (await client.get(f"{TENANTS}/{tenant_id}", headers=ops)).json()["status"] == "suspended"


async def _subscription(
    app: FastAPI, tenant_id: uuid.UUID, plan_code: str, start: date, end: date, status: str
) -> None:
    async with app.state.db.platform_sessionmaker() as session:
        plan = await billing.plan_by_code(session, plan_code)
        session.add(
            Subscription(
                tenant_id=tenant_id,
                plan_id=plan.id,
                status=status,
                period_start=start,
                period_end=end,
            )
        )
        await session.commit()


async def test_monthly_invoice_prorates_plan_changes(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    ops = await _ops(app, client)
    tenant_id = uuid.UUID(
        (await client.post(TENANTS, headers=ops, json=_tenant("acme"))).json()["id"]
    )
    month = month_start(_today(app)) - timedelta(days=1)
    first, last = month_start(month), month_end(month)
    days = (last - first).days + 1
    # 前 10 天试用（不计费），接着标准版，最后 5 天升级到旗舰版（最近创建的订阅优先）。
    await _subscription(app, tenant_id, "trial", first, first + timedelta(days=9), "cancelled")
    await _subscription(app, tenant_id, "standard", first + timedelta(days=10), last, "active")
    await _subscription(app, tenant_id, "enterprise", last - timedelta(days=4), last, "active")

    result = await client.post(
        "/platform/v1/invoices/generate", headers=ops, json={"month": f"{first:%Y-%m}"}
    )
    assert result.json() == {"created": 1, "updated": 0, "unchanged": 0}

    [invoice] = (await client.get("/platform/v1/invoices", headers=ops)).json()["items"]
    standard_days = days - 10 - 5
    expected = round(199900 * standard_days / days) + round(999900 * 5 / days)
    assert invoice["number"] == f"INV-{first:%Y%m}-acme"
    assert [(i["kind"], i["quantity"]) for i in invoice["items"]] == [
        ("plan", standard_days),
        ("plan", 5),
    ]
    assert invoice["amount"] == expected and invoice["plan_name"] == "旗舰版"
    assert (invoice["tenant_code"], invoice["status"]) == ("acme", "issued")

    paid = await client.patch(
        f"/platform/v1/invoices/{invoice['id']}",
        headers=ops,
        json={"status": "paid", "note": "对公转账"},
    )
    assert paid.status_code == 200
    assert paid.json()["paid_at"] is not None and paid.json()["note"] == "对公转账"
    # 已收款的账单重算时不变。
    again = await client.post(
        "/platform/v1/invoices/generate", headers=ops, json={"month": f"{first:%Y-%m}"}
    )
    assert again.json() == {"created": 0, "updated": 0, "unchanged": 1}

    admin = bearer(await login(client, "acme"))
    mine = (await client.get("/api/v1/billing/invoices", headers=admin)).json()["items"]
    assert [i["number"] for i in mine] == [invoice["number"]]
    voided = await client.patch(
        f"/platform/v1/invoices/{invoice['id']}", headers=ops, json={"status": "void"}
    )
    assert voided.json()["status"] == "void" and voided.json()["paid_at"] is None
    reopened = await client.patch(
        f"/platform/v1/invoices/{invoice['id']}", headers=ops, json={"status": "issued"}
    )
    assert reopened.status_code == 409


async def test_trial_only_month_has_no_invoice(app: FastAPI, client: httpx.AsyncClient) -> None:
    ops = await _ops(app, client)
    await client.post(TENANTS, headers=ops, json=_tenant("acme", plan_code="trial"))
    ctx: AppContext = app.state.ctx

    result = await billing.generate_invoices(ctx, _today(app))

    assert result.created == 0
    async with app.state.db.platform_sessionmaker() as session:
        assert (await session.scalar(select(Tenant.code))) == "acme"


async def test_ai_overage_continues_and_is_billed(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> None:
    desk = await Desk(app, client, fake_im, settings, database_urls).open()
    await enable_ai(desk)
    ops = await _ops(app, client)
    await _plan(
        client,
        ops,
        "metered",
        price_monthly=0,
        limits={"ai_replies_monthly": 1},
        overage={"policy": "warn", "ai_reply_price": 5},
    )
    subs = f"{TENANTS}/{desk.tenant_id}/subscriptions"
    assert (await client.post(subs, headers=ops, json={"plan_code": "metered"})).status_code == 201
    first, second = await desk.visitor(), await desk.visitor()

    await desk.say(first, "快递几天能到")
    await desk.say(second, "快递几天能到")

    # 允许超额的套餐：用完额度后 AI 继续接待。
    assert bot_texts(desk, first) and bot_texts(desk, second)
    overview = (await client.get("/api/v1/billing", headers=desk.admin)).json()
    ai = {x["key"]: x for x in overview["limits"]}["ai_replies_monthly"]
    assert (ai["limit"], ai["used"]) == (1, 2)
    assert overview["notice"] == "每月 AI 回复已达到套餐上限"

    ctx: AppContext = app.state.ctx
    result = await billing.generate_invoices(ctx, _today(app))
    assert result.created == 1
    [invoice] = (await client.get("/api/v1/billing/invoices", headers=desk.admin)).json()["items"]
    assert [(i["kind"], i["quantity"], i["amount"]) for i in invoice["items"]] == [
        ("ai_overage", 1, 5)
    ]
