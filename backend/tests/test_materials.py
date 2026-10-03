"""企业资料（设计文档 §36）：文件夹、浏览器直传 OSS（单次和分片）、文字资料、查看和下载地址、
封面、分享页、加入知识库、存储额度、病毒扫描、清理没有完成的上传、权限。

OSS 接的是模拟 OSS（tests/fake_oss.py）：它按 V4 签名校验平台签发的地址，浏览器的上传和下载在这里
用接到模拟 OSS 的 httpx 客户端代替。
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.context import AppContext
from app.core.config import Settings
from app.integrations.clamav import ClamAV
from app.integrations.oss import OssClient, OssConfig
from app.modules.kb.importer import run_imports
from app.modules.materials import jobs
from tests.desk import Agent, Desk
from tests.factories import create_knowledge_role
from tests.fake_clamd import EICAR, FakeClamd
from tests.fake_openim import FakeOpenIM
from tests.fake_oss import FakeOSS
from tests.support import DatabaseUrls

M = "/api/v1/materials"
# 分享页不用登录（不带员工令牌）。
ANONYMOUS = {"X-Anonymous": "1"}
PDF = b"%PDF-1.4 quote"


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
async def browser(fake_oss: FakeOSS) -> AsyncIterator[httpx.AsyncClient]:
    """浏览器：拿平台签发的地址直接访问（模拟）OSS。"""
    async with httpx.AsyncClient(transport=fake_oss.transport()) as client:
        yield client


@pytest.fixture
async def clamd() -> AsyncIterator[FakeClamd]:
    server = FakeClamd()
    await server.start()
    yield server
    await server.stop()


def ctx_of(app: FastAPI) -> AppContext:
    ctx: AppContext = app.state.ctx
    return ctx


async def call(
    desk: Desk,
    method: str,
    path: str,
    expected: int = 200,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    **body: Any,
) -> Any:
    response = await desk.client.request(
        method, path, headers=headers or desk.admin, json=body if body else None, params=params
    )
    assert response.status_code == expected, response.text
    return response.json() if response.content else None


async def upload(
    desk: Desk,
    browser: httpx.AsyncClient,
    filename: str,
    data: bytes,
    headers: dict[str, str] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """登记、按签名地址上传、完成（单次上传）。"""
    ticket = await call(
        desk, "POST", f"{M}/uploads", 201, headers, filename=filename, size=len(data), **fields
    )
    assert ticket["method"] == "single"
    put = await browser.put(ticket["upload_url"], content=data, headers=ticket["headers"])
    assert put.status_code == 200, put.text
    done: dict[str, Any] = await call(
        desk, "POST", f"{M}/{ticket['material']['id']}/complete", 200, headers
    )
    return done


def query_of(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


async def test_folders_are_a_tree_of_up_to_five_levels(desk: Desk) -> None:
    agent = await desk.agent("amy")
    config = await call(desk, "GET", f"{M}/config", headers=agent.headers)
    assert config["enabled"] and ".mp4" in config["extensions"]["video"]
    assert (config["can_manage"], config["can_import"]) == (False, False)
    await call(desk, "POST", f"{M}/folders", 403, agent.headers, name="视频")

    parent = None
    chain = []
    for level in range(1, 6):
        folder = await call(
            desk, "POST", f"{M}/folders", 201, name=f"第{level}层", parent_id=parent
        )
        chain.append(folder["id"])
        parent = folder["id"]
    await call(desk, "POST", f"{M}/folders", 422, name="第6层", parent_id=parent)
    await call(desk, "POST", f"{M}/folders", 409, name="第1层")
    # 不能移到自己的下级里；移到第一级后整棵子树变浅，下面又能再建一层。
    await call(desk, "PATCH", f"{M}/folders/{chain[0]}", 422, parent_id=chain[2])
    moved = await call(desk, "PATCH", f"{M}/folders/{chain[1]}", 200, parent_id=None, name="安装")
    assert (moved["parent_id"], moved["name"]) == (None, "安装")
    await call(desk, "POST", f"{M}/folders", 201, name="第6层", parent_id=parent)

    text = await call(desk, "POST", f"{M}/texts", 201, name="说明", body="正文", folder_id=chain[0])
    await call(desk, "DELETE", f"{M}/folders/{chain[0]}", 409)
    await call(desk, "DELETE", f"{M}/folders/{chain[1]}", 409)  # 还有下级
    folders = await call(desk, "GET", f"{M}/folders", headers=agent.headers)
    counts = {f["name"]: f["materials"] for f in folders["items"]}
    assert counts["第1层"] == 1 and folders["max_depth"] == 5 and folders["unfiled"] == 0
    await call(desk, "PATCH", f"{M}/{text['id']}", 200, folder_id=None)
    await call(desk, "DELETE", f"{M}/folders/{chain[0]}", 204)
    audit = await desk.sql(
        "SELECT action FROM audit_logs WHERE tenant_id = $1 AND resource_type = 'material_folder'",
        desk.tenant_id,
    )
    assert {a["action"] for a in audit} == {
        "material_folder.create",
        "material_folder.update",
        "material_folder.delete",
    }


async def test_single_upload_view_download_and_permissions(
    desk: Desk, browser: httpx.AsyncClient, fake_oss: FakeOSS
) -> None:
    agent = await desk.agent("amy")
    folder = await call(desk, "POST", f"{M}/folders", 201, name="报价")

    ticket = await call(
        desk,
        "POST",
        f"{M}/uploads",
        201,
        agent.headers,
        filename="报价单 2026.pdf",
        size=len(PDF),
        folder_id=folder["id"],
        tags=["报价", "报价", "2026"],
    )
    material = ticket["material"]
    assert ticket["method"] == "single" and ticket["headers"] == {"Content-Type": "application/pdf"}
    assert material["status"] == "uploading" and material["tags"] == ["报价", "2026"]
    assert material["name"] == "报价单 2026" and material["file_name"] == "报价单 2026.pdf"
    assert material["folder_path"] == "报价"
    key = f"{desk.tenant_id}/{material['id']}.pdf"
    # 签名包含 Content-Type：换成别的类型 OSS 拒绝。
    wrong = await browser.put(
        ticket["upload_url"], content=PDF, headers={"Content-Type": "text/html"}
    )
    assert wrong.status_code == 403
    # 上传中的只有上传的人看得到；还没传上去时不能完成。
    assert (await call(desk, "GET", M))["total"] == 0
    assert (await call(desk, "GET", M, headers=agent.headers))["total"] == 1
    await call(desk, "POST", f"{M}/{material['id']}/complete", 409, agent.headers)
    await call(desk, "POST", f"{M}/{material['id']}/complete", 404)
    put = await browser.put(ticket["upload_url"], content=PDF, headers=ticket["headers"])
    assert put.status_code == 200 and fake_oss.objects[key].data == PDF

    done = await call(desk, "POST", f"{M}/{material['id']}/complete", 200, agent.headers)
    assert done["status"] == "ready" and done["uploaded_at"] is not None
    assert done["scan_status"] is None  # 测试里没有配置病毒扫描
    page = await call(desk, "GET", M, params={"folder_id": folder["id"]})
    assert [m["id"] for m in page["items"]] == [material["id"]]
    assert page["used_bytes"] == len(PDF) and page["limit_bytes"] is None
    # 管理员（material:manage）能修改所有人的资料；另一位坐席不能。
    assert page["items"][0]["created_by_name"] == "Amy" and page["items"][0]["can_edit"]
    bob = await desk.agent("bob", online=False)
    seen_by_bob = await call(desk, "GET", f"{M}/{material['id']}", headers=bob.headers)
    assert not seen_by_bob["can_edit"]
    await call(desk, "PATCH", f"{M}/{material['id']}", 403, bob.headers, name="改名")
    assert page["tags"] == ["2026", "报价"]

    view = await call(desk, "GET", f"{M}/{material['id']}/link", params={"purpose": "view"})
    assert view["expires_in"] == 3600 and view["filename"] == "报价单 2026.pdf"
    assert query_of(view["url"])["response-content-type"] == "application/pdf"
    shown = await browser.get(view["url"])
    assert shown.content == PDF
    assert shown.headers["content-disposition"].startswith("inline;")
    download = await call(desk, "GET", f"{M}/{material['id']}/link", params={"purpose": "download"})
    got = await browser.get(download["url"])
    assert got.content == PDF and download["expires_in"] == 300
    assert got.headers["content-disposition"] == (
        'attachment; filename="___ 2026.pdf"; '
        "filename*=UTF-8''%E6%8A%A5%E4%BB%B7%E5%8D%95%202026.pdf"
    )
    counted = await call(desk, "GET", f"{M}/{material['id']}")
    assert (counted["views"], counted["downloads"]) == (1, 1)

    # 别人上传的：坐席不能改、不能删，管理员可以；Office 文档不能在线查看。
    other = await upload(desk, browser, "价目表.xlsx", b"xlsx")
    await call(desk, "PATCH", f"{M}/{other['id']}", 403, agent.headers, name="改名")
    await call(desk, "DELETE", f"{M}/{other['id']}", 403, agent.headers)
    await call(desk, "GET", f"{M}/{other['id']}/link", 422, params={"purpose": "view"})
    renamed = await call(
        desk,
        "PATCH",
        f"{M}/{material['id']}",
        200,
        name="2026 报价",
        tags=["报价"],
        description="新",
    )
    assert (renamed["name"], renamed["tags"], renamed["description"]) == (
        "2026 报价",
        ["报价"],
        "新",
    )
    searched = await call(desk, "GET", M, params={"q": "2026"})
    assert [m["id"] for m in searched["items"]] == [material["id"]]
    assert (await call(desk, "GET", M, params={"tag": "报价"}))["total"] == 1
    assert (await call(desk, "GET", M, params={"kind": "document"}))["total"] == 2
    await call(desk, "DELETE", f"{M}/{material['id']}", 204, agent.headers)
    assert key not in fake_oss.objects
    await call(desk, "GET", f"{M}/{material['id']}", 404)
    # 不支持的格式、超过单个文件上限。
    await call(desk, "POST", f"{M}/uploads", 422, filename="run.exe", size=10)
    await call(desk, "POST", f"{M}/uploads", 422, filename="a.pdf", size=201 * 1024 * 1024)
    await call(desk, "POST", f"{M}/uploads", 422, filename="a.mp4", size=3 * 1024**3)
    audit = await desk.sql(
        "SELECT action FROM audit_logs WHERE tenant_id = $1 AND resource_type = 'material'",
        desk.tenant_id,
    )
    assert {a["action"] for a in audit} == {"material.upload", "material.update", "material.delete"}


async def test_size_mismatch_and_abort(
    desk: Desk, browser: httpx.AsyncClient, fake_oss: FakeOSS
) -> None:
    ticket = await call(desk, "POST", f"{M}/uploads", 201, filename="a.png", size=10)
    # 登记 10 字节却传了 12 字节：合并时发现，文件和记录都删除。
    await browser.put(ticket["upload_url"], content=b"x" * 12, headers=ticket["headers"])
    await call(desk, "POST", f"{M}/{ticket['material']['id']}/complete", 422)
    await call(desk, "GET", f"{M}/{ticket['material']['id']}", 404)
    assert fake_oss.objects == {}

    second = await call(desk, "POST", f"{M}/uploads", 201, filename="b.png", size=3)
    await browser.put(second["upload_url"], content=b"png", headers=second["headers"])
    await call(desk, "DELETE", f"{M}/{second['material']['id']}/upload", 204)
    await call(desk, "GET", f"{M}/{second['material']['id']}", 404)
    assert fake_oss.objects == {}


async def test_multipart_upload_and_video_cover(
    desk: Desk,
    browser: httpx.AsyncClient,
    fake_oss: FakeOSS,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "material_multipart_threshold", 10)
    monkeypatch.setattr(settings, "material_part_size", 8)
    video = b"0123456789abcdefghij"
    ticket = await call(desk, "POST", f"{M}/uploads", 201, filename="演示.mp4", size=len(video))
    material_id = ticket["material"]["id"]
    assert (ticket["method"], ticket["upload_url"], ticket["part_count"]) == ("multipart", None, 3)
    assert len(fake_oss.uploads) == 1

    parts = await call(desk, "POST", f"{M}/{material_id}/parts", 200, part_numbers=[3, 1, 2])
    assert [(p["part_number"], p["size"]) for p in parts["parts"]] == [(1, 8), (2, 8), (3, 4)]
    await call(desk, "POST", f"{M}/{material_id}/parts", 422, part_numbers=[4])
    etags = []
    for part in parts["parts"]:
        start = (part["part_number"] - 1) * 8
        sent = await browser.put(part["url"], content=video[start : start + part["size"]])
        assert sent.status_code == 200, sent.text
        etags.append({"part_number": part["part_number"], "etag": sent.headers["etag"]})
    await call(desk, "POST", f"{M}/{material_id}/complete", 422, parts=etags[:2])
    wrong = [{**etags[0], "etag": '"0000"'}, *etags[1:]]
    await call(desk, "POST", f"{M}/{material_id}/complete", 422, parts=wrong)
    done = await call(desk, "POST", f"{M}/{material_id}/complete", 200, parts=etags)
    assert done["status"] == "ready" and done["size"] == len(video)
    assert fake_oss.objects[f"{desk.tenant_id}/{material_id}.mp4"].data == video
    assert fake_oss.uploads == {}

    # 封面：视频截帧（OSS 实时生成），签名时间取整点，一小时内地址不变。
    cover = done["cover_url"]
    assert query_of(cover)["x-oss-process"].startswith("video/snapshot,t_1000")
    assert query_of(cover)["x-oss-date"].endswith("0000Z")
    assert (await browser.get(cover)).headers["content-type"] == "image/png"
    again = await call(desk, "GET", M)
    assert again["items"][0]["cover_url"] == cover
    view = await call(desk, "GET", f"{M}/{material_id}/link", params={"purpose": "view"})
    assert query_of(view["url"])["response-content-type"] == "video/mp4"


async def test_text_materials_and_storage_quota(desk: Desk, browser: httpx.AsyncClient) -> None:
    agent = await desk.agent("amy")
    body = "# 安装说明\n\n" + "先断电，再拆下旧锁。" * 300
    text = await call(
        desk,
        "POST",
        f"{M}/texts",
        201,
        agent.headers,
        name="门锁安装说明",
        body=body,
        tags=["安装"],
    )
    assert (text["kind"], text["ext"], text["size"]) == ("text", ".md", len(body.encode()))
    assert len(text["excerpt"]) == 2000 and text["cover_url"] is None
    listed = (await call(desk, "GET", M))["items"][0]
    assert listed["excerpt"].endswith("…") and len(listed["excerpt"]) == 201
    # 正文能搜到（按开头的 2,000 字）。
    assert (await call(desk, "GET", M, params={"q": "旧锁"}))["total"] == 1
    read = await call(desk, "GET", f"{M}/{text['id']}/text")
    assert read["body"] == body
    assert (await call(desk, "GET", f"{M}/{text['id']}"))["views"] == 1
    bob = await desk.agent("bob", online=False)
    await call(desk, "PUT", f"{M}/{text['id']}/text", 403, bob.headers, body="改")
    updated = await call(desk, "PUT", f"{M}/{text['id']}/text", 200, agent.headers, body="# 新版")
    assert (updated["size"], updated["excerpt"]) == (len("# 新版".encode()), "# 新版")
    assert (await call(desk, "GET", f"{M}/{text['id']}/text"))["body"] == "# 新版"
    renamed = await call(desk, "PATCH", f"{M}/{text['id']}", 200, name="门锁安装说明 v2")
    assert renamed["file_name"] == "门锁安装说明 v2.md"
    pdf = await upload(desk, browser, "a.pdf", PDF)
    await call(desk, "PUT", f"{M}/{pdf['id']}/text", 422, body="x")

    # 套餐的企业资料存储（GB）：0 时什么都不能再存，计费页面显示用量。
    await desk.sql(
        "UPDATE tenants SET settings = settings || $2::jsonb WHERE id = $1",
        desk.tenant_id,
        '{"limits": {"material_gb": 0}}',
    )
    refused = await desk.client.post(
        f"{M}/texts", headers=desk.admin, json={"name": "x", "body": "x"}
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "plan_limit"
    await call(desk, "POST", f"{M}/uploads", 409, filename="b.pdf", size=1)
    page = await call(desk, "GET", M)
    assert page["limit_bytes"] == 0 and page["used_bytes"] == len("# 新版".encode()) + len(PDF)
    billing = await call(desk, "GET", "/api/v1/billing")
    usage = {x["key"]: x for x in billing["limits"]}["material_gb"]
    assert (usage["label"], usage["unit"], usage["limit"], usage["used"]) == (
        "企业资料存储",
        "GB",
        0,
        0,
    )


async def test_shares_open_without_login(
    desk: Desk, browser: httpx.AsyncClient, fake_oss: FakeOSS
) -> None:
    agent = await desk.agent("amy")
    text = await call(desk, "POST", f"{M}/texts", 201, name="保修政策", body="# 保修 12 个月")
    share = await call(desk, "POST", f"{M}/{text['id']}/shares", 201, agent.headers, days=3)
    token = query_of(share["url"])["share"]
    assert share["url"].startswith(desk.settings.widget_public_url) and share["active"]
    assert share["created_by_name"] == "Amy" and share["can_disable"]
    assert (await call(desk, "GET", f"{M}/{text['id']}"))["shares"] == 1

    opened = await call(desk, "GET", f"/api/v1/public/materials/{token}", headers=ANONYMOUS)
    assert opened["company"] and opened["text"] == "# 保修 12 个月"
    assert opened["view_url"] is None and opened["kind"] == "text"
    assert (await browser.get(opened["download_url"])).content == "# 保修 12 个月".encode()
    video = await upload(desk, browser, "演示.mp4", b"mp4")
    video_share = await call(desk, "POST", f"{M}/{video['id']}/shares", 201, days=1)
    shown = await call(
        desk,
        "GET",
        f"/api/v1/public/materials/{query_of(video_share['url'])['share']}",
        headers=ANONYMOUS,
    )
    assert shown["text"] is None and (await browser.get(shown["view_url"])).content == b"mp4"

    listed = await call(desk, "GET", f"{M}/{text['id']}/shares")
    assert listed["items"][0]["opens"] == 1 and listed["items"][0]["last_opened_at"]
    # 停用：管理员可以停用坐席建的；坐席不能停用管理员建的。
    await call(desk, "DELETE", f"{M}/shares/{video_share['id']}", 403, agent.headers)
    await call(desk, "DELETE", f"{M}/shares/{share['id']}", 204)
    await call(desk, "GET", f"/api/v1/public/materials/{token}", 404, headers=ANONYMOUS)
    disabled = (await call(desk, "GET", f"{M}/{text['id']}/shares"))["items"][0]
    assert disabled["disabled_at"] and not disabled["active"]
    # 到期的、不存在的令牌、上传中的资料。
    await desk.sql(
        "UPDATE material_shares SET expires_at = now() - interval '1 minute' WHERE id = $1",
        uuid.UUID(video_share["id"]),
    )
    vid_token = query_of(video_share["url"])["share"]
    await call(desk, "GET", f"/api/v1/public/materials/{vid_token}", 404, headers=ANONYMOUS)
    await call(desk, "GET", f"/api/v1/public/materials/{'x' * 32}", 404, headers=ANONYMOUS)
    pending = await call(desk, "POST", f"{M}/uploads", 201, filename="c.pdf", size=3)
    await call(desk, "POST", f"{M}/{pending['material']['id']}/shares", 409, days=1)
    # 删除资料时分享链接一起删除。
    await call(desk, "DELETE", f"{M}/{text['id']}", 204)
    gone = await desk.sql(
        "SELECT id FROM material_shares WHERE material_id = $1", uuid.UUID(text["id"])
    )
    assert gone == []
    audit = await desk.sql(
        "SELECT action FROM audit_logs WHERE tenant_id = $1 AND action LIKE 'material.share%'",
        desk.tenant_id,
    )
    assert sorted(a["action"] for a in audit) == [
        "material.share",
        "material.share",
        "material.share_disable",
    ]


async def test_documents_and_texts_can_join_the_knowledge_base(
    app: FastAPI, desk: Desk, browser: httpx.AsyncClient, fake_oss: FakeOSS
) -> None:
    agent = await desk.agent("amy")
    text = await call(
        desk, "POST", f"{M}/texts", 201, name="退换货规则", body="# 退换货\n七天无理由退货。"
    )
    await call(desk, "POST", f"{M}/{text['id']}/knowledge", 403, agent.headers)
    job = await call(desk, "POST", f"{M}/{text['id']}/knowledge", 201)
    assert (job["status"], job["source"], job["publish"]) == ("pending", "退换货规则.md", False)
    assert await run_imports(ctx_of(app)) == 1
    finished = await call(desk, "GET", f"/api/v1/kb/imports/{job['id']}")
    assert finished["status"] == "done" and finished["result"]["created"] == 1, finished
    [item] = await desk.sql(
        "SELECT title, content, status FROM kb_items WHERE id = $1",
        uuid.UUID(finished["result"]["item_ids"][0]),
    )
    assert "七天无理由退货" in item["content"] and item["status"] == "draft"
    # 文件留在资料里；压缩包不能加入知识库。
    assert f"{desk.tenant_id}/{text['id']}.md" in fake_oss.objects
    archive = await upload(desk, browser, "图纸.zip", b"PK")
    await call(desk, "POST", f"{M}/{archive['id']}/knowledge", 422)


async def test_virus_scan_blocks_infected_files(
    app: FastAPI,
    desk: Desk,
    browser: httpx.AsyncClient,
    fake_oss: FakeOSS,
    clamd: FakeClamd,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = ctx_of(app)
    port = int(clamd.server.sockets[0].getsockname()[1]) if clamd.server else 0
    monkeypatch.setattr(ctx, "clamav", ClamAV("127.0.0.1", port, timeout=5))
    monkeypatch.setattr(settings, "material_scan_max_bytes", 100)
    agent = await desk.agent("amy")
    clean = await upload(desk, browser, "说明.pdf", PDF, agent.headers)
    infected = await upload(desk, browser, "发票.txt", EICAR, agent.headers)
    large = await upload(desk, browser, "大文件.pdf", b"x" * 101, agent.headers)
    video = await upload(desk, browser, "演示.mp4", b"mp4", agent.headers)
    assert [m["scan_status"] for m in (clean, infected, large, video)] == [
        "pending",
        "pending",
        "pending",
        None,
    ]

    report = await jobs.run_material_scan(ctx)
    assert (report.scanned, report.infected, report.skipped, report.errors) == (3, 1, 1, 0)
    statuses = {
        m["id"]: (m["status"], m["scan_status"]) for m in (await call(desk, "GET", M))["items"]
    }
    assert statuses[clean["id"]] == ("ready", "clean")
    assert statuses[infected["id"]] == ("blocked", "infected")
    assert statuses[large["id"]] == ("ready", "skipped")
    assert f"{desk.tenant_id}/{infected['id']}.txt" not in fake_oss.objects
    await call(desk, "GET", f"{M}/{infected['id']}/link", 410, params={"purpose": "download"})
    await call(desk, "POST", f"{M}/{infected['id']}/shares", 410, days=1)
    [note] = await desk.sql(
        "SELECT title, link FROM staff_notifications"
        " WHERE staff_id = $1 AND kind = 'material_blocked'",
        agent.staff_id,
    )
    assert note["title"] == "资料含有病毒，已被拦截：发票"
    assert note["link"] == f"/materials?id={infected['id']}"
    [audit] = await desk.sql(
        "SELECT detail FROM audit_logs WHERE action = 'material.infected' AND tenant_id = $1",
        desk.tenant_id,
    )
    assert "Eicar" in audit["detail"]
    # 已拦截的资料不占空间，删除时不再访问 OSS。
    used = (await call(desk, "GET", M))["used_bytes"]
    assert used == len(PDF) + 101 + 3
    await call(desk, "DELETE", f"{M}/{infected['id']}", 204, agent.headers)
    assert (await jobs.run_material_scan(ctx)).scanned == 0


async def test_stale_uploads_are_cleaned_up(
    app: FastAPI,
    desk: Desk,
    browser: httpx.AsyncClient,
    fake_oss: FakeOSS,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "material_multipart_threshold", 10)
    single = await call(desk, "POST", f"{M}/uploads", 201, filename="a.png", size=3)
    await browser.put(single["upload_url"], content=b"png", headers=single["headers"])
    multi = await call(desk, "POST", f"{M}/uploads", 201, filename="b.mp4", size=30)
    fresh = await call(desk, "POST", f"{M}/uploads", 201, filename="c.png", size=3)
    await desk.sql(
        "UPDATE materials SET created_at = now() - interval '25 hours' WHERE id = ANY($1::uuid[])",
        [uuid.UUID(single["material"]["id"]), uuid.UUID(multi["material"]["id"])],
    )

    assert await jobs.cleanup_uploads(ctx_of(app)) == 2
    left = await desk.sql("SELECT id FROM materials WHERE tenant_id = $1", desk.tenant_id)
    assert [str(r["id"]) for r in left] == [fresh["material"]["id"]]
    assert fake_oss.objects == {} and fake_oss.uploads == {}


async def test_material_menu_and_unconfigured_storage(
    app: FastAPI, desk: Desk, agent_and_knowledge: tuple[Agent, Agent]
) -> None:
    agent, knowledge = agent_and_knowledge
    for headers in (agent.headers, knowledge.headers, desk.admin):
        me = await call(desk, "GET", "/api/v1/me", headers=headers)
        assert "materials" in me["console"]["menus"] and "material:use" in me["permissions"]
    ctx = ctx_of(app)
    original = ctx.oss
    ctx.oss = OssClient(
        OssConfig(endpoint="", region="", bucket="", access_key_id="", access_key_secret="")
    )
    try:
        assert (await call(desk, "GET", f"{M}/config"))["enabled"] is False
        assert (await call(desk, "GET", M))["total"] == 0
        refused = await desk.client.post(
            f"{M}/uploads", headers=desk.admin, json={"filename": "a.pdf", "size": 3}
        )
        assert refused.status_code == 409 and "阿里云 OSS" in refused.json()["error"]["message"]
        await call(desk, "POST", f"{M}/texts", 409, name="x", body="x")
        await call(desk, "GET", f"/api/v1/public/materials/{'x' * 32}", 404, headers=ANONYMOUS)
    finally:
        await ctx.oss.aclose()
        ctx.oss = original


@pytest.fixture
async def agent_and_knowledge(desk: Desk) -> tuple[Agent, Agent]:
    knowledge = await create_knowledge_role(desk.client, desk.admin_token)
    return await desk.agent("amy"), await desk.agent("kate", roles=[knowledge], online=False)


async def test_listing_with_another_tenants_folder_is_not_found(desk: Desk) -> None:
    other = await call(desk, "POST", f"{M}/folders", 201, name="自己的")
    await call(desk, "GET", M, params={"folder_id": other["id"]})
    missing = "00000000-0000-7000-8000-000000000000"
    await call(desk, "GET", M, 404, params={"folder_id": missing})
    unfiled = await call(desk, "POST", f"{M}/texts", 201, name="零散", body="x")
    filed = await call(
        desk, "POST", f"{M}/texts", 201, name="归档", body="x", folder_id=other["id"]
    )
    page = await call(desk, "GET", M, params={"unfiled": "true"})
    assert [m["id"] for m in page["items"]] == [unfiled["id"]]
    ordered = await call(desk, "GET", M, params={"sort": "name"})
    assert [m["name"] for m in ordered["items"]] == sorted([filed["name"], unfiled["name"]])
    assert datetime.fromisoformat(page["items"][0]["created_at"]) <= datetime.now(UTC) + timedelta(
        seconds=1
    )
