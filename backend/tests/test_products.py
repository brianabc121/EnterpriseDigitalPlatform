"""商品库（设计文档 §25.2）：维护与检索、成本价权限、Excel 模板与导入、商品缺口。"""

import base64
import io
import zipfile
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.xlsx import Column, Sheet, write_workbook
from app.modules.kb.parsers import parse_sheet
from app.modules.products import service as product_service
from tests.desk import Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls

HEADER = [
    "名称*",
    "代码",
    "型号",
    "规格",
    "分类",
    "图片URL",
    "成本价",
    "建议零售价",
    "别名",
    "备注",
    "状态",
]
EXAMPLE = [
    "智能门锁 X1",
    "LOCK-X1",
    "X1",
    "黑色",
    "智能家居/门锁",
    "https://example.com/images/lock-x1.jpg",
    "800",
    "1299",
    "指纹锁、电子锁",
    "示例行，导入前请删除",
    "上架",
]


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def create_product(desk: Desk, **body: Any) -> dict[str, Any]:
    response = await desk.client.post("/api/v1/products", headers=desk.admin, json=body)
    assert response.status_code == 201, response.text
    product: dict[str, Any] = response.json()
    return product


def workbook(rows: list[list[str]]) -> str:
    data = write_workbook([Sheet("商品", [Column(t) for t in HEADER], rows=list(rows))])
    return base64.b64encode(data).decode()


async def test_catalog_search_and_cost_price_permission(desk: Desk) -> None:
    created = await create_product(
        desk,
        code="LOCK-X1",
        name="智能门锁 X1",
        model="X1",
        spec="黑色",
        category="智能家居 / 门锁",
        retail_price="1299",
        cost_price="800",
        aliases=["指纹锁", "指纹锁", " 电子锁 "],
    )
    assert (created["retail_price"], created["cost_price"], created["category"]) == (
        "1299.00",
        "800.00",
        "智能家居/门锁",
    )
    assert created["aliases"] == ["指纹锁", "电子锁"]
    duplicate = await desk.client.post(
        "/api/v1/products", headers=desk.admin, json={"code": "LOCK-X1", "name": "另一个"}
    )
    assert duplicate.status_code == 409

    # 坐席看得到商品和建议零售价，看不到成本价；不能维护商品库。
    alice = await desk.agent("alice", online=False)
    listed = await desk.client.get("/api/v1/products", headers=alice.headers)
    [item] = listed.json()["items"]
    assert (item["cost_price"], item["cost_visible"], item["retail_price"]) == (
        None,
        False,
        "1299.00",
    )
    denied = await desk.client.put(
        f"/api/v1/products/{created['id']}",
        headers=alice.headers,
        json={"name": "改名", "cost_price": "1"},
    )
    assert denied.status_code == 403

    # 检索：代码、型号精确匹配得满分；别名等关键词也能找到；找不到的不返回。
    async def search(q: str, **extra: Any) -> list[dict[str, Any]]:
        response = await desk.client.get(
            "/api/v1/products/search", headers=alice.headers, params={"q": q, **extra}
        )
        assert response.status_code == 200, response.text
        found: list[dict[str, Any]] = response.json()["items"]
        return found

    assert [(c["product"]["code"], c["score"]) for c in await search("lock-x1")] == [
        ("LOCK-X1", 1.0)
    ]
    assert [c["product"]["name"] for c in await search("指纹锁")] == ["智能门锁 X1"]
    assert await search("空调") == []

    # 下架的商品默认不出现在检索结果里。
    updated = await desk.client.put(
        f"/api/v1/products/{created['id']}",
        headers=desk.admin,
        json={
            **{k: created[k] for k in ("code", "name", "model", "spec", "category", "aliases")},
            "status": "off",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["cost_price"] == "800.00"  # 不传成本价时保持原值
    assert await search("指纹锁") == []
    assert len(await search("指纹锁", include_off="true")) == 1
    categories = await desk.client.get("/api/v1/products/categories", headers=alice.headers)
    assert categories.json()["items"] == ["智能家居/门锁"]


async def test_template_import_preview_confirm_and_result(desk: Desk) -> None:
    await create_product(
        desk, code="LOCK-X1", name="智能门锁 X1", retail_price="1299", cost_price="800"
    )

    # 模板：表头（必填的带 *）、示例行、每列的填写说明，另有"填写说明"工作表。
    template = await desk.client.get("/api/v1/products/template", headers=desk.admin)
    assert template.status_code == 200
    assert template.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    rows = parse_sheet("template.xlsx", template.content)
    assert rows[0] == HEADER
    assert rows[1] == EXAMPLE
    with zipfile.ZipFile(io.BytesIO(template.content)) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        names = archive.read("xl/workbook.xml").decode()
    assert 'showInputMessage="1"' in sheet and 'type="decimal"' in sheet
    assert "填写说明" in names

    upload = workbook(
        [
            EXAMPLE,  # 原样保留的示例行：跳过
            ["智能门锁 X2", "LOCK-X2", "X2", "银色", "智能家居/门锁", "https://example.com/x2.jpg",
             "900", "1,599", "电子锁、密码锁", "", "上架"],
            ["", "LOCK-X3", "", "", "", "", "", "100", "", "", ""],
            ["智能门锁 X4", "LOCK-X4", "", "", "", "", "", "abc", "", "", ""],
            ["智能门锁 X2 重复", "LOCK-X2", "", "", "", "", "", "1", "", "", ""],
            ["门铃 D1", "", "D1", "", "", "ftp://example.com/d1.jpg", "", "199", "", "", ""],
            ["智能门锁 X1", "LOCK-X1", "", "", "", "", "", "1199", "", "", "下架"],
        ]
    )  # fmt: skip
    alice = await desk.agent("alice", online=False)
    denied = await desk.client.post(
        "/api/v1/products/imports",
        headers=alice.headers,
        json={"filename": "商品.xlsx", "content_base64": upload},
    )
    assert denied.status_code == 403
    preview = await desk.client.post(
        "/api/v1/products/imports",
        headers=desk.admin,
        json={"filename": "商品.xlsx", "content_base64": upload},
    )
    assert preview.status_code == 201, preview.text
    body = preview.json()
    assert (body["status"], body["total"], body["will_create"], body["will_update"]) == (
        "preview",
        7,
        1,
        1,
    )
    problems = {r["row"]: (r["action"], r["problems"]) for r in body["rows"]}
    assert problems[2] == ("skip", ["模板里的示例行，已跳过"])
    assert problems[3] == ("create", [])
    assert problems[4] == ("skip", ["名称必填"])
    assert problems[5] == ("skip", ["建议零售价只能填数字"])
    assert problems[6] == ("skip", ["代码与第 3 行重复"])
    assert problems[7] == ("skip", ["图片URL 要以 http:// 或 https:// 开头"])
    assert problems[8][0] == "update"

    confirmed = await desk.client.post(
        f"/api/v1/products/imports/{body['id']}/confirm", headers=desk.admin
    )
    assert confirmed.status_code == 200, confirmed.text
    assert (
        confirmed.json()["created"],
        confirmed.json()["updated"],
        confirmed.json()["skipped"],
    ) == (
        1,
        1,
        5,
    )
    again = await desk.client.post(
        f"/api/v1/products/imports/{body['id']}/confirm", headers=desk.admin
    )
    assert again.status_code == 409

    products = {
        p["code"]: p
        for p in (await desk.client.get("/api/v1/products", headers=desk.admin)).json()["items"]
    }
    # 按代码更新：填了的列更新，留空的列（成本价）保持原值。
    assert (products["LOCK-X1"]["retail_price"], products["LOCK-X1"]["cost_price"]) == (
        "1199.00",
        "800.00",
    )
    assert products["LOCK-X1"]["status"] == "off"
    assert products["LOCK-X2"]["retail_price"] == "1599.00"
    assert products["LOCK-X2"]["aliases"] == ["电子锁", "密码锁"]
    [row] = await desk.sql("SELECT terms FROM products WHERE code = 'LOCK-X2'")
    assert "密码" in row["terms"]
    [audit] = await desk.sql("SELECT detail FROM audit_logs WHERE action = 'product.import'")
    assert audit["detail"]

    result = await desk.client.get(
        f"/api/v1/products/imports/{body['id']}/result", headers=desk.admin
    )
    result_rows = parse_sheet("result.xlsx", result.content)
    assert result_rows[0] == ["行号", "名称", "代码", "结果", "说明"]
    outcomes = {r[0]: r[3] for r in result_rows[1:]}
    assert (outcomes["3"], outcomes["8"], outcomes["4"]) == ("新增", "更新", "跳过")


async def test_bad_files_and_product_gaps(desk: Desk) -> None:
    bad = await desk.client.post(
        "/api/v1/products/imports",
        headers=desk.admin,
        json={"filename": "商品.xls", "content_base64": base64.b64encode(b"x").decode()},
    )
    assert bad.status_code == 422
    no_name = await desk.client.post(
        "/api/v1/products/imports",
        headers=desk.admin,
        json={
            "filename": "商品.csv",
            "content_base64": base64.b64encode("型号,价格\nX1,1\n".encode()).decode(),
        },
    )
    assert no_name.status_code == 422
    assert "名称" in no_name.text

    # 商品缺口：客户问到、商品库里没有的，按规范化后的说法累计。
    async with desk.ctx.db.tenant_session(desk.tenant_id) as session:
        for query in ("小米电视", "小米 电视！", "扫地机器人"):
            await product_service.record_gap(session, desk.tenant_id, query)
        await session.commit()
    gaps = (await desk.client.get("/api/v1/products/gaps", headers=desk.admin)).json()["items"]
    assert [(g["term"], g["count"]) for g in gaps] == [("小米电视", 2), ("扫地机器人", 1)]
    resolved = await desk.client.post(
        f"/api/v1/products/gaps/{gaps[0]['id']}/resolve", headers=desk.admin
    )
    assert resolved.status_code == 204
    gaps = (await desk.client.get("/api/v1/products/gaps", headers=desk.admin)).json()["items"]
    assert [g["term"] for g in gaps] == ["扫地机器人"]


async def test_export_round_trips_and_includes_cost_only_with_permission(desk: Desk) -> None:
    await create_product(
        desk,
        code="LOCK-X1",
        name="智能门锁 X1",
        spec="黑色",
        retail_price="1299",
        cost_price="800",
        aliases=["指纹锁"],
    )
    await create_product(desk, name="门铃 D1", retail_price="199", status="off")

    exported = await desk.client.get("/api/v1/products/export", headers=desk.admin)
    assert exported.status_code == 200, exported.text
    rows = parse_sheet("products.xlsx", exported.content)
    assert rows[0] == HEADER
    by_name = {r[0]: r for r in rows[1:]}
    assert by_name["智能门锁 X1"][6:9] == ["800.00", "1299.00", "指纹锁"]
    assert by_name["门铃 D1"][-1] == "下架"
    [audit] = await desk.sql("SELECT detail FROM audit_logs WHERE action = 'product.export'")
    assert '"cost": true' in audit["detail"]
    # 只导出上架的；筛选条件与列表相同。
    on_shelf = await desk.client.get(
        "/api/v1/products/export", headers=desk.admin, params={"status": "on"}
    )
    assert [r[0] for r in parse_sheet("on.xlsx", on_shelf.content)[1:]] == ["智能门锁 X1"]

    # 改完直接再导入：按代码更新。
    rows[1 if rows[1][0] == "智能门锁 X1" else 2][7] = "1199"
    upload = base64.b64encode(
        write_workbook([Sheet("商品", [Column(t) for t in rows[0]], rows=rows[1:])])
    ).decode()
    preview = await desk.client.post(
        "/api/v1/products/imports",
        headers=desk.admin,
        json={"filename": "商品.xlsx", "content_base64": upload},
    )
    assert (preview.json()["will_create"], preview.json()["will_update"]) == (0, 2)

    # 没有查看成本价的权限（维护商品库的自定义角色）：导出里没有成本价列，也不记审计。
    role = await desk.client.post(
        "/api/v1/roles",
        headers=desk.admin,
        json={"code": "catalog", "name": "商品管理员", "permissions": ["product:manage"]},
    )
    assert role.status_code == 201, role.text
    dan = await desk.agent("dan", roles=["catalog"], online=False)
    plain = await desk.client.get("/api/v1/products/export", headers=dan.headers)
    header = parse_sheet("plain.xlsx", plain.content)[0]
    assert "成本价" not in header and "建议零售价" in header
    # 管理员导出了两次（都含成本价），这次不含成本价，不记审计。
    assert len(await desk.sql("SELECT id FROM audit_logs WHERE action = 'product.export'")) == 2
    agent = await desk.agent("eve", online=False)
    assert (
        await desk.client.get("/api/v1/products/export", headers=agent.headers)
    ).status_code == 403
