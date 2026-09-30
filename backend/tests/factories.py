from uuid import UUID

import httpx
from fastapi import FastAPI

from app.db.session import Database
from app.modules.tenancy import service as tenancy
from app.modules.tenancy.schemas import TenantAdminCreate, TenantCreate

ADMIN_PASSWORD = "admin-pass-123"
STAFF_PASSWORD = "staff-pass-123"
PLATFORM_PASSWORD = "ops-pass-123"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def provision(app: FastAPI, code: str) -> UUID:
    db: Database = app.state.db
    async with db.platform_sessionmaker() as session:
        tenant = await tenancy.provision_tenant(
            session,
            TenantCreate(
                code=code,
                name=code.upper(),
                admin=TenantAdminCreate(
                    username="admin", display_name="管理员", password=ADMIN_PASSWORD
                ),
            ),
            actor_id=None,
            ip=None,
        )
    return tenant.id


async def login(
    client: httpx.AsyncClient,
    tenant_code: str,
    username: str = "admin",
    password: str = ADMIN_PASSWORD,
) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant_code": tenant_code, "username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


async def create_staff(
    client: httpx.AsyncClient, admin_token: str, username: str, roles: list[str] | None = None
) -> str:
    response = await client.post(
        "/api/v1/staff",
        headers=bearer(admin_token),
        json={
            "username": username,
            "display_name": username.capitalize(),
            "password": STAFF_PASSWORD,
            "role_codes": roles or ["agent"],
        },
    )
    assert response.status_code == 201, response.text
    staff_id: str = response.json()["id"]
    return staff_id


async def create_platform_admin(app: FastAPI, username: str = "ops") -> None:
    db: Database = app.state.db
    async with db.platform_sessionmaker() as session:
        await tenancy.create_platform_user(
            session, username=username, display_name="运营", password=PLATFORM_PASSWORD
        )


async def platform_login(client: httpx.AsyncClient, username: str = "ops") -> str:
    response = await client.post(
        "/platform/v1/auth/login", json={"username": username, "password": PLATFORM_PASSWORD}
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token
