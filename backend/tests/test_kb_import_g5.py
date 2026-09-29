"""知识导入（设计文档 §12.1 冷启动）：文档按标题分节切片、上传 Word/PDF/Markdown/网页、
Excel 问答表、抓取官网帮助中心（同站同目录、robots.txt、每一跳检查地址）。"""

import base64
import io
import zipfile
from typing import Any
from xml.sax.saxutils import escape

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.kb import crawler, parsers
from app.modules.kb.importer import run_imports
from app.modules.kb.text import chunk_document
from tests.desk import Desk
from tests.fake_openim import FakeOpenIM
from tests.fake_web import FakeWeb
from tests.support import DatabaseUrls

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


def _zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def make_docx(blocks: list[tuple[str | None, str]], rows: list[list[str]]) -> bytes:
    """中文版 Word 的标题样式 ID 是 "1"、"2"，样式名是 heading 1、heading 2。"""
    body = ""
    for style, text in blocks:
        props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body += f'<w:p>{props}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
    table = "".join(
        "<w:tr>"
        + "".join(f"<w:tc><w:p><w:r><w:t>{escape(c)}</w:t></w:r></w:p></w:tc>" for c in row)
        + "</w:tr>"
        for row in rows
    )
    body += f"<w:tbl>{table}</w:tbl>"
    styles = "".join(
        f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/></w:style>'
        for sid, name in (("1", "heading 1"), ("2", "heading 2"), ("a3", "Title"))
    )
    return _zip(
        {
            "word/document.xml": f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
            "word/styles.xml": f'<w:styles xmlns:w="{W}">{styles}</w:styles>',
            "docProps/core.xml": (
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
                'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<dc:title>售后服务手册</dc:title></cp:coreProperties>"
            ),
        }
    )


def make_xlsx(rows: list[list[str | None]]) -> bytes:
    shared: list[str] = []
    cells = ""
    for r, row in enumerate(rows, start=1):
        cells += f'<row r="{r}">'
        for c, value in enumerate(row):
            if value is None:
                continue
            ref = f"{chr(ord('A') + c)}{r}"
            if value.startswith("inline:"):
                text = escape(value.removeprefix("inline:"))
                cells += f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'
                continue
            if value not in shared:
                shared.append(value)
            cells += f'<c r="{ref}" t="s"><v>{shared.index(value)}</v></c>'
        cells += "</row>"
    strings = "".join(f"<si><t>{escape(s)}</t></si>" for s in shared)
    return _zip(
        {
            "xl/workbook.xml": (
                f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>'
                '<sheet name="问答" sheetId="1" r:id="rId1"/></sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": (
                f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{R}/worksheet" '
                'Target="worksheets/faq.xml"/></Relationships>'
            ),
            "xl/sharedStrings.xml": f'<sst xmlns="{S}">{strings}</sst>',
            "xl/worksheets/faq.xml": (
                f'<worksheet xmlns="{S}"><sheetData>{cells}</sheetData></worksheet>'
            ),
        }
    )


def make_pdf(lines: list[str], title: str) -> bytes:
    """只有一页文字的最小 PDF（标准字体，英文）。"""
    content = "BT /F1 12 Tf 72 720 Td " + " ".join(f"({line}) Tj 0 -16 Td" for line in lines)
    content += " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        " /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Title ({title}) >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 6 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return out


async def _upload(
    desk: Desk, kind: str, filename: str, data: bytes, expect: int = 202, **extra: Any
) -> Any:
    response = await desk.client.post(
        "/api/v1/kb/imports",
        headers=desk.admin,
        json={
            "kind": kind,
            "filename": filename,
            "content_base64": base64.b64encode(data).decode(),
            **extra,
        },
    )
    assert response.status_code == expect, response.text
    return response.json()


async def _job(desk: Desk, job_id: str) -> Any:
    response = await desk.client.get(f"/api/v1/kb/imports/{job_id}", headers=desk.admin)
    assert response.status_code == 200, response.text
    return response.json()


async def _item(desk: Desk, item_id: str) -> Any:
    response = await desk.client.get(f"/api/v1/kb/items/{item_id}", headers=desk.admin)
    assert response.status_code == 200, response.text
    return response.json()


def test_documents_are_chunked_by_heading() -> None:
    text = (
        "开头的说明。\n\n# 退货\n\n七天无理由退货。\n\n## 运费\n\n质量问题由我们承担运费。"
        "\n\n# 换货\n\n换货流程同退货。"
    )
    assert chunk_document("售后手册", text) == [
        "售后手册\n开头的说明。",
        "售后手册 > 退货\n七天无理由退货。",
        "售后手册 > 退货 > 运费\n质量问题由我们承担运费。",
        "售后手册 > 换货\n换货流程同退货。",
    ]
    long = "\n\n".join(f"第{i}段。" + "内容" * 200 for i in range(3))
    chunks = chunk_document("手册", f"# 很长的一节\n\n{long}")
    assert len(chunks) >= 3
    assert all(c.startswith("手册 > 很长的一节\n") for c in chunks)
    # 超长文档拆成几条知识，后面几条开头带上所在的标题。
    parts = parsers.split_long("手册", f"# 第一章\n\n{long}", limit=600)
    assert len(parts) == 3
    assert parts[0][0] == "手册（第 1 部分）"
    assert all(p.startswith("# 第一章") for _, p in parts)
    assert all(len(p) <= 600 for _, p in parts)


def test_html_keeps_the_main_content() -> None:
    html = """<html><head><title>退货政策 - 帮助中心</title><style>p{}</style></head><body>
    <nav><a href="/docs/a.html">首页</a> 导航菜单</nav>
    <main><h1>退货政策</h1><p>七天无理由退货，商品需保持完好。</p>
    <h2>运费</h2><ul><li>质量问题：我们承担</li><li>个人原因：客户承担</li></ul>
    <table><tr><td>商品</td><td>期限</td></tr></table>
    <script>alert(1)</script></main><footer>版权所有</footer></body></html>"""
    doc = parsers.html_to_document(html)
    assert doc.title == "退货政策 - 帮助中心"
    assert doc.text == (
        "# 退货政策\n\n七天无理由退货，商品需保持完好。\n\n## 运费\n\n- 质量问题：我们承担"
        "\n\n- 个人原因：客户承担\n\n商品 | 期限"
    )
    assert doc.links == ["/docs/a.html"]
    assert "导航" not in doc.text and "版权" not in doc.text and "alert" not in doc.text


async def test_word_pdf_and_markdown_documents_are_imported(desk: Desk) -> None:
    docx = make_docx(
        [
            ("a3", "售后服务手册（2026 版）"),
            (None, "本手册说明退换货规则。"),
            ("1", "退货"),
            (None, "签收后七天内可以申请退货。"),
            ("2", "运费"),
            (None, "质量问题由我们承担运费。"),
        ],
        [["商品", "退货期限"], ["耳机", "7 天"]],
    )
    job = await _upload(desk, "document", "售后手册.docx", docx)
    assert (job["status"], job["kind"], job["source"]) == ("pending", "document", "售后手册.docx")
    assert await run_imports(desk.ctx) == 1
    assert await run_imports(desk.ctx) == 0
    done = await _job(desk, job["id"])
    assert (done["status"], done["result"]["created"]) == ("done", 1), done
    item = await _item(desk, done["result"]["item_ids"][0])
    assert (item["kind"], item["title"], item["source"], item["source_url"]) == (
        "doc",
        "售后服务手册",
        "document",
        "售后手册.docx",
    )
    assert item["status"] == "draft"
    assert item["content"] == (
        "本手册说明退换货规则。\n\n# 退货\n\n签收后七天内可以申请退货。\n\n## 运费\n\n"
        "质量问题由我们承担运费。\n\n商品 | 退货期限\n耳机 | 7 天"
    )
    # 发布后按标题分节切片。
    published = await desk.client.post(f"/api/v1/kb/items/{item['id']}/publish", headers=desk.admin)
    assert published.status_code == 200, published.text
    chunks = [
        r["text"]
        for r in await desk.sql(
            "SELECT text FROM kb_chunks WHERE item_id = $1 ORDER BY text", published.json()["id"]
        )
    ]
    assert (
        "售后服务手册 > 退货 > 运费\n质量问题由我们承担运费。\n商品 | 退货期限\n耳机 | 7 天"
        in chunks
    )
    # 上传的文件导入后删除；创建人收到站内信。
    [notice] = (await desk.client.get("/api/v1/notifications", headers=desk.admin)).json()["items"]
    assert (notice["kind"], notice["title"]) == ("kb_import", "知识导入完成：售后手册.docx")

    pdf = make_pdf(["Return policy", "Items can be returned within 7 days."], "Return Policy")
    space = await desk.client.post("/api/v1/kb/spaces", headers=desk.admin, json={"name": "售后"})
    job = await _upload(
        desk, "document", "policy.pdf", pdf, publish=True, space_id=space.json()["id"]
    )
    markdown = "# 发票说明\n\n在订单详情里申请电子发票。\n\n## 抬头\n\n支持个人和企业抬头。"
    md_job = await _upload(desk, "document", "invoice.md", markdown.encode(), visibility="agent")
    assert await run_imports(desk.ctx) == 2
    pdf_item = await _item(desk, (await _job(desk, job["id"]))["result"]["item_ids"][0])
    assert pdf_item["title"] == "Return Policy"
    assert "returned within 7 days" in pdf_item["content"]
    assert (pdf_item["status"], pdf_item["space_id"]) == ("published", space.json()["id"])
    md_item = await _item(desk, (await _job(desk, md_job["id"]))["result"]["item_ids"][0])
    assert (md_item["title"], md_item["visibility"]) == ("发票说明", "agent")


async def test_bad_uploads_are_rejected_or_fail(desk: Desk) -> None:
    await _upload(desk, "document", "旧格式.doc", b"x", expect=422)
    await _upload(desk, "excel", "问答.docx", b"x", expect=422)
    bad = await desk.client.post(
        "/api/v1/kb/imports",
        headers=desk.admin,
        json={"kind": "document", "filename": "a.txt", "content_base64": "不是base64"},
    )
    assert bad.status_code == 422
    alice = await desk.agent("alice", online=False)
    denied = await desk.client.post(
        "/api/v1/kb/imports",
        headers=alice.headers,
        json={"kind": "document", "filename": "a.txt", "content_base64": "YQ=="},
    )
    assert denied.status_code == 403

    broken = await _upload(desk, "document", "损坏.docx", b"not a zip file")
    empty = await _upload(desk, "document", "空白.txt", b"   \n  ")
    assert await run_imports(desk.ctx) == 2
    failed = await _job(desk, broken["id"])
    assert (failed["status"], failed["error"]) == ("failed", "文件已损坏或不是有效的 Office 文件")
    assert (await _job(desk, empty["id"]))["error"].startswith("没有从文件里读到文字")
    listed = await desk.client.get("/api/v1/kb/imports", headers=desk.admin)
    assert [j["status"] for j in listed.json()["items"]] == ["failed", "failed"]


async def test_excel_faq_sheets_are_imported(desk: Desk) -> None:
    data = make_xlsx(
        [
            ["标准问", "答案", "相似问", "分类"],
            ["发货要多久", "一般 48 小时内发货", "几天发货|什么时候发货", "物流"],
            ["可以开发票吗", "inline:可以，在订单详情里申请", None, None],
            ["只有问题", None, None, None],
        ]
    )
    job = await _upload(desk, "excel", "常见问题.xlsx", data, publish=True)
    await run_imports(desk.ctx)
    done = await _job(desk, job["id"])
    assert done["status"] == "done", done
    assert done["result"]["created"] == 2
    assert done["result"]["errors"] == ["第 4 行：标准问和答案不能为空"]
    first = await _item(desk, done["result"]["item_ids"][0])
    assert (first["kind"], first["questions"], first["category"], first["status"]) == (
        "faq",
        ["几天发货", "什么时候发货"],
        "物流",
        "published",
    )
    second = await _item(desk, done["result"]["item_ids"][1])
    assert second["content"] == "可以，在订单详情里申请"


HELP = "http://help.example.com"


def _site(web: FakeWeb, *, returns: str = "签收后七天内可以申请退货，商品需保持完好。") -> None:
    web.text(f"{HELP}/robots.txt", "User-agent: *\nDisallow: /docs/private/\n")
    web.page(
        f"{HELP}/docs/",
        '<html><body><h1>帮助中心</h1><a href="returns.html">退货</a>'
        '<a href="/docs/shipping.html#top">发货</a><a href="/docs/private/x.html">内部</a>'
        '<a href="/blog/post.html">博客</a><a href="https://other.example.com/">外站</a>'
        '<a href="old.html">旧地址</a><a href="copy.html">副本</a><a href="logo.png">图</a>'
        "</body></html>",
    )
    web.page(
        f"{HELP}/docs/returns.html",
        f"<html><head><title>退货政策</title></head><body><nav>导航</nav>"
        f"<article><h1>退货政策</h1><p>{returns}</p><h2>运费</h2>"
        "<p>质量问题由我们承担运费，个人原因由客户承担。</p></article></body></html>",
    )
    shipping = (
        "<html><head><title>发货说明</title></head><body><h1>发货说明</h1>"
        "<p>下单后 48 小时内发货，偏远地区顺延一到两天。</p></body></html>"
    )
    web.page(f"{HELP}/docs/shipping.html", shipping)
    web.page(f"{HELP}/docs/copy.html", shipping)
    web.redirect(f"{HELP}/docs/old.html", "/docs/new.html")
    web.page(
        f"{HELP}/docs/new.html",
        "<html><head><title>会员权益</title></head><body><h1>会员权益</h1>"
        "<p>会员享受免运费和优先发货，每月还有专属优惠券。</p></body></html>",
    )
    web.page(f"{HELP}/docs/private/x.html", "<p>内部资料，不应被抓取的内容写在这里。</p>")
    web.page(f"{HELP}/blog/post.html", "<p>博客文章，不在帮助中心目录下，不应被抓取。</p>")


async def test_help_center_is_crawled_and_refreshed(desk: Desk, fake_web: FakeWeb) -> None:
    _site(fake_web)
    response = await desk.client.post(
        "/api/v1/kb/imports/crawl",
        headers=desk.admin,
        json={"url": f"{HELP}/docs/", "publish": True},
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert (job["kind"], job["source"]) == ("crawl", f"{HELP}/docs/")
    await run_imports(desk.ctx)
    done = await _job(desk, job["id"])
    assert done["status"] == "done", done
    result = done["result"]
    assert (result["created"], result["pages"]) == (3, 3)
    assert f"{HELP}/docs/private/x.html：robots.txt 不允许抓取" in result["errors"]
    assert not any("blog" in u or "other.example" in u for u in fake_web.requests)
    items = {
        i["title"]: i
        for i in (
            await desk.client.get("/api/v1/kb/items", headers=desk.admin, params={"limit": 50})
        ).json()["items"]
    }
    assert set(items) == {"退货政策", "发货说明", "会员权益"}
    returns = items["退货政策"]
    assert (returns["source"], returns["source_url"], returns["status"]) == (
        "crawl",
        f"{HELP}/docs/returns.html",
        "published",
    )
    assert returns["content"].startswith("# 退货政策\n\n签收后七天内")
    assert "导航" not in returns["content"]
    assert items["会员权益"]["source_url"] == f"{HELP}/docs/new.html"

    # 再次抓取：变化的网页更新原来的知识（升级版本），没变的跳过。
    _site(fake_web, returns="签收后十五天内可以申请退货，商品需保持完好。")
    again = await desk.client.post(
        "/api/v1/kb/imports/crawl",
        headers=desk.admin,
        json={"url": f"{HELP}/docs/", "publish": True, "max_pages": 10},
    )
    await run_imports(desk.ctx)
    result = (await _job(desk, again.json()["id"]))["result"]
    assert (result["created"], result["updated"], result["skipped"]) == (0, 1, 2)
    refreshed = await _item(desk, returns["id"])
    assert "十五天" in refreshed["content"]
    assert refreshed["version"] == returns["version"] + 1


async def test_crawling_checks_every_hop(desk: Desk, fake_web: FakeWeb) -> None:
    ctx = desk.ctx
    private = await crawler.crawl(ctx, "https://127.0.0.1/docs/", max_pages=5, allow_private=False)
    assert private.pages == []
    assert private.errors == ["https://127.0.0.1/docs/：不能使用内网地址"]
    # 公网地址跳转到内网地址：跳转的那一跳被拦下。
    fake_web.redirect("https://93.184.216.34/docs/", "https://10.0.0.1/admin")
    redirected = await crawler.crawl(
        ctx, "https://93.184.216.34/docs/", max_pages=5, allow_private=False
    )
    assert redirected.pages == []
    assert redirected.errors == ["https://93.184.216.34/docs/：不能使用内网地址"]
    assert "https://10.0.0.1/admin" not in fake_web.requests

    failed = await desk.client.post(
        "/api/v1/kb/imports/crawl",
        headers=desk.admin,
        json={"url": f"{HELP}/missing/"},
    )
    await run_imports(desk.ctx)
    job = await _job(desk, failed.json()["id"])
    assert (job["status"], job["error"]) == (
        "failed",
        f"没有抓取到网页正文：{HELP}/missing/：HTTP 404",
    )
