"""客户数据范围（DataScope）与租户隔离——对应 P0 验收标准第 2 条。"""

import httpx
import pytest
from fastapi import FastAPI

from tests.factories import (
    STAFF_PASSWORD,
    bearer,
    create_knowledge_role,
    create_staff,
    login,
    provision,
)

CUSTOMERS = "/api/v1/customers"


class Acme:
    """一个租户：管理员 + 两名坐席（alice、bob）。"""

    admin: str
    alice: str
    bob: str
    alice_id: str
    bob_id: str


@pytest.fixture
async def acme(app: FastAPI, client: httpx.AsyncClient) -> Acme:
    await provision(app, "acme")
    ctx = Acme()
    ctx.admin = await login(client, "acme")
    ctx.alice_id = await create_staff(client, ctx.admin, "alice")
    ctx.bob_id = await create_staff(client, ctx.admin, "bob")
    ctx.alice = await login(client, "acme", "alice", STAFF_PASSWORD)
    ctx.bob = await login(client, "acme", "bob", STAFF_PASSWORD)
    return ctx


async def _create(
    client: httpx.AsyncClient, token: str, name: str, owner_id: str | None = None
) -> httpx.Response:
    payload: dict[str, str] = {"display_name": name}
    if owner_id:
        payload["owner_id"] = owner_id
    return await client.post(CUSTOMERS, headers=bearer(token), json=payload)


async def _names(client: httpx.AsyncClient, token: str) -> list[str]:
    response = await client.get(CUSTOMERS, headers=bearer(token))
    assert response.status_code == 200, response.text
    return sorted(c["display_name"] for c in response.json()["items"])


async def test_agents_see_only_their_own_customers_and_admin_sees_all(
    client: httpx.AsyncClient, acme: Acme
) -> None:
    await _create(client, acme.alice, "A1")
    await _create(client, acme.alice, "A2")
    await _create(client, acme.bob, "B1")

    assert await _names(client, acme.alice) == ["A1", "A2"]
    assert await _names(client, acme.bob) == ["B1"]
    assert await _names(client, acme.admin) == ["A1", "A2", "B1"]


async def test_agent_cannot_open_another_agents_customer(
    client: httpx.AsyncClient, acme: Acme
) -> None:
    customer_id = (await _create(client, acme.alice, "A1")).json()["id"]

    as_bob = await client.get(f"{CUSTOMERS}/{customer_id}", headers=bearer(acme.bob))
    as_admin = await client.get(f"{CUSTOMERS}/{customer_id}", headers=bearer(acme.admin))

    assert as_bob.status_code == 404
    assert as_admin.status_code == 200
    assert as_admin.json()["owner_display_name"] == "Alice"


async def test_agent_cannot_assign_customer_to_someone_else(
    client: httpx.AsyncClient, acme: Acme
) -> None:
    response = await _create(client, acme.alice, "A1", owner_id=acme.bob_id)
    assert response.status_code == 403


async def test_admin_can_assign_owner(client: httpx.AsyncClient, acme: Acme) -> None:
    response = await _create(client, acme.admin, "C1", owner_id=acme.alice_id)

    assert response.status_code == 201
    assert response.json()["owner_id"] == acme.alice_id
    assert await _names(client, acme.alice) == ["C1"]


async def test_pagination_reports_total(client: httpx.AsyncClient, acme: Acme) -> None:
    for name in ("A1", "A2", "A3"):
        await _create(client, acme.alice, name)

    response = await client.get(f"{CUSTOMERS}?limit=2", headers=bearer(acme.alice))

    assert len(response.json()["items"]) == 2
    assert response.json()["total"] == 3


async def test_role_without_customer_permission_is_forbidden(
    client: httpx.AsyncClient, acme: Acme
) -> None:
    knowledge = await create_knowledge_role(client, acme.admin)
    await create_staff(client, acme.admin, "kate", roles=[knowledge])
    kate = await login(client, "acme", "kate", STAFF_PASSWORD)

    assert (await client.get(CUSTOMERS, headers=bearer(kate))).status_code == 403


async def test_tenants_cannot_see_each_others_customers(
    app: FastAPI, client: httpx.AsyncClient, acme: Acme
) -> None:
    customer_id = (await _create(client, acme.admin, "Acme-1")).json()["id"]
    await provision(app, "globex")
    globex_admin = await login(client, "globex")

    assert await _names(client, globex_admin) == []
    other = await client.get(f"{CUSTOMERS}/{customer_id}", headers=bearer(globex_admin))
    assert other.status_code == 404


async def test_owner_from_another_tenant_is_rejected(
    app: FastAPI, client: httpx.AsyncClient, acme: Acme
) -> None:
    await provision(app, "globex")
    globex_admin = await login(client, "globex")
    globex_agent_id = await create_staff(client, globex_admin, "gary")

    response = await _create(client, acme.admin, "C1", owner_id=globex_agent_id)

    assert response.status_code == 422
