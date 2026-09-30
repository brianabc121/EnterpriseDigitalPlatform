from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update

from app.core.permissions import ALL_PERMISSIONS
from app.db.session import Database
from app.modules.iam import service as iam_service
from app.modules.iam.models import Staff
from app.modules.tenancy.models import Tenant
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

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"


async def _login_response(
    client: httpx.AsyncClient, tenant: str, username: str, password: str
) -> httpx.Response:
    return await client.post(
        LOGIN, json={"tenant_code": tenant, "username": username, "password": password}
    )


async def _refresh_with(client: httpx.AsyncClient, refresh_token: str) -> httpx.Response:
    client.cookies.clear()
    return await client.post(REFRESH, headers={"Cookie": f"edp_refresh={refresh_token}"})


async def test_login_returns_access_token_and_secure_refresh_cookie(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await provision(app, "acme")
    response = await _login_response(client, "acme", "admin", ADMIN_PASSWORD)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    cookie = response.headers["set-cookie"]
    assert "edp_refresh=" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/api/v1/auth" in cookie
    assert "SameSite=strict" in cookie


async def test_tenant_code_is_case_insensitive(app: FastAPI, client: httpx.AsyncClient) -> None:
    await provision(app, "acme")
    response = await _login_response(client, "ACME", "admin", ADMIN_PASSWORD)
    assert response.status_code == 200


async def test_me_returns_profile_roles_and_permissions(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await provision(app, "acme")
    token = await login(client, "acme")

    response = await client.get("/api/v1/me", headers=bearer(token))

    assert response.status_code == 200
    me = response.json()
    assert me["username"] == "admin"
    assert me["tenant"]["code"] == "acme"
    assert me["roles"] == ["tenant_admin"]
    assert set(me["permissions"]) == set(ALL_PERMISSIONS)


@pytest.mark.parametrize(
    ("tenant", "username", "password"),
    [
        ("acme", "admin", "wrong-password"),
        ("acme", "nobody", ADMIN_PASSWORD),
        ("nope", "admin", ADMIN_PASSWORD),
    ],
)
async def test_bad_credentials_get_the_same_generic_error(
    app: FastAPI, client: httpx.AsyncClient, tenant: str, username: str, password: str
) -> None:
    await provision(app, "acme")
    response = await _login_response(client, tenant, username, password)

    assert response.status_code == 401
    assert response.json()["error"]["message"] == iam_service.INVALID_CREDENTIALS


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-jwt"}])
async def test_protected_endpoint_requires_valid_token(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get("/api/v1/me", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_refresh_rotates_the_token_pair(app: FastAPI, client: httpx.AsyncClient) -> None:
    await provision(app, "acme")
    first = await _login_response(client, "acme", "admin", ADMIN_PASSWORD)
    refresh_1 = first.cookies["edp_refresh"]

    response = await _refresh_with(client, refresh_1)

    assert response.status_code == 200
    refresh_2 = response.cookies["edp_refresh"]
    assert refresh_2 != refresh_1
    me = await client.get("/api/v1/me", headers=bearer(response.json()["access_token"]))
    assert me.status_code == 200


async def test_reusing_a_rotated_refresh_token_revokes_the_whole_session(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(iam_service, "REFRESH_REUSE_GRACE", timedelta(0))
    await provision(app, "acme")
    refresh_1 = (await _login_response(client, "acme", "admin", ADMIN_PASSWORD)).cookies[
        "edp_refresh"
    ]
    refresh_2 = (await _refresh_with(client, refresh_1)).cookies["edp_refresh"]

    reused = await _refresh_with(client, refresh_1)
    assert reused.status_code == 401

    # 盗用检测会吊销整组令牌：连最新的刷新令牌也失效。
    assert (await _refresh_with(client, refresh_2)).status_code == 401


async def test_concurrent_refresh_within_grace_period_is_allowed(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await provision(app, "acme")
    refresh_1 = (await _login_response(client, "acme", "admin", ADMIN_PASSWORD)).cookies[
        "edp_refresh"
    ]
    assert (await _refresh_with(client, refresh_1)).status_code == 200
    assert (await _refresh_with(client, refresh_1)).status_code == 200


async def test_logout_revokes_the_refresh_token_and_clears_cookie(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await provision(app, "acme")
    refresh_token = (await _login_response(client, "acme", "admin", ADMIN_PASSWORD)).cookies[
        "edp_refresh"
    ]

    client.cookies.clear()
    response = await client.post(
        "/api/v1/auth/logout", headers={"Cookie": f"edp_refresh={refresh_token}"}
    )

    assert response.status_code == 204
    assert 'edp_refresh=""' in response.headers["set-cookie"]
    assert (await _refresh_with(client, refresh_token)).status_code == 401


async def test_refresh_without_cookie_is_unauthorized(client: httpx.AsyncClient) -> None:
    assert (await client.post(REFRESH)).status_code == 401


async def test_suspending_a_tenant_blocks_login_and_existing_tokens(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    tenant_id = await provision(app, "acme")
    token = await login(client, "acme")

    db: Database = app.state.db
    async with db.platform_sessionmaker() as session:
        await session.execute(
            update(Tenant).where(Tenant.id == tenant_id).values(status="suspended")
        )
        await session.commit()

    assert (await client.get("/api/v1/me", headers=bearer(token))).status_code == 401
    assert (await _login_response(client, "acme", "admin", ADMIN_PASSWORD)).status_code == 403


async def test_disabled_staff_cannot_login_or_use_tokens(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await provision(app, "acme")
    admin_token = await login(client, "acme")
    await create_staff(client, admin_token, "alice")
    alice_token = await login(client, "acme", "alice", STAFF_PASSWORD)

    db: Database = app.state.db
    async with db.platform_sessionmaker() as session:
        await session.execute(
            update(Staff).where(Staff.username == "alice").values(status="disabled")
        )
        await session.commit()

    assert (await client.get("/api/v1/me", headers=bearer(alice_token))).status_code == 401
    assert (await _login_response(client, "acme", "alice", STAFF_PASSWORD)).status_code == 403


async def test_tenant_and_platform_tokens_are_not_interchangeable(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await provision(app, "acme")
    await create_platform_admin(app)
    staff_token = await login(client, "acme")
    platform_token = await platform_login(client)

    assert (await client.get("/api/v1/me", headers=bearer(platform_token))).status_code == 401
    platform_as_staff = await client.get("/platform/v1/tenants", headers=bearer(staff_token))
    assert platform_as_staff.status_code == 401
