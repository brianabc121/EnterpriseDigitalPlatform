"""用量计量：按日汇总、幂等重算、租户与平台查询。"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.dates import day_bounds, today
from app.modules.usage.service import rollup_day, run_usage_rollup
from tests.desk import Desk
from tests.factories import (
    STAFF_PASSWORD,
    bearer,
    create_platform_admin,
    login,
    platform_login,
)
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def serve_one_visitor(desk: Desk) -> None:
    """一位访客咨询，坐席 Alice 回复一句文字和一张 4 KB 的图片。"""
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    url = f"/api/v1/sessions/{chat['id']}/messages"
    await desk.client.post(
        url,
        headers=alice.headers,
        json={"client_msg_id": uuid.uuid4().hex, "text": "您好"},
    )
    upload = await desk.client.post(
        "/api/v1/uploads",
        headers=alice.headers,
        json={"filename": "a.png", "content_type": "image/png", "size": 4096},
    )
    await desk.client.post(
        url,
        headers=alice.headers,
        json={
            "client_msg_id": uuid.uuid4().hex,
            "type": "image",
            "attachment": {
                "url": upload.json()["file_url"],
                "name": "a.png",
                "size": 4096,
                "content_type": "image/png",
            },
        },
    )
    await desk.flush()


async def usage_rows(desk: Desk, day: date) -> dict[str, int]:
    rows = await desk.sql(
        "SELECT metric, value FROM usage_daily WHERE tenant_id = $1 AND day = $2",
        desk.tenant_id,
        day,
    )
    return {r["metric"]: r["value"] for r in rows}


def test_days_follow_the_usage_timezone() -> None:
    start, end = day_bounds(date(2026, 9, 28), TZ)
    assert start.astimezone(UTC) == datetime(2026, 9, 27, 16, tzinfo=UTC)
    assert end - start == timedelta(days=1)
    # 北京时间 9 月 28 日 0:30 还是 UTC 的 27 日。
    assert today(TZ, datetime(2026, 9, 27, 16, 30, tzinfo=UTC)) == date(2026, 9, 28)


async def test_rollup_counts_a_day_of_service(desk: Desk) -> None:
    await serve_one_visitor(desk)
    day = today(TZ)

    report = await rollup_day(desk.ctx.db, day, TZ)

    assert (report.tenants, report.errors) == (1, 0)
    assert await usage_rows(desk, day) == {
        "messages_in": 1,
        "agent_messages": 2,
        "sessions": 1,
        "human_sessions": 1,
        "new_customers": 1,
        "active_agents": 1,
        "file_bytes": 4096,
        "seats": 2,  # 管理员和 Alice
        "channels": 1,
    }
    # 前一天没有数据，也不补员工数、渠道数这类只在当天记录的快照。
    await rollup_day(desk.ctx.db, day - timedelta(days=1), TZ)
    assert await usage_rows(desk, day - timedelta(days=1)) == {}


async def test_rollup_is_idempotent_and_drops_zeroes(desk: Desk) -> None:
    await serve_one_visitor(desk)
    day = today(TZ)
    await run_usage_rollup(desk.ctx)
    first = await usage_rows(desk, day)

    await run_usage_rollup(desk.ctx)
    assert await usage_rows(desk, day) == first

    # 渠道停用后重算：渠道数变为 0，这一行被删除。
    await desk.sql("UPDATE channel_accounts SET status = 'disabled'")
    await rollup_day(desk.ctx.db, day, TZ)
    assert "channels" not in await usage_rows(desk, day)


async def usage(desk: Desk, headers: dict[str, str], **params: Any) -> httpx.Response:
    return await desk.client.get("/api/v1/usage", headers=headers, params=params)


async def test_tenant_admin_sees_own_daily_usage(desk: Desk) -> None:
    await serve_one_visitor(desk)
    day = today(TZ)
    await rollup_day(desk.ctx.db, day, TZ)
    # 另一个租户也有用量。
    other = await Desk(desk.app, desk.client, desk.im, desk.settings, desk.database_urls).open(
        "globex"
    )
    await serve_one_visitor(other)
    await rollup_day(desk.ctx.db, day, TZ)

    response = await usage(desk, desk.admin)

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["days"]) == 30
    assert (body["end"], body["timezone"]) == (day.isoformat(), "Asia/Shanghai")
    assert body["days"][-1]["values"]["messages_in"] == 1
    assert body["totals"]["messages_in"] == 1  # 不含 globex 的用量
    assert body["totals"]["seats"] == 2
    assert {m["key"]: m["kind"] for m in body["metrics"]}["seats"] == "snapshot"
    assert body["updated_at"] is not None

    alice_token = await login(desk.client, desk.code, "alice", STAFF_PASSWORD)
    assert (await usage(desk, bearer(alice_token))).status_code == 403


async def test_totals_sum_counts_and_keep_the_latest_snapshot(desk: Desk) -> None:
    day = today(TZ)
    rows = [
        (day - timedelta(days=2), "messages_in", 5),
        (day - timedelta(days=1), "messages_in", 7),
        (day - timedelta(days=2), "active_agents", 3),
        (day - timedelta(days=1), "active_agents", 2),
        (day - timedelta(days=2), "seats", 10),
        (day - timedelta(days=1), "seats", 8),
    ]
    for d, metric, value in rows:
        await desk.sql(
            "INSERT INTO usage_daily (tenant_id, day, metric, value) VALUES ($1, $2, $3, $4)",
            desk.tenant_id,
            d,
            metric,
            value,
        )

    body = (await usage(desk, desk.admin, start=(day - timedelta(days=2)).isoformat())).json()

    assert len(body["days"]) == 3
    assert (body["totals"]["messages_in"], body["totals"]["active_agents"]) == (12, 3)
    assert body["totals"]["seats"] == 8


async def test_usage_range_is_validated(desk: Desk) -> None:
    day = today(TZ)
    backwards = await usage(desk, desk.admin, start=day.isoformat(), end="2020-01-01")
    too_long = await usage(desk, desk.admin, start="2020-01-01", end=day.isoformat())
    assert (backwards.status_code, too_long.status_code) == (422, 422)


async def test_platform_sees_usage_of_every_tenant(desk: Desk) -> None:
    await serve_one_visitor(desk)
    await Desk(desk.app, desk.client, desk.im, desk.settings, desk.database_urls).open("globex")
    await run_usage_rollup(desk.ctx)
    await create_platform_admin(desk.app)
    ops = bearer(await platform_login(desk.client))

    listing = await desk.client.get("/platform/v1/usage", headers=ops)
    detail = await desk.client.get(f"/platform/v1/tenants/{desk.tenant_id}/usage", headers=ops)
    missing = await desk.client.get(f"/platform/v1/tenants/{uuid.uuid4()}/usage", headers=ops)
    staff = await desk.client.get("/platform/v1/usage", headers=desk.admin)

    assert listing.status_code == 200, listing.text
    totals = {item["code"]: item["totals"] for item in listing.json()["items"]}
    assert (totals["acme"]["messages_in"], totals["acme"]["seats"]) == (1, 2)
    assert (totals["globex"]["messages_in"], totals["globex"]["seats"]) == (0, 1)
    assert detail.json()["totals"]["file_bytes"] == 4096
    assert (missing.status_code, staff.status_code) == (404, 401)
