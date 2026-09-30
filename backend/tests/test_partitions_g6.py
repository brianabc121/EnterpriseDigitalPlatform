"""G6 大表分区：messages 按月分区、kb_chunks 按租户哈希分区；分区维护；分区后的去重。"""

import asyncio
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.partitions import MONTHS_AHEAD, ensure_partitions, partition_status
from app.modules.sessions import messages
from tests.desk import Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_agent_messages import send, serving


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


def month_name(year: int, month: int) -> str:
    return f"messages_y{year}m{month:02d}"


def add_months(when: datetime, months: int) -> tuple[int, int]:
    index = when.year * 12 + when.month - 1 + months
    return index // 12, index % 12 + 1


async def test_large_tables_are_partitioned(desk: Desk) -> None:
    strategies = {
        r["table"]: r["strategy"]
        for r in await desk.sql(
            "SELECT partrelid::regclass::text AS table, partstrat::text AS strategy"
            " FROM pg_partitioned_table"
        )
    }
    assert strategies == {"messages": "r", "kb_chunks": "h"}
    chunks = await desk.sql(
        "SELECT count(*) AS n FROM pg_inherits WHERE inhparent = 'kb_chunks'::regclass"
    )
    assert chunks[0]["n"] == 8
    partitions = {
        r["name"]
        for r in await desk.sql(
            "SELECT inhrelid::regclass::text AS name FROM pg_inherits"
            " WHERE inhparent = 'messages'::regclass"
        )
    }
    now = datetime.now(UTC)
    expected = {month_name(*add_months(now, i)) for i in range(MONTHS_AHEAD + 1)}
    assert expected | {"messages_default", "messages_history"} <= partitions


async def test_partition_job_creates_months_and_moves_stray_rows(desk: Desk) -> None:
    alice, _, session_id = await serving(desk)
    assert (await send(desk, alice, session_id, "您好")).status_code == 200
    now = datetime.now(UTC)
    year, month = add_months(now, MONTHS_AHEAD + 2)
    stray = datetime(year, month, 15, 8, tzinfo=UTC)
    created = [month_name(*add_months(now, i)) for i in (MONTHS_AHEAD + 1, MONTHS_AHEAD + 2)]
    try:
        # 发送时间在还没建分区的月份：进入默认分区。
        await desk.sql("UPDATE messages SET sent_at = $1 WHERE text_plain = '您好'", stray)
        async with desk.ctx.db.platform_sessionmaker() as session:
            assert (await partition_status(session)).default_rows == 1

        assert await ensure_partitions(desk.ctx) == 0  # 本月和之后 3 个月都已建好
        async with desk.ctx.db.platform_sessionmaker() as session:
            made = await session.scalar(text("SELECT edp_ensure_message_partitions(:n)"), {"n": 5})
            await session.commit()
            status = await partition_status(session)
        assert made == 2
        assert (status.months_ahead, status.default_rows) == (5, 0)
        # 新分区与父表一样强制行级安全；默认分区搬完行后恢复强制。
        protected = await desk.sql(
            "SELECT c.relname, c.relforcerowsecurity AS forced, count(p.polname) AS policies"
            " FROM pg_class c LEFT JOIN pg_policy p ON p.polrelid = c.oid"
            " WHERE c.relname = ANY($1) GROUP BY c.relname, c.relforcerowsecurity",
            [*created, "messages_default"],
        )
        assert sorted((r["relname"], r["forced"], r["policies"]) for r in protected) == sorted(
            (name, True, 2) for name in [*created, "messages_default"]
        )
        [row] = await desk.sql(
            "SELECT tableoid::regclass::text AS part FROM messages WHERE text_plain = '您好'"
        )
        assert row["part"] == month_name(year, month)
        # 搬过去的行照常可以读到。
        page = await desk.client.get(
            f"/api/v1/sessions/{session_id}/messages", headers=alice.headers
        )
        assert "您好" in [m["text_plain"] for m in page.json()["items"]]
    finally:
        for name in created:
            await desk.sql(f"DROP TABLE IF EXISTS {name}")


async def test_partitions_are_only_reachable_through_the_parent(desk: Desk) -> None:
    async with desk.ctx.db.app_sessionmaker() as session:
        with pytest.raises(Exception, match="permission denied"):
            await session.execute(text("SELECT count(*) FROM messages_default"))
    async with desk.ctx.db.app_sessionmaker() as session:
        with pytest.raises(Exception, match="permission denied"):
            await session.execute(text("SELECT edp_ensure_message_partitions(1)"))


async def test_concurrent_resends_of_one_message_store_it_once(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice, _, session_id = await serving(desk)
    client_msg_id = uuid.uuid4().hex
    original = messages.check_agent_text

    async def slow_check(session: AsyncSession, text: str) -> None:
        # 让几个请求都在"查不到这条消息"之后、写入之前停一下，重现并发重试。
        await asyncio.sleep(0.2)
        await original(session, text)

    monkeypatch.setattr(messages, "check_agent_text", slow_check)

    responses = await asyncio.gather(
        *(
            send(desk, alice, session_id, "马上为您处理", client_msg_id=client_msg_id)
            for _ in range(4)
        )
    )

    assert {r.status_code for r in responses} == {200}
    assert len({r.json()["id"] for r in responses}) == 1
    rows = await desk.sql("SELECT id FROM messages WHERE client_msg_id = $1", client_msg_id)
    assert len(rows) == 1
