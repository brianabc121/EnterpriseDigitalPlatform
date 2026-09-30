import uuid

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from app.modules.channels.service import new_public_key, tenant_code_of
from tests.factories import bearer, create_staff, login, provision
from tests.support import DatabaseUrls


def test_public_key_carries_the_tenant_code() -> None:
    key = new_public_key("demo-co")
    assert tenant_code_of(key) == "demo-co"
    assert len(key.split(".")[1]) == 32
    assert tenant_code_of("no-dot") is None
    assert tenant_code_of(".abc") is None
    assert tenant_code_of("demo.") is None


async def test_provisioning_creates_a_default_web_channel(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await provision(app, "acme")
    await provision(app, "globex")
    token = await login(client, "acme")

    response = await client.get("/api/v1/channels", headers=bearer(token))

    assert response.status_code == 200
    items = response.json()["items"]
    assert [(c["type"], c["name"], c["status"]) for c in items] == [("web", "官网", "active")]
    assert tenant_code_of(items[0]["public_key"]) == "acme"


async def test_agents_cannot_list_channels(app: FastAPI, client: httpx.AsyncClient) -> None:
    await provision(app, "acme")
    admin = await login(client, "acme")
    await create_staff(client, admin, "alice")
    agent = await login(client, "acme", "alice", "staff-pass-123")

    response = await client.get("/api/v1/channels", headers=bearer(agent))

    assert response.status_code == 403


async def test_identity_cannot_reference_another_tenants_channel(
    app: FastAPI, database_urls: DatabaseUrls
) -> None:
    tenant_a = await provision(app, "acme")
    await provision(app, "globex")
    conn = await asyncpg.connect(database_urls.platform_dsn)
    try:
        other_channel = await conn.fetchval(
            "SELECT c.id FROM channel_accounts c JOIN tenants t ON t.id = c.tenant_id "
            "WHERE t.code = 'globex'"
        )
        customer_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO customers (id, tenant_id, display_name) VALUES ($1, $2, 'x')",
            customer_id,
            tenant_a,
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO customer_identities "
                "(id, tenant_id, customer_id, channel_account_id, external_id, im_user_id) "
                "VALUES ($1, $2, $3, $4, 'v1', 'acme_c_1')",
                uuid.uuid4(),
                tenant_a,
                customer_id,
                other_channel,
            )
    finally:
        await conn.close()
