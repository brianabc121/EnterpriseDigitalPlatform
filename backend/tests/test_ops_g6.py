"""G6 运维：事件死信的查看、重新处理与丢弃；IM 发件箱失败与积压操作的查看、重试与放弃。"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.events.bus import DEAD_LETTER_STREAM, Event, EventProcessor
from tests.desk import Desk
from tests.factories import bearer, create_platform_admin, platform_login
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


@pytest.fixture
async def ops(desk: Desk) -> dict[str, str]:
    await create_platform_admin(desk.app)
    return bearer(await platform_login(desk.client))


async def dead_letter(desk: Desk, n: int, type_: str = "test.flaky") -> None:
    """发布一个事件，处理函数一直失败，事件进入死信。"""

    async def broken(_: Event) -> None:
        raise RuntimeError(f"数据库暂时不可用 #{n}")

    await desk.ctx.bus.publish(
        Event(type=type_, tenant_id=desk.tenant_id, key=f"room-{n}", data={"n": n})
    )
    await EventProcessor(desk.ctx.bus, {type_: broken}, consumer="t").process_available()


async def test_dead_letters_can_be_inspected_retried_and_discarded(
    desk: Desk, ops: dict[str, str]
) -> None:
    for n in range(3):
        await dead_letter(desk, n)
    await dead_letter(desk, 9, type_="test.other")

    listed = await desk.client.get(
        "/platform/v1/ops/dead-letters", headers=ops, params={"type": "test.flaky"}
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] == 4
    assert [i["data"]["n"] for i in body["items"]] == [2, 1, 0]  # 新的在前
    first = body["items"][0]
    assert (first["tenant_code"], first["key"]) == (desk.code, "room-2")
    assert "数据库暂时不可用 #2" in first["error"]
    # 翻页。
    page = await desk.client.get(
        "/platform/v1/ops/dead-letters", headers=ops, params={"type": "test.flaky", "limit": 2}
    )
    assert [i["data"]["n"] for i in page.json()["items"]] == [2, 1]
    rest = await desk.client.get(
        "/platform/v1/ops/dead-letters",
        headers=ops,
        params={"type": "test.flaky", "limit": 2, "before": page.json()["next_before"]},
    )
    assert [i["data"]["n"] for i in rest.json()["items"]] == [0]

    # 问题修复后重新处理：重新发布后处理成功，死信里不再有。
    seen: list[int] = []

    async def fixed(e: Event) -> None:
        seen.append(e.data["n"])

    ids = [i["id"] for i in body["items"]]
    retried = await desk.client.post(
        "/platform/v1/ops/dead-letters/retry", headers=ops, json={"ids": ids}
    )
    assert retried.json() == {"done": 3, "skipped": 0}
    await EventProcessor(desk.ctx.bus, {"test.flaky": fixed}, consumer="t").process_available()
    assert sorted(seen) == [0, 1, 2]

    [other] = (await desk.client.get("/platform/v1/ops/dead-letters", headers=ops)).json()["items"]
    discarded = await desk.client.post(
        "/platform/v1/ops/dead-letters/discard", headers=ops, json={"ids": [other["id"], ids[0]]}
    )
    assert discarded.json() == {"done": 1, "skipped": 1}
    assert await desk.ctx.redis.xlen(DEAD_LETTER_STREAM) == 0

    audit = await desk.sql(
        "SELECT action, detail FROM audit_logs WHERE action LIKE 'platform.dead_letters.%' "
        "ORDER BY created_at"
    )
    assert [a["action"] for a in audit] == [
        "platform.dead_letters.retry",
        "platform.dead_letters.discard",
    ]
    assert json.loads(audit[0]["detail"])["done"] == 3


async def test_failed_and_stuck_im_ops_can_be_retried_or_discarded(
    desk: Desk, ops: dict[str, str]
) -> None:
    visitor = await desk.visitor()
    now = datetime.now(UTC)
    rows = await desk.sql(
        """
        INSERT INTO im_ops (tenant_id, room_id, op, payload, status, attempts,
                            next_attempt_at, last_error, created_at, done_at)
        VALUES ($1, $2, 'notice', '{"text": "客户看得到的提示"}', 'failed', 12, $3,
                'OpenIM unavailable', $3, $3),
               ($1, $2, 'signal', '{"staff_id": "x", "signal": {"type": "assigned"}}',
                'failed', 1, $3, 'offline', $3, $3),
               ($1, $2, 'kick', '{"staff_id": "00000000-0000-0000-0000-000000000001"}',
                'pending', 3, $4, 'timeout', $4, NULL)
        RETURNING id
        """,
        desk.tenant_id,
        visitor.room_id,
        now - timedelta(minutes=30),
        now - timedelta(minutes=20),
    )
    notice_id, signal_id, kick_id = (r["id"] for r in rows)

    failed = await desk.client.get("/platform/v1/ops/im-ops", headers=ops)
    assert failed.status_code == 200, failed.text
    body = failed.json()
    assert body["counts"] == {"pending": 1, "stuck": 1, "failed": 2}
    by_id = {i["id"]: i for i in body["items"]}
    assert set(by_id) == {notice_id, signal_id}
    # 不显示消息正文。
    assert by_id[notice_id]["detail"] == {}
    assert "客户看得到的提示" not in failed.text
    assert (by_id[notice_id]["retryable"], by_id[signal_id]["retryable"]) == (True, False)
    assert by_id[notice_id]["tenant_code"] == desk.code

    stuck = await desk.client.get(
        "/platform/v1/ops/im-ops", headers=ops, params={"status": "stuck"}
    )
    assert [i["id"] for i in stuck.json()["items"]] == [kick_id]

    # 放弃积压的操作（失败的操作不能再放弃）。
    discarded = await desk.client.post(
        "/platform/v1/ops/im-ops/discard", headers=ops, json={"ids": [kick_id, notice_id]}
    )
    assert discarded.json() == {"done": 1, "skipped": 1}
    [kick] = await desk.sql("SELECT status, last_error FROM im_ops WHERE id = $1", kick_id)
    assert (kick["status"], kick["last_error"]) == ("failed", "运营人员已放弃")

    # 失败的提示重新排队并立即执行；在线信令不能重试。
    retried = await desk.client.post(
        "/platform/v1/ops/im-ops/retry", headers=ops, json={"ids": [notice_id, signal_id]}
    )
    assert retried.json() == {"done": 1, "skipped": 1}
    [notice] = await desk.sql("SELECT status, attempts FROM im_ops WHERE id = $1", notice_id)
    assert (notice["status"], notice["attempts"]) == ("done", 0)
    assert "客户看得到的提示" in desk.room_texts(visitor)
    audit = await desk.sql(
        "SELECT action, detail FROM audit_logs WHERE action LIKE 'platform.im_ops.%' "
        "ORDER BY created_at"
    )
    assert [a["action"] for a in audit] == ["platform.im_ops.discard", "platform.im_ops.retry"]
    assert json.loads(audit[1]["detail"])["tenants"] == [str(desk.tenant_id)]


async def test_ops_endpoints_are_for_platform_operators_only(desk: Desk) -> None:
    for method, path, body in (
        ("GET", "/platform/v1/ops/dead-letters", None),
        ("POST", "/platform/v1/ops/dead-letters/retry", {"ids": ["1-0"]}),
        ("POST", "/platform/v1/ops/dead-letters/discard", {"ids": ["1-0"]}),
        ("GET", "/platform/v1/ops/im-ops", None),
        ("POST", "/platform/v1/ops/im-ops/retry", {"ids": [1]}),
        ("POST", "/platform/v1/ops/im-ops/discard", {"ids": [1]}),
    ):
        response = await desk.client.request(method, path, headers=desk.admin, json=body)
        assert response.status_code == 401, (path, response.text)


async def test_dead_letter_ids_are_validated(desk: Desk, ops: dict[str, str]) -> None:
    response = await desk.client.post(
        "/platform/v1/ops/dead-letters/retry", headers=ops, json={"ids": ["0-0 OR 1"]}
    )
    assert response.status_code == 422
    empty = await desk.client.post("/platform/v1/ops/im-ops/retry", headers=ops, json={"ids": []})
    assert empty.status_code == 422
    unknown = await desk.client.post(
        "/platform/v1/ops/dead-letters/retry",
        headers=ops,
        json={"ids": [f"{int(uuid.uuid4().int % 10**12)}-0"]},
    )
    assert unknown.json() == {"done": 0, "skipped": 1}
