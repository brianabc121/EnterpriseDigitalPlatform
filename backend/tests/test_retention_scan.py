"""聊天记录保留期与附件病毒扫描。"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.context import AppContext
from app.core.config import Settings
from app.integrations.clamav import ClamAV, ScanError, parse
from app.modules.files.service import key_of_url
from app.modules.security.retention import run_retention
from app.modules.security.scanning import run_file_scan
from tests.desk import Agent, Desk, Visitor
from tests.fake_clamd import EICAR, FakeClamd
from tests.fake_openim import FakeOpenIM
from tests.fake_storage import FakeStorage
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
async def clamd() -> AsyncIterator[FakeClamd]:
    server = FakeClamd()
    await server.start()
    yield server
    await server.stop()


def _ctx(app: FastAPI) -> AppContext:
    ctx: AppContext = app.state.ctx
    return ctx


async def _send_file(
    desk: Desk,
    agent: Agent,
    visitor: Visitor,
    fake_storage: FakeStorage,
    data: bytes,
    name: str = "报价单.pdf",
) -> str:
    chat = await desk.session_of(visitor)
    upload = await desk.client.post(
        "/api/v1/uploads",
        headers=agent.headers,
        json={"filename": name, "content_type": "application/pdf", "size": len(data)},
    )
    file_url: str = upload.json()["file_url"]
    key = key_of_url(desk.settings, file_url)
    assert key is not None
    fake_storage.objects[f"/edp-files/{key}"] = (data, "application/pdf")
    sent = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages",
        headers=agent.headers,
        json={
            "client_msg_id": uuid.uuid4().hex,
            "type": "file",
            "attachment": {
                "url": file_url,
                "name": name,
                "size": len(data),
                "content_type": "application/pdf",
            },
        },
    )
    assert sent.status_code == 200, sent.text
    await desk.flush()
    return file_url


async def test_retention_policy_validation(desk: Desk) -> None:
    empty = await desk.client.get("/api/v1/tenant/retention", headers=desk.admin)
    assert empty.json() == {"messages_days": None, "files_days": None}
    short = await desk.client.put(
        "/api/v1/tenant/retention", headers=desk.admin, json={"messages_days": 7}
    )
    assert short.status_code == 422
    saved = await desk.client.put(
        "/api/v1/tenant/retention",
        headers=desk.admin,
        json={"messages_days": 180, "files_days": 30},
    )
    assert saved.status_code == 200, saved.text
    assert (await desk.client.get("/api/v1/tenant/retention", headers=desk.admin)).json() == {
        "messages_days": 180,
        "files_days": 30,
    }
    agent = await desk.agent("alice", online=False)
    denied = await desk.client.get("/api/v1/tenant/retention", headers=agent.headers)
    assert denied.status_code == 403


async def test_retention_deletes_expired_messages_and_files(
    app: FastAPI, desk: Desk, fake_storage: FakeStorage
) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    file_url = await _send_file(desk, alice, visitor, fake_storage, b"%PDF-1.4 quote")
    key = key_of_url(desk.settings, file_url)
    await desk.client.put(
        "/api/v1/tenant/retention",
        headers=desk.admin,
        json={"messages_days": 90, "files_days": 30},
    )
    now = datetime.now(UTC)

    # 还没到期：什么都不删。
    report = await run_retention(_ctx(app), now=now)
    assert (report.files_expired, report.messages_deleted) == (0, 0)

    # 文件到期：删除文件，消息保留并标记已过期。
    report = await run_retention(_ctx(app), now=now + timedelta(days=31))
    assert (report.files_expired, report.messages_deleted) == (1, 0)
    assert f"/edp-files/{key}" not in fake_storage.objects
    [message] = await desk.sql("SELECT content FROM messages WHERE content_type = 'file'")
    assert '"expired": true' in message["content"] and "url" not in message["content"]
    assert '"name": "报价单.pdf"' in message["content"]

    # 消息到期：全部删除，会话保留。
    report = await run_retention(_ctx(app), now=now + timedelta(days=91))
    assert report.messages_deleted >= 2
    assert await desk.sql("SELECT 1 FROM messages") == []
    assert len(await desk.sql("SELECT 1 FROM sessions")) == 1
    [audit] = await desk.sql(
        "SELECT detail FROM audit_logs WHERE action = 'tenant.retention_run' ORDER BY created_at"
        " DESC LIMIT 1"
    )
    assert '"messages_deleted"' in audit["detail"]


async def test_attachments_are_scanned_and_infected_files_blocked(
    app: FastAPI, desk: Desk, fake_storage: FakeStorage, clamd: FakeClamd, settings: Settings
) -> None:
    ctx = _ctx(app)
    port = int(clamd.server.sockets[0].getsockname()[1]) if clamd.server else 0
    ctx.clamav = ClamAV("127.0.0.1", port, timeout=5)
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "请发报价")
    clean_url = await _send_file(desk, alice, visitor, fake_storage, b"%PDF-1.4 clean")
    bad_url = await _send_file(desk, alice, visitor, fake_storage, EICAR, name="virus.pdf")

    report = await run_file_scan(ctx)
    assert (report.scanned, report.infected, report.errors) == (2, 1, 0)
    rows = await desk.sql(
        "SELECT content->>'scan' AS scan, content->>'blocked' AS blocked,"
        " content ? 'url' AS has_url FROM messages WHERE content_type = 'file' ORDER BY sent_at"
    )
    assert [(r["scan"], r["blocked"], r["has_url"]) for r in rows] == [
        ("clean", None, True),
        ("infected", "true", False),
    ]
    bad_key = key_of_url(settings, bad_url)
    assert f"/edp-files/{bad_key}" not in fake_storage.objects
    [scan] = await desk.sql(
        "SELECT status, signature FROM file_scans WHERE object_key = $1", bad_key
    )
    assert (scan["status"], scan["signature"]) == ("infected", "Eicar-Test-Signature")
    [audit] = await desk.sql("SELECT detail FROM audit_logs WHERE action = 'file.infected'")
    assert "Eicar-Test-Signature" in audit["detail"]

    # 下载链接：干净的文件照常跳转，感染的返回 410。
    ok = await desk.client.get(httpx.URL(clean_url).raw_path.decode())
    assert ok.status_code == 302
    gone = await desk.client.get(httpx.URL(bad_url).raw_path.decode())
    assert gone.status_code == 410
    # 已经扫描过的不再扫描。
    assert (await run_file_scan(ctx)).scanned == 0


async def test_scanning_waits_when_clamd_is_down(
    app: FastAPI, desk: Desk, fake_storage: FakeStorage
) -> None:
    ctx = _ctx(app)
    ctx.clamav = ClamAV("127.0.0.1", 1, timeout=1)
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "请发报价")
    await _send_file(desk, alice, visitor, fake_storage, b"%PDF-1.4")
    report = await run_file_scan(ctx)
    assert (report.scanned, report.errors) == (0, 1)
    [row] = await desk.sql(
        "SELECT content ? 'scan' AS scanned FROM messages WHERE content_type = 'file'"
    )
    assert row["scanned"] is False
    # 没有配置扫描时不处理。
    ctx.clamav = None
    assert (await run_file_scan(ctx)).scanned == 0


def test_parse_clamd_replies() -> None:
    assert parse("stream: OK\0").clean
    infected = parse("stream: Win.Test.EICAR_HDB-1 FOUND\0")
    assert (infected.clean, infected.signature) == (False, "Win.Test.EICAR_HDB-1")
    with pytest.raises(ScanError):
        parse("INSTREAM size limit exceeded. ERROR\0")
