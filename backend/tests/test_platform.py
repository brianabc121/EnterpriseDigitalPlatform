import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.db.session import Database
from app.modules.audit.models import AuditLog
from tests.factories import (
    ADMIN_PASSWORD,
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
