"""迁移以非超级用户的表所有者执行（生产环境的 edp_owner，见 deploy/k8s/secrets.example.env）。

强制行级安全对表的所有者同样生效，而迁移时没有设置租户：整表复制、校验外键、调度进程建消息分区时
搬行，所有者都"看不到"任何租户的行。这里用这样的所有者走一遍 0017 的升级和降级，确认不丢数据、
不失败。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from tests.support import ROLE_PASSWORDS, SUPERUSER_URL

BACKEND_DIR = Path(__file__).resolve().parents[1]
OWNER_PASSWORD = "owner-test"


@dataclass(frozen=True)
class OwnedDatabase:
    superuser_dsn: str
    owner_url: str
    platform_dsn: str
    app_dsn: str

    async def migrate(self, action: str, revision: str) -> None:
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        cfg.set_main_option("sqlalchemy.url", self.owner_url)
        # env.py 自己启动事件循环，放到线程里执行。
        await asyncio.to_thread(getattr(command, action), cfg, revision)

    async def fetch(self, query: str, *args: object) -> list[asyncpg.Record]:
        conn = await asyncpg.connect(self.superuser_dsn)
        try:
            return list(await conn.fetch(query, *args))
        finally:
            await conn.close()


@pytest.fixture
async def owned() -> AsyncIterator[OwnedDatabase]:
    suffix = uuid.uuid4().hex[:8]
    role = dbname = f"edp_owner_{suffix}"
    conn = await asyncpg.connect(SUPERUSER_URL)
    try:
        await conn.execute(
            f"CREATE ROLE {role} LOGIN PASSWORD '{OWNER_PASSWORD}' NOSUPERUSER NOBYPASSRLS"
        )
        await conn.execute(f'CREATE DATABASE "{dbname}" OWNER {role}')
    finally:
        await conn.close()
    base = make_url(SUPERUSER_URL).set(database=dbname)
    db = OwnedDatabase(
        superuser_dsn=base.render_as_string(hide_password=False),
        owner_url=base.set(
            drivername="postgresql+asyncpg", username=role, password=OWNER_PASSWORD
        ).render_as_string(hide_password=False),
        platform_dsn=base.set(
            username="edp_platform", password=ROLE_PASSWORDS["edp_platform"]
        ).render_as_string(hide_password=False),
        app_dsn=base.set(username="edp_app", password=ROLE_PASSWORDS["edp_app"]).render_as_string(
            hide_password=False
        ),
    )
    # 扩展由数据库管理员预先安装（生产环境同样如此）。
    await db.fetch("CREATE EXTENSION IF NOT EXISTS vector")
    try:
        yield db
    finally:
        conn = await asyncpg.connect(SUPERUSER_URL)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
            await conn.execute(f"DROP ROLE IF EXISTS {role}")
        finally:
            await conn.close()


async def seed(db: OwnedDatabase) -> list[uuid.UUID]:
    """两个租户，各有 30 个月前、上个月和本月的消息，以及一条知识的检索单元。"""
    now = datetime.now(UTC)
    tenants = []
    conn = await asyncpg.connect(db.superuser_dsn)
    try:
        for n in range(2):
            tenant, channel, customer, identity, room, item = (uuid.uuid4() for _ in range(6))
            tenants.append(tenant)
            await conn.execute(
                "INSERT INTO tenants (id, code, name) VALUES ($1, $2, $3)",
                tenant,
                f"owner-{n}",
                f"租户{n}",
            )
            await conn.execute(
                "INSERT INTO channel_accounts (id, tenant_id, type, name, public_key)"
                " VALUES ($1, $2, 'web', '官网', $3)",
                channel,
                tenant,
                f"pk-{n}",
            )
            await conn.execute(
                "INSERT INTO customers (id, tenant_id, display_name) VALUES ($1, $2, '客户')",
                customer,
                tenant,
            )
            await conn.execute(
                "INSERT INTO customer_identities"
                " (id, tenant_id, customer_id, channel_account_id, external_id, im_user_id)"
                " VALUES ($1, $2, $3, $4, 'visitor', $5)",
                identity,
                tenant,
                customer,
                channel,
                f"im-{n}",
            )
            await conn.execute(
                "INSERT INTO rooms"
                " (id, tenant_id, customer_id, identity_id, channel_account_id, im_group_id)"
                " VALUES ($1, $2, $3, $4, $5, $6)",
                room,
                tenant,
                customer,
                identity,
                channel,
                f"group-{n}",
            )
            for sent_at in (now - timedelta(days=915), now - timedelta(days=31), now):
                await conn.execute(
                    "INSERT INTO messages (id, tenant_id, room_id, channel_account_id, direction,"
                    " sender_type, content_type, content, text_plain, source, sent_at)"
                    " VALUES ($1, $2, $3, $4, 'in', 'customer', 'text', '{}', '你好', 'webhook',"
                    " $5)",
                    uuid.uuid4(),
                    tenant,
                    room,
                    channel,
                    sent_at,
                )
            await conn.execute(
                "INSERT INTO kb_items (id, tenant_id, kind, title, content)"
                " VALUES ($1, $2, 'faq', '几天到', '一般 2 天')",
                item,
                tenant,
            )
            await conn.execute(
                "INSERT INTO kb_chunks (id, tenant_id, item_id, kind, text)"
                " VALUES ($1, $2, $3, 'question', '几天到')",
                uuid.uuid4(),
                tenant,
                item,
            )
    finally:
        await conn.close()
    return tenants


async def counts(db: OwnedDatabase) -> tuple[int, int]:
    [row] = await db.fetch(
        "SELECT (SELECT count(*) FROM messages) AS messages, (SELECT count(*) FROM kb_chunks) AS kb"
    )
    return row["messages"], row["kb"]


async def unforced(db: OwnedDatabase) -> list[str]:
    """带 tenant_id、启用了行级安全却没有强制的表。"""
    rows = await db.fetch(
        """
        SELECT c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id'
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relforcerowsecurity
        """
    )
    return sorted(r["relname"] for r in rows)


async def test_partitioning_migration_as_non_superuser_owner(owned: OwnedDatabase) -> None:
    await owned.migrate("upgrade", "0016")
    tenants = await seed(owned)
    assert await counts(owned) == (6, 2)

    await owned.migrate("upgrade", "0017")
    assert await counts(owned) == (6, 2)
    assert await unforced(owned) == []
    placed = await owned.fetch(
        "SELECT tableoid::regclass::text AS part, count(*) AS n FROM messages GROUP BY 1"
    )
    parts = {r["part"]: r["n"] for r in placed}
    assert parts.pop("messages_history") == 2
    assert sorted(parts.values()) == [2, 2]

    # 员工只看到本租户的消息（经父表的行级安全）。
    app = await asyncpg.connect(owned.app_dsn)
    try:
        await app.execute("SELECT set_config('app.tenant_id', $1, false)", str(tenants[0]))
        assert await app.fetchval("SELECT count(*) FROM messages") == 3
        assert await app.fetchval("SELECT count(*) FROM kb_chunks") == 1
    finally:
        await app.close()

    # 发送时间在还没建分区的月份：进入默认分区，调度进程建分区时搬走。
    await owned.fetch(
        "UPDATE messages SET sent_at = now() + interval '8 months'"
        " WHERE id = (SELECT id FROM messages ORDER BY sent_at DESC LIMIT 1)"
    )
    platform = await asyncpg.connect(owned.platform_dsn)
    try:
        assert await platform.fetchval("SELECT count(*) FROM messages_default") == 1
        assert await platform.fetchval("SELECT edp_ensure_message_partitions(9)") == 6
        assert await platform.fetchval("SELECT count(*) FROM messages_default") == 0
    finally:
        await platform.close()
    assert await counts(owned) == (6, 2)
    assert await unforced(owned) == []
    [moved] = await owned.fetch(
        "SELECT c.relname, count(*) FILTER (WHERE k.conparentid <> 0) AS attached,"
        " count(*) AS fks"
        " FROM messages m JOIN pg_class c ON c.oid = m.tableoid"
        " JOIN pg_constraint k ON k.conrelid = c.oid AND k.contype = 'f'"
        " WHERE m.sent_at > now() + interval '7 months' GROUP BY c.relname"
    )
    # 挂载分区时沿用事先建好的外键（没有重复）。
    [parent_fks] = await owned.fetch(
        "SELECT count(*) AS n FROM pg_constraint"
        " WHERE conrelid = 'messages'::regclass AND contype = 'f'"
    )
    assert moved["attached"] == moved["fks"] == parent_fks["n"]

    await owned.migrate("downgrade", "0016")
    assert await counts(owned) == (6, 2)
    assert await unforced(owned) == []
