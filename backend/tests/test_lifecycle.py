"""租户生命周期（G2）：自助注册、注销与保留期、数据导出、删除数据与删除记录、授权平台运维访问。"""

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from app.context import AppContext
from app.core.config import Settings
from app.modules.lifecycle.closure import deletion_digest, run_purges
from app.modules.lifecycle.export import run_exports
from app.modules.tenancy.models import Tenant
from tests.desk import Desk
from tests.factories import (
    ADMIN_PASSWORD,
    STAFF_PASSWORD,
    bearer,
    create_platform_admin,
    create_staff,
    login,
    platform_login,
    provision,
)
from tests.fake_openim import FakeOpenIM
from tests.fake_storage import FakeStorage
from tests.support import DatabaseUrls
from tests.test_visitor import channel_key, init

SIGNUP = {
    "company_name": "示例科技",
    "tenant_code": "demo-co",
    "admin_display_name": "张三",
    "password": "signup-pass-1",
    "contact": "13800000000",
    "agree": True,
}


async def _ops(app: FastAPI, client: httpx.AsyncClient) -> dict[str, str]:
    await create_platform_admin(app)
    return bearer(await platform_login(client))


async def _fetch(database_urls: DatabaseUrls, query: str, *args: Any) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(database_urls.platform_dsn)
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


# ---- 自助注册 ----


async def test_self_signup_starts_a_trial(app: FastAPI, client: httpx.AsyncClient) -> None:
    options = (await client.get("/api/v1/signup")).json()
    assert options == {"enabled": True, "plan_name": "试用版", "trial_days": 14}

    created = await client.post("/api/v1/signup", json=SIGNUP)

    assert created.status_code == 201, created.text
    assert created.json() == {
        "tenant_code": "demo-co",
        "username": "admin",
        "plan_name": "试用版",
        "trial_days": 14,
    }
    admin = bearer(await login(client, "demo-co", "admin", "signup-pass-1"))
    me = (await client.get("/api/v1/me", headers=admin)).json()
    assert (me["tenant"]["name"], me["display_name"]) == ("示例科技", "张三")
    assert (me["plan"]["code"], me["plan"]["status"]) == ("trial", "trial")
    ops = await _ops(app, client)
    [tenant] = (await client.get("/platform/v1/tenants", headers=ops)).json()["items"]
    assert (tenant["code"], tenant["plan_name"]) == ("demo-co", "试用版")
    async with app.state.db.platform_sessionmaker() as session:
        row = await session.get(Tenant, tenant["id"])
        assert row is not None and row.settings["contact"] == "13800000000"

    again = await client.post("/api/v1/signup", json=SIGNUP)
    assert again.status_code == 409


async def test_signup_requires_consent_and_can_be_closed(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    refused = await client.post("/api/v1/signup", json={**SIGNUP, "agree": False})
    assert refused.status_code == 422

    ops = await _ops(app, client)
    policy = (await client.get("/platform/v1/settings/tenant-policy", headers=ops)).json()
    assert policy == {
        "signup_enabled": True,
        "signup_plan_code": "trial",
        "grace_days": 7,
        "retention_days": 30,
        "export_ttl_days": 7,
    }
    put = await client.put(
        "/platform/v1/settings/tenant-policy",
        headers=ops,
        json={**policy, "signup_enabled": False},
    )
    assert put.status_code == 200
    assert (await client.get("/api/v1/signup")).json()["enabled"] is False
    closed = await client.post("/api/v1/signup", json=SIGNUP)
    assert closed.status_code == 403


async def test_signup_is_rate_limited_per_ip(client: httpx.AsyncClient) -> None:
    codes = [f"demo-{i}" for i in range(6)]
    statuses = [
        (await client.post("/api/v1/signup", json={**SIGNUP, "tenant_code": code})).status_code
        for code in codes
    ]
    assert statuses == [201] * 5 + [429]


# ---- 注销与导出 ----


async def test_closure_needs_password_and_code_then_stops_services(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await provision(app, "acme")
    token = await login(client, "acme")
    admin = bearer(token)
    key = await channel_key(client, "acme")

    wrong = await client.post(
        "/api/v1/tenant/closure", headers=admin, json={"password": "x", "confirm_code": "acme"}
    )
    assert (wrong.status_code, wrong.json()["error"]["message"]) == (422, "密码不正确")
    wrong_code = await client.post(
        "/api/v1/tenant/closure",
        headers=admin,
        json={"password": ADMIN_PASSWORD, "confirm_code": "other"},
    )
    assert wrong_code.json()["error"]["message"] == "企业代码不正确"

    before = datetime.now(UTC)
    closing = await client.post(
        "/api/v1/tenant/closure",
        headers=admin,
        json={"password": ADMIN_PASSWORD, "confirm_code": "ACME", "reason": "业务调整"},
    )
    assert closing.status_code == 200, closing.text
    status = closing.json()
    assert status["closing"] and status["retention_days"] == 30
    scheduled = datetime.fromisoformat(status["scheduled_at"])
    assert timedelta(days=30) <= scheduled - before < timedelta(days=30, minutes=1)
    # 自动排队导出一次；新访客不能发起咨询；功能停止；员工仍可登录。
    exports = (await client.get("/api/v1/tenant/exports", headers=admin)).json()["items"]
    assert [e["status"] for e in exports] == ["pending"]
    assert (await init(client, key)).status_code == 403
    me = (await client.get("/api/v1/me", headers=admin)).json()
    assert not any(me["features"].values())
    again = await client.post(
        "/api/v1/tenant/closure",
        headers=admin,
        json={"password": ADMIN_PASSWORD, "confirm_code": "acme"},
    )
    assert again.status_code == 409

    cancelled = await client.delete("/api/v1/tenant/closure", headers=admin)
    assert cancelled.json()["closing"] is False and cancelled.json()["scheduled_at"] is None
    assert (await init(client, key)).status_code == 200
    assert all((await client.get("/api/v1/me", headers=admin)).json()["features"].values())

    # 坐席不能注销或导出。
    await create_staff(client, token, "amy")
    agent = bearer(await login(client, "acme", "amy", STAFF_PASSWORD))
    assert (await client.get("/api/v1/tenant/closure", headers=agent)).status_code == 403
    assert (await client.post("/api/v1/tenant/exports", headers=agent)).status_code == 403


async def test_export_contains_tables_without_secrets_and_expires(
    app: FastAPI, client: httpx.AsyncClient, fake_storage: FakeStorage
) -> None:
    await provision(app, "acme")
    await provision(app, "other")
    admin = bearer(await login(client, "acme"))
    fake_storage.objects["/edp-files/acme/2026/09/abc/a.png"] = (b"PNG", "image/png")
    fake_storage.objects["/edp-files/other/2026/09/abc/b.png"] = (b"OTHER", "image/png")

    requested = await client.post("/api/v1/tenant/exports", headers=admin)
    assert requested.status_code == 201
    busy = await client.post("/api/v1/tenant/exports", headers=admin)
    assert busy.status_code == 409
    not_ready = await client.get(
        f"/api/v1/tenant/exports/{requested.json()['id']}/download", headers=admin
    )
    assert not_ready.status_code == 404

    ctx: AppContext = app.state.ctx
    assert await run_exports(ctx) == 1

    [done] = (await client.get("/api/v1/tenant/exports", headers=admin)).json()["items"]
    assert done["status"] == "done", done["error"]
    assert done["tables"]["staff"] == 1 and done["tables"]["files"] == 1
    assert "refresh_tokens" not in done["tables"]
    [key] = [k for k in fake_storage.object_keys() if k.startswith("acme/_exports/")]
    archive = zipfile.ZipFile(io.BytesIO(fake_storage.get(key)))
    names = set(archive.namelist())
    assert {"manifest.json", "tables/staff.jsonl", "files/2026/09/abc/a.png"} <= names
    assert not any("b.png" in n for n in names)
    [staff] = [json.loads(line) for line in archive.read("tables/staff.jsonl").splitlines()]
    assert staff["username"] == "admin" and "password_hash" not in staff
    [channel] = [
        json.loads(line) for line in archive.read("tables/channel_accounts.jsonl").splitlines()
    ]
    assert "identity_secret" not in (channel.get("config") or {})
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["tenant"]["code"] == "acme" and manifest["tables"]["staff"] == 1

    link = await client.get(f"/api/v1/tenant/exports/{done['id']}/download", headers=admin)
    assert link.status_code == 200
    assert key in link.json()["url"] and "X-Amz-Signature" in link.json()["url"]

    # 过了保留天数后删除文件。
    await run_exports(ctx, now=datetime.now(UTC) + timedelta(days=8))
    assert key not in fake_storage.object_keys()
    gone = await client.get(f"/api/v1/tenant/exports/{done['id']}/download", headers=admin)
    assert gone.status_code == 404


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def test_purge_deletes_tenant_data_and_leaves_a_record(
    desk: Desk,
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_storage: FakeStorage,
    database_urls: DatabaseUrls,
) -> None:
    ops = await _ops(app, client)
    subs = f"/platform/v1/tenants/{desk.tenant_id}/subscriptions"
    assert (await client.post(subs, headers=ops, json={"plan_code": "standard"})).status_code == 201
    other = await provision(app, "other")
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    await desk.reply(alice, visitor, "您好")
    fake_storage.objects["/edp-files/acme/2026/09/abc/a.png"] = (b"PNG", "image/png")
    fake_storage.objects["/edp-files/other/2026/09/abc/b.png"] = (b"OTHER", "image/png")
    assert visitor.group_id in desk.im.groups

    # 没有申请注销时不能删除。
    not_closing = await client.post(f"/platform/v1/tenants/{desk.tenant_id}/purge", headers=ops)
    assert not_closing.status_code == 409
    closing = await client.post(
        f"/platform/v1/tenants/{desk.tenant_id}/closure", headers=ops, json={"reason": "合同终止"}
    )
    assert closing.status_code == 200 and closing.json()["closing"]
    await run_exports(app.state.ctx)

    purged = await client.post(f"/platform/v1/tenants/{desk.tenant_id}/purge", headers=ops)

    assert purged.status_code == 200, purged.text
    deletion = purged.json()["deletion"]
    counts = deletion["counts"]
    assert counts["tables"]["staff"] == 2 and counts["tables"]["messages"] >= 2
    assert counts["tables"]["customers"] == 1
    assert "subscriptions" not in counts["tables"]
    assert counts["objects"] == 2  # 聊天文件和导出文件
    assert counts["im_groups"] == 1 and visitor.group_id not in desk.im.groups
    assert deletion["export_id"] is not None
    for table in ("staff", "customers", "messages", "sessions", "rooms", "channel_accounts"):
        rows = await _fetch(
            database_urls, f"SELECT count(*) AS n FROM {table} WHERE tenant_id = $1", desk.tenant_id
        )
        assert rows[0]["n"] == 0, table
    kept = await _fetch(
        database_urls,
        "SELECT count(*) AS n FROM subscriptions WHERE tenant_id = $1",
        desk.tenant_id,
    )
    assert kept[0]["n"] == 1
    platform_audit = await _fetch(
        database_urls,
        "SELECT action FROM audit_logs WHERE tenant_id = $1 ORDER BY created_at",
        desk.tenant_id,
    )
    assert {r["action"] for r in platform_audit} >= {"tenant.closure_request", "tenant.purge"}
    assert "auth.login" not in {r["action"] for r in platform_audit}
    # 其他租户不受影响。
    assert fake_storage.object_keys() == ["other/2026/09/abc/b.png"]
    other_staff = await _fetch(
        database_urls, "SELECT count(*) AS n FROM staff WHERE tenant_id = $1", other
    )
    assert other_staff[0]["n"] == 1

    tenant = (await client.get(f"/platform/v1/tenants/{desk.tenant_id}", headers=ops)).json()
    assert tenant["status"] == "closed" and tenant["purged_at"] is not None
    refused = await client.post(
        "/api/v1/auth/login",
        json={"tenant_code": "acme", "username": "admin", "password": ADMIN_PASSWORD},
    )
    assert refused.status_code == 401
    [record] = (await client.get("/platform/v1/deletions", headers=ops)).json()["items"]
    assert record["digest"] == deletion["digest"] and record["code"] == "acme"
    async with app.state.db.platform_sessionmaker() as session:
        row = await session.get(Tenant, desk.tenant_id)
        assert row is not None
        purged_at = datetime.fromisoformat(record["purged_at"])
        assert deletion_digest(row, purged_at, record["counts"]) == record["digest"]
    again = await client.post(f"/platform/v1/tenants/{desk.tenant_id}/purge", headers=ops)
    assert again.status_code == 409
    cancel = await client.delete(f"/platform/v1/tenants/{desk.tenant_id}/closure", headers=ops)
    assert cancel.status_code == 409


async def test_scheduled_purge_runs_after_retention(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await provision(app, "acme")
    admin = bearer(await login(client, "acme"))
    await client.post(
        "/api/v1/tenant/closure",
        headers=admin,
        json={"password": ADMIN_PASSWORD, "confirm_code": "acme"},
    )
    ctx: AppContext = app.state.ctx

    assert await run_purges(ctx) == 0
    assert await run_purges(ctx, now=datetime.now(UTC) + timedelta(days=31)) == 1

    ops = await _ops(app, client)
    [tenant] = (await client.get("/platform/v1/tenants", headers=ops)).json()["items"]
    assert tenant["status"] == "closed"


# ---- 授权平台运维访问 ----


async def test_support_access_needs_a_grant_and_is_audited(
    desk: Desk, app: FastAPI, client: httpx.AsyncClient
) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "发票怎么开")
    await desk.reply(alice, visitor, "请在订单页申请")
    ops = await _ops(app, client)
    base = f"/platform/v1/tenants/{desk.tenant_id}/support"

    assert (await client.get(base, headers=ops)).json() == {"active": False, "grant": None}
    assert (await client.get(f"{base}/sessions", headers=ops)).status_code == 403
    assert (
        await client.get("/api/v1/tenant/support-grants", headers=alice.headers)
    ).status_code == 403

    granted = await client.post(
        "/api/v1/tenant/support-grants",
        headers=desk.admin,
        json={"reason": "排查消息延迟", "hours": 2},
    )
    assert granted.status_code == 201, granted.text
    assert granted.json()["active"] is True

    status = (await client.get(base, headers=ops)).json()
    assert status["active"] and status["grant"]["reason"] == "排查消息延迟"
    [chat] = (await client.get(f"{base}/sessions", headers=ops)).json()["items"]
    assert chat["assignee_name"] == "Alice"
    messages = (await client.get(f"{base}/sessions/{chat['id']}/messages", headers=ops)).json()
    texts = [m["text"] for m in messages["items"]]
    assert "发票怎么开" in texts and "请在订单页申请" in texts

    listing = (await client.get("/api/v1/tenant/support-grants", headers=desk.admin)).json()
    assert [a["what"] for a in listing["accesses"]] == ["messages", "sessions"]
    assert listing["accesses"][0]["resource_id"] == chat["id"]

    revoked = await client.delete(
        f"/api/v1/tenant/support-grants/{granted.json()['id']}", headers=desk.admin
    )
    assert revoked.json()["active"] is False
    assert (await client.get(f"{base}/sessions", headers=ops)).status_code == 403
