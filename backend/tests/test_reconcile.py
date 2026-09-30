from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.conversation.reconcile import reconcile_all
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_openim_hooks import deliver, new_visitor, rows


async def reconcile(app: FastAPI, **kwargs: Any) -> Any:
    return await reconcile_all(app.state.db, app.state.im, **kwargs)


async def test_lost_callbacks_are_recovered_with_their_seq(
    app: FastAPI, client: httpx.AsyncClient, fake_im: FakeOpenIM, database_urls: DatabaseUrls
) -> None:
    visitor = await new_visitor(app, client)
    im = visitor["im"]
    for text in ("你好", "在吗", "想咨询价格"):
        fake_im.send_as(im["user_id"], im["group_id"], text)
    # 回调全部丢失：一条也不投递。

    report = await reconcile(app)

    stored = await rows(
        database_urls, "SELECT text_plain, im_seq, source FROM messages ORDER BY im_seq"
    )
    # seq 1 是建群通知，不入库。
    assert [tuple(r) for r in stored] == [
        ("你好", 2, "reconcile"),
        ("在吗", 3, "reconcile"),
        ("想咨询价格", 4, "reconcile"),
    ]
    assert (report.rooms_behind, report.recovered, report.backfilled) == (1, 3, 0)
    [room] = await rows(database_urls, "SELECT synced_seq FROM rooms")
    assert room["synced_seq"] == 4


async def test_delivered_messages_only_get_their_seq_backfilled(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> None:
    visitor = await new_visitor(app, client)
    im = visitor["im"]
    fake_im.send_as(im["user_id"], im["group_id"], "第一条")
    await deliver(client, settings, fake_im.callbacks)
    fake_im.send_as(im["user_id"], im["group_id"], "第二条（回调丢失）")

    report = await reconcile(app)

    stored = await rows(
        database_urls, "SELECT text_plain, im_seq, source FROM messages ORDER BY im_seq"
    )
    assert [tuple(r) for r in stored] == [
        ("第一条", 2, "webhook"),
        ("第二条（回调丢失）", 3, "reconcile"),
    ]
    assert (report.recovered, report.backfilled) == (1, 1)


async def test_reconcile_is_idempotent(
    app: FastAPI, client: httpx.AsyncClient, fake_im: FakeOpenIM, database_urls: DatabaseUrls
) -> None:
    visitor = await new_visitor(app, client)
    fake_im.send_as(visitor["im"]["user_id"], visitor["im"]["group_id"], "你好")
    await reconcile(app)
    pulls_before = fake_im.calls.count("/msg/pull_msg_by_seq")

    report = await reconcile(app)

    assert (report.rooms_checked, report.rooms_behind, report.recovered) == (1, 0, 0)
    assert fake_im.calls.count("/msg/pull_msg_by_seq") == pulls_before
    assert len(await rows(database_urls, "SELECT 1 FROM messages")) == 1


async def test_long_gaps_are_pulled_in_pages(
    app: FastAPI, client: httpx.AsyncClient, fake_im: FakeOpenIM, database_urls: DatabaseUrls
) -> None:
    visitor = await new_visitor(app, client)
    for i in range(250):
        fake_im.send_as(visitor["im"]["user_id"], visitor["im"]["group_id"], f"消息 {i}")

    report = await reconcile(app)

    assert report.recovered == 250
    assert fake_im.calls.count("/msg/pull_msg_by_seq") == 3
    [room] = await rows(database_urls, "SELECT synced_seq FROM rooms")
    assert room["synced_seq"] == 251


async def test_inactive_rooms_are_not_checked(
    app: FastAPI, client: httpx.AsyncClient, fake_im: FakeOpenIM
) -> None:
    visitor = await new_visitor(app, client)
    fake_im.send_as(visitor["im"]["user_id"], visitor["im"]["group_id"], "你好")

    report = await reconcile(app, now=datetime.now(UTC) + timedelta(days=8))

    assert (report.tenants, report.rooms_checked) == (1, 0)


async def test_openim_outage_is_reported_and_retried_next_round(
    app: FastAPI, client: httpx.AsyncClient, fake_im: FakeOpenIM, database_urls: DatabaseUrls
) -> None:
    visitor = await new_visitor(app, client)
    fake_im.send_as(visitor["im"]["user_id"], visitor["im"]["group_id"], "你好")
    fake_im.down = True

    failed = await reconcile(app)

    assert (failed.errors, failed.recovered) == (1, 0)
    fake_im.down = False
    recovered = await reconcile(app)
    assert (recovered.errors, recovered.recovered) == (0, 1)
    assert len(await rows(database_urls, "SELECT 1 FROM messages")) == 1
