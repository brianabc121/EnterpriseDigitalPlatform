"""测试基础设施。

需要一个可以用超级用户连接的 PostgreSQL（默认连本地 `make dev-up` 起的实例，
也可以用 EDP_TEST_PG_SUPERUSER_URL 指定）。每次测试会话创建一个临时数据库并执行迁移，
每个测试开始前清空所有表。

限流用到 Redis（默认 `make dev-up` 起的实例的 15 号库，可以用 EDP_TEST_REDIS_URL 指定），
每个测试开始前清空。OpenIM 用内存版（tests/fake_openim.py），大模型用模拟服务（tests/fake_llm.py）。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
import httpx
import pytest
from alembic.config import Config
from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError

from alembic import command
from app.core.config import Settings
from app.integrations.llm import EmbedEndpoint, LLMClient, LLMEndpoint
from app.integrations.openim import OpenIMClient
from app.main import create_app
from tests.fake_llm import DIM as FAKE_EMBED_DIM
from tests.fake_llm import FakeLLM
from tests.fake_openim import SECRET as FAKE_OPENIM_SECRET
from tests.fake_openim import FakeOpenIM
from tests.fake_storage import FakeStorage
from tests.fake_web import FakeWeb
from tests.fake_wecom import FakeWeCom
from tests.support import (
    REDIS_URL,
    ROLE_PASSWORDS,
    SUPERUSER_URL,
    DatabaseUrls,
    TwoTenants,
    seed_two_tenants,
)

ALL_TABLES = (
    "tenants, platform_users, staff, roles, staff_roles, customers, refresh_tokens, audit_logs, "
    "channel_accounts, customer_identities, rooms, messages, skill_groups, skill_group_members, "
    "routing_policies, agent_states, sessions, session_events, tickets, im_ops, quick_replies, "
    "session_transfers, customer_owner_history, usage_daily, ai_settings, kb_items, kb_chunks, "
    "ai_session_states, ai_decisions, llm_calls, ai_eval_runs, llm_providers, platform_settings, "
    "prompt_templates"
)
SEEDED_PLANS = "'trial', 'standard', 'enterprise'"
BACKEND_DIR = Path(__file__).resolve().parents[1]

if TYPE_CHECKING:
    from tests.wecom_desk import WecomDesk


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
        # 迁移预置的套餐保留，测试里新建的删除。
        await conn.execute(f"DELETE FROM plans WHERE code NOT IN ({SEEDED_PLANS})")
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _clean_redis() -> None:
    redis = Redis.from_url(REDIS_URL)
    try:
        await redis.flushdb()
    except RedisError as exc:
        pytest.exit(
            f"无法连接测试用 Redis（{REDIS_URL}）：{exc}。"
            "请先执行 `make dev-up`，或设置 EDP_TEST_REDIS_URL。",
            returncode=2,
        )
    finally:
        await redis.aclose()


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
        redis_url=REDIS_URL,
        openim_api_url="http://openim",
        openim_secret=FAKE_OPENIM_SECRET,
        ai_debounce_seconds=0,
    )


@pytest.fixture
def fake_im() -> FakeOpenIM:
    return FakeOpenIM()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


def fake_llm_client(fake: FakeLLM) -> LLMClient:
    """接到模拟大模型的客户端（对话与向量都可用，不重试，测试更快）。"""
    return LLMClient(
        LLMEndpoint(
            base_url="http://fake-llm/v1", api_key="k", chat_model="fake-chat", name="fake"
        ),
        embed=EmbedEndpoint(
            base_url="http://fake-llm/v1", api_key="k", model="fake-embed", dim=FAKE_EMBED_DIM
        ),
        retries=0,
        transport=fake.transport(),
    )


@pytest.fixture
def fake_web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
async def app(
    settings: Settings,
    fake_im: FakeOpenIM,
    fake_llm: FakeLLM,
    fake_storage: FakeStorage,
    fake_web: FakeWeb,
) -> AsyncIterator[FastAPI]:
    im = OpenIMClient(
        settings.openim_api_url, secret=FAKE_OPENIM_SECRET, transport=fake_im.transport()
    )
    application = create_app(
        settings,
        im=im,
        llm=fake_llm_client(fake_llm),
        storage_transport=fake_storage.transport(),
        llm_transport=fake_llm.transport(),
        web_transport=fake_web.transport(),
    )
    yield application
    await application.state.ctx.web.aclose()
    await application.state.ctx.llm.aclose()
    await application.state.ctx.storage.aclose()
    await im.aclose()
    await application.state.redis.aclose()
    await application.state.db.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---- 企业微信（tests/wecom_desk.py） ----


@pytest.fixture
def fake_wecom() -> FakeWeCom:
    return FakeWeCom()


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
async def wecom_app(
    settings: Settings,
    fake_im: FakeOpenIM,
    fake_llm: FakeLLM,
    fake_wecom: FakeWeCom,
    fake_storage: FakeStorage,
) -> AsyncIterator[FastAPI]:
    """配置了企业微信服务商的应用：接到模拟企业微信和内存对象存储。"""
    from tests.wecom_desk import wecom_settings

    im = OpenIMClient(
        settings.openim_api_url, secret=FAKE_OPENIM_SECRET, transport=fake_im.transport()
    )
    application = create_app(
        wecom_settings(settings),
        im=im,
        llm=fake_llm_client(fake_llm),
        wecom_transport=fake_wecom.transport(),
        storage_transport=fake_storage.transport(),
        llm_transport=fake_llm.transport(),
    )
    yield application
    await application.state.ctx.aclose()


@pytest.fixture
async def wecom_client(
    wecom_app: FastAPI, fake_wecom: FakeWeCom
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=wecom_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        # 模拟企业微信经这个客户端把回调推送给平台。
        fake_wecom.platform = c
        yield c


@pytest.fixture
async def wdesk(
    wecom_app: FastAPI,
    wecom_client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
    fake_wecom: FakeWeCom,
    fake_storage: FakeStorage,
) -> "WecomDesk":
    from tests.wecom_desk import WecomDesk, wecom_settings

    desk = WecomDesk(
        wecom_app,
        wecom_client,
        fake_im,
        wecom_settings(settings),
        database_urls,
        fake_wecom,
        fake_storage,
    )
    await desk.open()
    return desk
