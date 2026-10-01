import re

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.db.session import Database
from app.modules.audit.models import AuditLog
from tests.factories import (
    ADMIN_PASSWORD,
    PLATFORM_PASSWORD,
    bearer,
    create_platform_admin,
    login,
    platform_login,
)

TENANTS = "/platform/v1/tenants"


def _tenant_payload(code: str) -> dict[str, object]:
    return {
        "code": code,
        "name": f"{code} 公司",
        "admin": {"username": "admin", "display_name": "管理员", "password": ADMIN_PASSWORD},
    }


async def test_platform_login_and_me(app: FastAPI, client: httpx.AsyncClient) -> None:
    await create_platform_admin(app)
    token = await platform_login(client)

    response = await client.get("/platform/v1/me", headers=bearer(token))

    assert response.status_code == 200
    assert response.json()["username"] == "ops"


async def test_platform_login_rejects_wrong_password(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await create_platform_admin(app)
    response = await client.post(
        "/platform/v1/auth/login", json={"username": "ops", "password": "wrong"}
    )
    assert response.status_code == 401


async def test_platform_session_is_restored_from_the_refresh_cookie(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """登录写入 httpOnly 的刷新令牌 Cookie；刷新换取新令牌但不延长登录；退出后作废。"""
    await create_platform_admin(app)
    assert (await client.post("/platform/v1/auth/refresh")).status_code == 401

    login = await client.post(
        "/platform/v1/auth/login", json={"username": "ops", "password": PLATFORM_PASSWORD}
    )
    assert login.status_code == 200, login.text
    cookie = login.headers["set-cookie"]
    assert "edp_platform_refresh=" in cookie and "HttpOnly" in cookie
    assert "Path=/platform/v1/auth" in cookie and "samesite=strict" in cookie.lower()

    refreshed = await client.post("/platform/v1/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    token = refreshed.json()["access_token"]
    me = await client.get("/platform/v1/me", headers=bearer(token))
    assert me.status_code == 200 and me.json()["username"] == "ops"
    # 新 Cookie 的有效期不超过登录时的（登录后最多 12 小时要重新登录）。
    match = re.search(r"Max-Age=(\d+)", refreshed.headers["set-cookie"])
    assert match is not None and 12 * 3600 - 60 < int(match.group(1)) <= 12 * 3600

    assert (await client.post("/platform/v1/auth/logout")).status_code == 204
    assert (await client.post("/platform/v1/auth/refresh")).status_code == 401
    assert (await client.get("/platform/v1/me", headers=bearer(token))).status_code == 200


async def test_platform_endpoints_require_platform_login(client: httpx.AsyncClient) -> None:
    assert (await client.get(TENANTS)).status_code == 401


async def test_provisioned_tenant_admin_can_log_in_and_sees_system_roles(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await create_platform_admin(app)
    token = await platform_login(client)

    created = await client.post(TENANTS, headers=bearer(token), json=_tenant_payload("acme"))
    assert created.status_code == 201
    assert created.json()["status"] == "active"

    admin_token = await login(client, "acme")
    roles = await client.get("/api/v1/roles", headers=bearer(admin_token))
    assert roles.status_code == 200
    assert {r["code"] for r in roles.json()["items"]} == {
        "tenant_admin",
        "agent",
        "supervisor",
        "knowledge_manager",
        "worker",
        "keeper",
    }


async def test_duplicate_tenant_code_conflicts(app: FastAPI, client: httpx.AsyncClient) -> None:
    await create_platform_admin(app)
    token = await platform_login(client)
    await client.post(TENANTS, headers=bearer(token), json=_tenant_payload("acme"))

    response = await client.post(TENANTS, headers=bearer(token), json=_tenant_payload("acme"))

    assert response.status_code == 409


async def test_invalid_tenant_code_is_rejected(app: FastAPI, client: httpx.AsyncClient) -> None:
    await create_platform_admin(app)
    token = await platform_login(client)

    response = await client.post(TENANTS, headers=bearer(token), json=_tenant_payload("A!"))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_list_and_suspend_tenant(app: FastAPI, client: httpx.AsyncClient) -> None:
    await create_platform_admin(app)
    token = await platform_login(client)
    tenant_id = (
        await client.post(TENANTS, headers=bearer(token), json=_tenant_payload("acme"))
    ).json()["id"]

    listed = await client.get(TENANTS, headers=bearer(token))
    assert [t["code"] for t in listed.json()["items"]] == ["acme"]

    suspended = await client.patch(
        f"{TENANTS}/{tenant_id}", headers=bearer(token), json={"status": "suspended"}
    )
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"

    login_attempt = await client.post(
        "/api/v1/auth/login",
        json={"tenant_code": "acme", "username": "admin", "password": ADMIN_PASSWORD},
    )
    assert login_attempt.status_code == 403


async def test_tenant_provisioning_is_audited(app: FastAPI, client: httpx.AsyncClient) -> None:
    await create_platform_admin(app)
    token = await platform_login(client)
    await client.post(TENANTS, headers=bearer(token), json=_tenant_payload("acme"))

    db: Database = app.state.db
    async with db.platform_sessionmaker() as session:
        actions = (await session.scalars(select(AuditLog.action))).all()
    assert actions == ["tenant.provision"]


async def test_ai_quota_can_be_set_and_cleared(app: FastAPI, client: httpx.AsyncClient) -> None:
    await create_platform_admin(app)
    token = await platform_login(client)
    created = await client.post(TENANTS, headers=bearer(token), json=_tenant_payload("acme"))
    tenant_id = created.json()["id"]
    assert created.json()["ai_monthly_quota"] is None

    limited = await client.patch(
        f"{TENANTS}/{tenant_id}", headers=bearer(token), json={"ai_monthly_quota": 5000}
    )
    renamed = await client.patch(
        f"{TENANTS}/{tenant_id}", headers=bearer(token), json={"name": "新名称"}
    )
    cleared = await client.patch(
        f"{TENANTS}/{tenant_id}", headers=bearer(token), json={"ai_monthly_quota": None}
    )

    assert limited.json()["ai_monthly_quota"] == 5000
    # 只改名称时额度不变；显式传 null 表示不限。
    assert (renamed.json()["name"], renamed.json()["ai_monthly_quota"]) == ("新名称", 5000)
    assert cleared.json()["ai_monthly_quota"] is None
    negative = await client.patch(
        f"{TENANTS}/{tenant_id}", headers=bearer(token), json={"ai_monthly_quota": -1}
    )
    assert negative.status_code == 422
