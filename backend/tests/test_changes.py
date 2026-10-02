"""增量更新索引（设计文档 §33.9）：企业数据表的 change_seq 和每个企业的 tenant_data_index。"""

import importlib.util
import uuid
from types import ModuleType

import asyncpg
import pytest

from app.core.config import Settings
from app.db.session import Database
from app.modules.changes import service as changes
from tests.conftest import BACKEND_DIR
from tests.support import DatabaseUrls, TwoTenants

TRACKED_SQL = """
SELECT c.relname,
       EXISTS (SELECT 1 FROM pg_attribute a
                WHERE a.attrelid = c.oid AND a.attname = 'change_seq' AND NOT a.attisdropped)
         AS has_column,
       (SELECT array_agg(t.tgname::text ORDER BY t.tgname) FROM pg_trigger t
         WHERE t.tgrelid = c.oid AND NOT t.tgisinternal
           AND t.tgname IN ('edp_stamp_change', 'edp_data_index')) AS triggers
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
"""


def _migration() -> ModuleType:
    path = BACKEND_DIR / "alembic" / "versions" / "0036_change_index.py"
    spec = importlib.util.spec_from_file_location("migration_0036", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _connect(urls: DatabaseUrls) -> asyncpg.Connection:
    return await asyncpg.connect(urls.owner_dsn)


async def _index(conn: asyncpg.Connection, tenant_id: uuid.UUID) -> dict[str, int]:
    rows = await conn.fetch(
        "SELECT domain, seq FROM tenant_data_index WHERE tenant_id = $1", tenant_id
    )
    return {r["domain"]: r["seq"] for r in rows}


async def _customer(conn: asyncpg.Connection, tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    customer_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO customers (id, tenant_id, display_name) VALUES ($1, $2, $3)",
        customer_id,
        tenant_id,
        name,
    )
    return customer_id


async def test_every_tenant_table_is_tracked_unless_excluded(
    database_urls: DatabaseUrls,
) -> None:
    """企业的每张数据表都有增量字段和两个触发器；不记变化的表与迁移、代码里的清单一致。以后新建的
    企业数据表要在迁移里调用 edp_track_changes，否则这个测试失败。"""
    assert set(_migration().EXCLUDED) == changes.EXCLUDED
    conn = await _connect(database_urls)
    try:
        rows = await conn.fetch(TRACKED_SQL)
    finally:
        await conn.close()
    tracked = {r["relname"]: (r["has_column"], r["triggers"]) for r in rows}
    assert set(tracked) >= changes.EXCLUDED, "清单里有不存在的表"
    wrong = {
        name: state
        for name, state in tracked.items()
        if state
        != (
            (False, None)
            if name in changes.EXCLUDED
            else (True, ["edp_data_index", "edp_stamp_change"])
        )
    }
    assert wrong == {}
    assert len(tracked) - len(changes.EXCLUDED) > 80


async def test_changes_stamp_rows_and_update_the_tenant_index(
    database_urls: DatabaseUrls, two_tenants: TwoTenants
) -> None:
    a, b = two_tenants.tenant_a, two_tenants.tenant_b
    conn = await _connect(database_urls)
    try:
        before_b = await _index(conn, b)
        customer_id = await _customer(conn, a, "张三")
        seq = await conn.fetchval("SELECT change_seq FROM customers WHERE id = $1", customer_id)
        index = await _index(conn, a)
        # 新增：这一行有了变化编号，index 在提交时更新（编号比这一行的大）。
        assert seq is not None and index["customers"] > seq
        assert await _index(conn, b) == before_b

        await conn.execute(
            "UPDATE customers SET display_name = '张三丰' WHERE id = $1", customer_id
        )
        updated = await conn.fetchval("SELECT change_seq FROM customers WHERE id = $1", customer_id)
        assert updated > seq
        assert (await _index(conn, a))["customers"] > index["customers"]

        # 一个事务里改很多行：每张表只更新一次 index，编号比这些行的都大。
        async with conn.transaction():
            for i in range(5):
                await _customer(conn, a, f"客户{i}")
        latest = await conn.fetchval(
            "SELECT max(change_seq) FROM customers WHERE tenant_id = $1", a
        )
        after_bulk = await _index(conn, a)
        assert after_bulk["customers"] > latest

        # 删除：行没有了，index 仍然记下这次变化。
        await conn.execute("DELETE FROM customers WHERE id = $1", customer_id)
        assert (await _index(conn, a))["customers"] > after_bulk["customers"]

        # "上次之后变了哪些"：按 (tenant_id, change_seq) 的索引只读变化的行。
        changed = await conn.fetch(
            "SELECT display_name FROM customers WHERE tenant_id = $1 AND change_seq > $2"
            " ORDER BY change_seq",
            a,
            latest - 1,
        )
        assert [r["display_name"] for r in changed] == ["客户4"]
    finally:
        await conn.close()


async def test_counters_and_poll_times_are_not_data_changes(
    database_urls: DatabaseUrls, two_tenants: TwoTenants
) -> None:
    """知识的命中次数、评价，邮箱的收信时间等只改这些列时不算变化；改了内容才算。"""
    a = two_tenants.tenant_a
    item_id = uuid.uuid4()
    conn = await _connect(database_urls)
    try:
        await conn.execute(
            "INSERT INTO kb_items (id, tenant_id, kind, title, content)"
            " VALUES ($1, $2, 'faq', '退货期限是多久？', '7 天')",
            item_id,
            a,
        )
        seq = await conn.fetchval("SELECT change_seq FROM kb_items WHERE id = $1", item_id)
        index = await _index(conn, a)
        await conn.execute(
            "UPDATE kb_items SET hits = hits + 1, last_hit_at = now(), likes = 3,"
            " updated_at = now() WHERE id = $1",
            item_id,
        )
        assert await conn.fetchval("SELECT change_seq FROM kb_items WHERE id = $1", item_id) == seq
        assert await _index(conn, a) == index
        await conn.execute("UPDATE kb_items SET content = '15 天' WHERE id = $1", item_id)
        assert await conn.fetchval("SELECT change_seq FROM kb_items WHERE id = $1", item_id) > seq
        assert (await _index(conn, a))["kb_items"] > index["kb_items"]

        # 打印机：定时查询状态只更新查询时间，不算变化；状态变了才算。
        printer_id = uuid.uuid4()
        await conn.execute(
            "INSERT INTO printers (id, tenant_id, name, brand, account, key_enc, sn)"
            " VALUES ($1, $2, '前台', 'xpyun', 'dev@example.com', 'sealed', 'XPY1')",
            printer_id,
            a,
        )
        index = await _index(conn, a)
        await conn.execute(
            "UPDATE printers SET status_checked_at = now() WHERE id = $1", printer_id
        )
        assert await _index(conn, a) == index
        await conn.execute("UPDATE printers SET status = 'offline' WHERE id = $1", printer_id)
        assert (await _index(conn, a))["printers"] > index["printers"]
    finally:
        await conn.close()


async def test_purging_a_tenant_does_not_recreate_its_index(
    database_urls: DatabaseUrls, two_tenants: TwoTenants
) -> None:
    """删除整个租户的数据时（注销，§19）设置了 edp.data_index_off，触发器不再写 index。"""
    a = two_tenants.tenant_a
    conn = await _connect(database_urls)
    try:
        await _customer(conn, a, "张三")
        async with conn.transaction():
            await conn.execute(f"SET LOCAL {changes.OFF_SETTING} = 'on'")
            await conn.execute("DELETE FROM tenant_data_index WHERE tenant_id = $1", a)
            await conn.execute("DELETE FROM customers WHERE tenant_id = $1", a)
        assert await _index(conn, a) == {}
    finally:
        await conn.close()


async def test_tenants_only_read_their_own_index(
    database_urls: DatabaseUrls, two_tenants: TwoTenants, settings: Settings
) -> None:
    conn = await _connect(database_urls)
    try:
        await _customer(conn, two_tenants.tenant_a, "张三")
        await _customer(conn, two_tenants.tenant_b, "李四")
        await conn.execute(
            "UPDATE staff SET display_name = 'Bob 2' WHERE id = $1", two_tenants.staff_b
        )
    finally:
        await conn.close()
    db = Database(settings)
    try:
        async with db.tenant_session(two_tenants.tenant_a) as session:
            index = await changes.snapshot(session)
            recent, tracked = await changes.recent(session)
    finally:
        await db.dispose()
    # 租户 A 的员工（造数据时写入）和客户；B 的变化看不到。
    assert set(index) == {"staff", "customers"}
    assert tracked == 2 and recent[0].domain == "customers"
    assert changes.latest(index, ("customers", "orders")) == index["customers"]
    assert changes.latest(index, ("orders",)) == 0


@pytest.mark.parametrize("table", sorted(_migration().IGNORED_COLUMNS))
async def test_ignored_columns_exist(database_urls: DatabaseUrls, table: str) -> None:
    conn = await _connect(database_urls)
    try:
        columns = {
            r["attname"]
            for r in await conn.fetch(
                "SELECT attname FROM pg_attribute WHERE attrelid = $1::regclass"
                " AND attnum > 0 AND NOT attisdropped",
                table,
            )
        }
    finally:
        await conn.close()
    assert set(_migration().IGNORED_COLUMNS[table]) <= columns
