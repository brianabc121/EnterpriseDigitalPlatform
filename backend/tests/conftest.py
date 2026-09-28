"""测试基础设施。

需要一个可以用超级用户连接的 PostgreSQL（默认连本地 `make dev-up` 起的实例，
也可以用 EDP_TEST_PG_SUPERUSER_URL 指定）。每次测试会话创建一个临时数据库并执行迁移，
每个测试开始前清空所有表。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import httpx
import pytest
from alembic.config import Config
from fastapi import FastAPI

from alembic import command
from app.core.config import Settings
from app.main import create_app
from tests.support import ROLE_PASSWORDS, SUPERUSER_URL, DatabaseUrls, TwoTenants, seed_two_tenants

ALL_TABLES = (
    "tenants, platform_users, staff, roles, staff_roles, customers, refresh_tokens, audit_logs, "
    "channel_accounts, customer_identities, rooms, messages"
)
BACKEND_DIR = Path(__file__).resolve().parents[1]


async def _create_database(dbname: str) -> None:
    try:
        conn = await asyncpg.connect(SUPERUSER_URL)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.exit(
            f"无法连接测试用 PostgreSQL（{SUPERUSER_URL}）：{exc}。"
            "请先执行 `make dev-up`，或设置 EDP_TEST_PG_SUPERUSER_URL。",
            returncode=2,
        )
    try:
        for role, password in ROLE_PASSWORDS.items():
            if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
                await conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        await conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await conn.close()


async def _drop_database(dbname: str) -> None:
    conn = await asyncpg.connect(SUPERUSER_URL)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def database_urls() -> Iterator[DatabaseUrls]:
    dbname = f"edp_test_{uuid.uuid4().hex[:8]}"
    asyncio.run(_create_database(dbname))
    urls = DatabaseUrls(dbname)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", urls.owner)
    try:
        command.upgrade(cfg, "head")
        yield urls
    finally:
        asyncio.run(_drop_database(dbname))


@pytest.fixture(autouse=True)
async def _clean_tables(database_urls: DatabaseUrls) -> None:
    conn = await asyncpg.connect(database_urls.owner_dsn)
    try:
        await conn.execute(f"TRUNCATE {ALL_TABLES} CASCADE")
    finally:
        await conn.close()


@pytest.fixture
async def two_tenants(database_urls: DatabaseUrls) -> TwoTenants:
    return await seed_two_tenants(database_urls.platform_dsn)


@pytest.fixture
def settings(database_urls: DatabaseUrls) -> Settings:
    return Settings(
        env="test",
        database_url_app=database_urls.app,
        database_url_platform=database_urls.platform,
        database_url_owner=database_urls.owner,
        cookie_secure=False,
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    yield application
    await application.state.db.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
