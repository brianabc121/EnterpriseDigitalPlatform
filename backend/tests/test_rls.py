"""数据库层面的租户隔离：直接用 edp_app / edp_platform 角色连接，绕过应用代码。"""

import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

from tests.support import DatabaseUrls, TwoTenants

INSERT_STAFF = (
    "INSERT INTO staff (id, tenant_id, username, display_name, password_hash) "
    "VALUES ($1, $2, $3, 'X', 'x')"
)


@pytest.fixture
async def app_conn(database_urls: DatabaseUrls) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(database_urls.app_dsn)
    yield conn
    await conn.close()


async def _use_tenant(conn: asyncpg.Connection, tenant_id: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", str(tenant_id))


async def test_without_tenant_context_nothing_is_visible(
    app_conn: asyncpg.Connection, two_tenants: TwoTenants
) -> None:
    assert await app_conn.fetchval("SELECT count(*) FROM staff") == 0


async def test_only_current_tenant_is_visible(
    app_conn: asyncpg.Connection, two_tenants: TwoTenants
) -> None:
    async with app_conn.transaction():
        await _use_tenant(app_conn, two_tenants.tenant_a)
        rows = await app_conn.fetch("SELECT username FROM staff")
    assert [r["username"] for r in rows] == ["alice"]


async def test_tenant_context_does_not_outlive_its_transaction(
    app_conn: asyncpg.Connection, two_tenants: TwoTenants
) -> None:
    async with app_conn.transaction():
        await _use_tenant(app_conn, two_tenants.tenant_a)
    # 事务结束后设置被重置为空字符串：应返回空结果，而不是报错或泄露数据。
    assert await app_conn.fetchval("SELECT count(*) FROM staff") == 0


async def test_cannot_write_rows_for_another_tenant(
    app_conn: asyncpg.Connection, two_tenants: TwoTenants
) -> None:
    tx = app_conn.transaction()
    await tx.start()
    try:
        await _use_tenant(app_conn, two_tenants.tenant_a)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute(INSERT_STAFF, uuid.uuid4(), two_tenants.tenant_b, "mallory")
    finally:
        await tx.rollback()


async def test_cannot_move_rows_to_another_tenant(
    app_conn: asyncpg.Connection, two_tenants: TwoTenants
) -> None:
    tx = app_conn.transaction()
    await tx.start()
    try:
        await _use_tenant(app_conn, two_tenants.tenant_a)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app_conn.execute("UPDATE staff SET tenant_id = $1", two_tenants.tenant_b)
    finally:
        await tx.rollback()


async def test_tenant_id_defaults_to_current_tenant(
    app_conn: asyncpg.Connection, two_tenants: TwoTenants
) -> None:
    tx = app_conn.transaction()
    await tx.start()
    try:
        await _use_tenant(app_conn, two_tenants.tenant_a)
        tenant_id = await app_conn.fetchval(
            "INSERT INTO customers (id, display_name) VALUES ($1, 'C') RETURNING tenant_id",
            uuid.uuid4(),
        )
        assert tenant_id == two_tenants.tenant_a
    finally:
        await tx.rollback()


async def test_insert_without_tenant_context_is_rejected(
    app_conn: asyncpg.Connection, two_tenants: TwoTenants
) -> None:
    # RLS 的 WITH CHECK 先于 NOT NULL 约束生效。
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute(
            "INSERT INTO customers (id, display_name) VALUES ($1, 'C')", uuid.uuid4()
        )


async def test_cross_tenant_reference_is_rejected_even_for_platform(
    database_urls: DatabaseUrls, two_tenants: TwoTenants
) -> None:
    conn = await asyncpg.connect(database_urls.platform_dsn)
    try:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                "INSERT INTO customers (id, tenant_id, display_name, owner_id) "
                "VALUES ($1, $2, 'C', $3)",
                uuid.uuid4(),
                two_tenants.tenant_a,
                two_tenants.staff_b,
            )
    finally:
        await conn.close()


async def test_platform_role_sees_every_tenant(
    database_urls: DatabaseUrls, two_tenants: TwoTenants
) -> None:
    conn = await asyncpg.connect(database_urls.platform_dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM staff") == 2
    finally:
        await conn.close()


async def test_app_role_cannot_modify_tenants(
    app_conn: asyncpg.Connection, two_tenants: TwoTenants
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE tenants SET status = 'suspended'")


async def test_every_table_with_tenant_id_has_forced_rls(database_urls: DatabaseUrls) -> None:
    conn = await asyncpg.connect(database_urls.owner_dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a
              ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
            """
        )
    finally:
        await conn.close()
    assert rows, "expected tenant tables"
    unprotected = [
        r["relname"] for r in rows if not (r["relrowsecurity"] and r["relforcerowsecurity"])
    ]
    assert unprotected == []
