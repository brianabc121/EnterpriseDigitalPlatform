import uuid

import asyncpg
import httpx
from fastapi import FastAPI

from app.core.permissions import DEFAULT_ROLES, Permission
from app.db.session import Database
from app.modules.iam.models import Role, StaffRole
from tests.factories import STAFF_PASSWORD, bearer, create_staff, login, provision
from tests.support import DatabaseUrls

STAFF = "/api/v1/staff"
AGENT_PERMISSIONS = next(r.permissions for r in DEFAULT_ROLES if r.code == "agent")


def _payload(username: str, roles: list[str]) -> dict[str, object]:
    return {
        "username": username,
        "display_name": username,
        "password": STAFF_PASSWORD,
        "role_codes": roles,
    }


async def test_admin_creates_agent_who_gets_agent_permissions(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await provision(app, "acme")
    admin = await login(client, "acme")
    await create_staff(client, admin, "alice")

    alice = await login(client, "acme", "alice", STAFF_PASSWORD)
    me = (await client.get("/api/v1/me", headers=bearer(alice))).json()

    assert me["roles"] == ["agent"]
    assert set(me["permissions"]) == set(AGENT_PERMISSIONS)


async def test_agent_cannot_create_staff(app: FastAPI, client: httpx.AsyncClient) -> None:
    await provision(app, "acme")
    admin = await login(client, "acme")
    await create_staff(client, admin, "alice")
    alice = await login(client, "acme", "alice", STAFF_PASSWORD)

    response = await client.post(STAFF, headers=bearer(alice), json=_payload("eve", ["agent"]))

    assert response.status_code == 403


async def test_duplicate_username_conflicts(app: FastAPI, client: httpx.AsyncClient) -> None:
    await provision(app, "acme")
    admin = await login(client, "acme")
    await create_staff(client, admin, "alice")

    response = await client.post(STAFF, headers=bearer(admin), json=_payload("alice", ["agent"]))

    assert response.status_code == 409


async def test_unknown_role_is_rejected(app: FastAPI, client: httpx.AsyncClient) -> None:
    await provision(app, "acme")
    admin = await login(client, "acme")

    response = await client.post(STAFF, headers=bearer(admin), json=_payload("eve", ["root"]))

    assert response.status_code == 422


async def test_staff_list_is_scoped_to_tenant(app: FastAPI, client: httpx.AsyncClient) -> None:
    await provision(app, "acme")
    await provision(app, "globex")
    acme_admin = await login(client, "acme")
    await create_staff(client, acme_admin, "alice")
    globex_admin = await login(client, "globex")

    acme = await client.get(STAFF, headers=bearer(acme_admin))
    globex = await client.get(STAFF, headers=bearer(globex_admin))

    assert sorted(s["username"] for s in acme.json()["items"]) == ["admin", "alice"]
    assert [s["username"] for s in globex.json()["items"]] == ["admin"]


async def test_cannot_grant_a_role_with_more_permissions_than_you_have(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    tenant_id = await provision(app, "acme")
    admin = await login(client, "acme")
    hr_id = uuid.UUID(await create_staff(client, admin, "hrmgr"))

    # 一个只能管理员工、但没有其他权限的自定义角色。
    db: Database = app.state.db
    async with db.platform_sessionmaker() as session:
        role = Role(
            tenant_id=tenant_id,
            code="hr",
            name="人事",
            permissions=[Permission.STAFF_READ, Permission.STAFF_MANAGE],
        )
        session.add(role)
        await session.flush()
        session.add(StaffRole(tenant_id=tenant_id, staff_id=hr_id, role_id=role.id))
        await session.commit()

    hr = await login(client, "acme", "hrmgr", STAFF_PASSWORD)
    response = await client.post(STAFF, headers=bearer(hr), json=_payload("boss", ["supervisor"]))

    assert response.status_code == 403
    # 企业所有者的角色只能由平台创建（§39.5），谁都不能分配。
    owner = await client.post(STAFF, headers=bearer(hr), json=_payload("boss", ["tenant_admin"]))
    assert owner.status_code == 422


async def test_system_role_permissions_come_from_code(
    app: FastAPI, client: httpx.AsyncClient, database_urls: DatabaseUrls
) -> None:
    # 迁移为已有租户补建的主管角色在数据库里没有保存权限点：系统角色的权限以代码为准。
    await provision(app, "acme")
    conn = await asyncpg.connect(database_urls.platform_dsn)
    try:
        await conn.execute("UPDATE roles SET permissions = '{}' WHERE code = 'supervisor'")
    finally:
        await conn.close()
    admin = await login(client, "acme")
    await create_staff(client, admin, "lead", roles=["supervisor"])
    lead = await login(client, "acme", "lead", STAFF_PASSWORD)

    me = (await client.get("/api/v1/me", headers=bearer(lead))).json()
    roles = (await client.get("/api/v1/roles", headers=bearer(admin))).json()["items"]

    supervisor = next(r for r in DEFAULT_ROLES if r.code == "supervisor")
    assert set(me["permissions"]) == set(supervisor.permissions)
    assert Permission.SESSION_READ_TEAM in me["permissions"]
    listed = next(r for r in roles if r["code"] == "supervisor")
    assert set(listed["permissions"]) == set(supervisor.permissions)
