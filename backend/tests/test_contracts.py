"""项目合同管理（设计文档 §34）：多层级分类、模板的上传和空白识别、AI 按需求和知识库起草、
内置填写项、编辑和修改历史、定稿、签署、作废、导出 Word、查看范围和权限、合同快到期的检查项。"""

import base64
import io
import json
import uuid
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core import docx
from app.core.config import Settings
from app.modules.contracts import categories, document, fields
from app.modules.kb import parsers
from tests.desk import Desk
from tests.fake_llm import FakeLLM
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import catalog, customer, new_order
from tests.test_wake import findings, wake

C = "/api/v1/contracts"
REQUIREMENT = "给华东分公司定制 200 套工服，30 天交货，预付 30%，验收后付清"
POLICY = """# 售后服务制度
## 质保
产品质保期 12 个月，质保期内非人为损坏免费维修。
## 违约
逾期交货每天按合同金额的千分之五支付违约金。"""


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def call(
    desk: Desk,
    method: str,
    path: str,
    expected: int = 200,
    headers: dict[str, str] | None = None,
    **body: Any,
) -> Any:
    response = await desk.client.request(
        method, path, headers=headers or desk.admin, json=body if body else None
    )
    assert response.status_code == expected, response.text
    return response.json() if response.content else None


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def sample_docx() -> bytes:
    """一份带空白的合同模板（Word）。"""
    items = [
        docx.Item("title", [docx.Span("工服定制合同")]),
        docx.Item("paragraph", [docx.Span("甲方：________  乙方：{{我方名称}}")]),
        docx.Item("heading", [docx.Span("一、标的")]),
        docx.Item("paragraph", [docx.Span("{{标的清单}}")]),
        docx.Item("heading", [docx.Span("二、交货")]),
        docx.Item("paragraph", [docx.Span("交货日期：____年__月__日，交货地点：【    】")]),
        docx.Item("paragraph", [docx.Span("合同金额：{{合同金额}} 元（{{合同金额大写}}）")]),
    ]
    return docx.build(items, title="工服定制合同")


async def knowledge(desk: Desk) -> None:
    for body in (
        {"kind": "doc", "title": "售后服务制度", "content": POLICY, "policy": True},
        {"kind": "faq", "title": "可以开发票吗？", "content": "可以开增值税专用发票。"},
    ):
        await call(desk, "POST", "/api/v1/kb/items", 201, publish=True, **body)


async def order_for(desk: Desk) -> tuple[str, dict[str, Any]]:
    products = await catalog(desk)
    response = await desk.client.post(
        "/api/v1/customers",
        headers=desk.admin,
        json={"display_name": "张经理", "company": "华东分公司", "phone": "13900001234"},
    )
    assert response.status_code == 201, response.text
    customer_id = str(response.json()["id"])
    order = await new_order(
        desk,
        desk.admin,
        customer_id,
        [
            {"product_id": products["LOCK-X1"], "quantity": 2},
            {"product_id": products["BELL-D1"], "quantity": 3},
        ],
        payment_method="deposit",
    )
    return customer_id, order


def test_document_placeholders_blanks_and_amounts() -> None:
    body, found = document.detect_blanks(
        "甲方：________  乙方：____\n电话：____ 传真：____\n交货日期：____年__月__日\n"
        "签订地点：【   】\n甲方：____"
    )
    assert found == ["甲方", "乙方", "电话", "传真", "交货日期", "签订地点", "甲方 2"]
    assert "交货日期：{{交货日期}}" in body
    assert document.missing(body, {"甲方": "华东分公司"})[:2] == ["乙方", "电话"]
    assert document.blank_out("交货：{{交货日期}}", {}) == "交货：＿＿＿（交货日期）"
    sample = "# 标题\n## 一、标的\n| a | b |\n|---|---|\n| 1 | 2 |\n- 项"
    assert [b.kind for b in document.blocks(sample)] == ["heading", "heading", "table", "bullet"]
    assert fields.rmb_upper(Decimal("12000")) == "人民币壹万贰仟元整"
    assert fields.rmb_upper(Decimal("1205.5")) == "人民币壹仟贰佰零伍元伍角"
    assert fields.rmb_upper(Decimal("100010")) == "人民币壹拾万零壹拾元整"
    assert fields.rmb_upper(Decimal("10.05")) == "人民币壹拾元零伍分"


async def test_categories_tree_rules(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    # 坐席能看分类，不能维护。
    await call(desk, "GET", f"{C}/categories", headers=alice.headers)
    await call(desk, "POST", f"{C}/categories", 403, headers=alice.headers, name="销售合同")

    defaults = await call(desk, "POST", f"{C}/categories/defaults")
    assert [c["name"] for c in defaults["items"]] == list(categories.DEFAULTS)
    await call(desk, "POST", f"{C}/categories/defaults", 409)
    sales = next(c for c in defaults["items"] if c["name"] == "销售合同")

    # 最多 5 层；同一级下名称不重复，不同上级下可以同名。
    parent = sales["id"]
    chain = [parent]
    for level in range(2, 6):
        node = await call(
            desk, "POST", f"{C}/categories", 201, name=f"第{level}层", parent_id=parent
        )
        parent = node["id"]
        chain.append(parent)
    await call(desk, "POST", f"{C}/categories", 422, name="第6层", parent_id=parent)
    await call(desk, "POST", f"{C}/categories", 409, name="第2层", parent_id=sales["id"])
    await call(desk, "POST", f"{C}/categories", 201, name="第2层", parent_id=None)

    # 不能移到自己的下级下面；整棵子树移动后超过 5 层也不行。
    await call(desk, "PATCH", f"{C}/categories/{chain[1]}", 422, parent_id=chain[3])
    other = next(c for c in defaults["items"] if c["name"] == "采购合同")
    child = await call(desk, "POST", f"{C}/categories", 201, name="原材料", parent_id=other["id"])
    await call(desk, "PATCH", f"{C}/categories/{other['id']}", 422, parent_id=chain[4])
    moved = await call(desk, "PATCH", f"{C}/categories/{child['id']}", parent_id=sales["id"])
    assert moved["parent_id"] == sales["id"]

    # 有下级、模板的分类不能删除；空的可以。
    await call(desk, "DELETE", f"{C}/categories/{sales['id']}", 409)
    template = await call(
        desk, "POST", f"{C}/templates", 201, name="采购模板", category_id=other["id"], body="# 采购"
    )
    await call(desk, "DELETE", f"{C}/categories/{other['id']}", 409)
    await call(desk, "PATCH", f"{C}/templates/{template['id']}", category_id=None)
    await call(desk, "DELETE", f"{C}/categories/{other['id']}", 204)
    listed = await call(desk, "GET", f"{C}/categories")
    assert other["id"] not in {c["id"] for c in listed["items"]}
    assert listed["max_depth"] == 5


async def test_template_upload_fields_and_permissions(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)
    uploaded = await call(
        desk,
        "POST",
        f"{C}/templates/upload",
        headers=alice.headers,
        filename="工服定制合同.docx",
        content_base64=b64(sample_docx()),
    )
    template = uploaded["template"]
    assert uploaded["detected"] == ["甲方", "交货日期", "交货地点"]
    assert template["name"] == "工服定制合同"
    assert "甲方：{{甲方}}" in template["body"]
    assert template["file_name"] == "工服定制合同.docx"
    names = {f["name"]: f["builtin"] for f in template["fields"]}
    assert names == {
        "甲方": False,
        "我方名称": True,
        "标的清单": True,
        "交货日期": False,
        "交货地点": False,
        "合同金额": True,
        "合同金额大写": True,
    }
    link = await call(desk, "GET", f"{C}/templates/{template['id']}/file", headers=bob.headers)
    assert link["filename"] == "工服定制合同.docx" and link["url"].startswith("http")

    # 不支持的格式；别人的模板不能改，管理员可以；填写项的说明保存下来。
    await call(
        desk,
        "POST",
        f"{C}/templates/upload",
        422,
        headers=alice.headers,
        filename="a.xlsx",
        content_base64=b64(b"x"),
    )
    path = f"{C}/templates/{template['id']}"
    await call(desk, "PATCH", path, 403, headers=bob.headers, name="改名")
    hint = [{"name": "交货日期", "hint": "签订后多少天交货", "default": ""}]
    updated = await call(desk, "PATCH", path, headers=alice.headers, fields=hint)
    hints = {f["name"]: f["hint"] for f in updated["fields"]}
    assert hints["交货日期"] == "签订后多少天交货"
    disabled = await call(desk, "PATCH", path, status="disabled")
    assert disabled["status"] == "disabled"

    # 停用的模板不能用来新建合同；用过的模板不能删除，没用过的可以。
    await call(desk, "POST", C, 409, template_id=template["id"])
    await call(desk, "PATCH", path, status="active")
    await call(desk, "POST", C, 201, template_id=template["id"])
    await call(desk, "DELETE", path, 409)
    spare = await call(desk, "POST", f"{C}/templates", 201, name="空模板")
    assert spare["body"] == "# 空模板\n"
    await call(desk, "DELETE", f"{C}/templates/{spare['id']}", 204)

    # 修改历史：上传、修改、停用、启用。
    history = await call(desk, "GET", f"/api/v1/history/contract_tpl/{template['id']}")
    assert [v["action"] for v in history["versions"]] == ["enable", "disable", "update", "upload"]


async def test_ai_generates_from_requirement_knowledge_and_order(
    desk: Desk, fake_llm: FakeLLM
) -> None:
    await knowledge(desk)
    customer_id, order = await order_for(desk)
    party = {
        "name": "上海某某服饰有限公司",
        "address": "上海市松江区",
        "phone": "021-12345678",
        "tax_no": "91310000XXXXXXXX",
        "bank": "工商银行松江支行",
        "account": "1001 2345 6789",
        "representative": "王总",
    }
    await call(desk, "PUT", f"{C}/settings", party=party, prefix="HT", expiring_days=30)
    fake_llm.requests.clear()

    contract = await call(
        desk, "POST", f"{C}/generate", 201, requirement=REQUIREMENT, order_id=order["id"]
    )
    values = contract["field_values"]
    total = Decimal(order["total"])
    assert contract["status"] == "draft" and contract["title"] == "定制加工合同"
    assert contract["no"].startswith(f"HT{datetime.now(UTC):%Y}")
    assert contract["customer_id"] == customer_id and contract["order_id"] == order["id"]
    assert Decimal(contract["amount"]) == total
    assert values["客户名称"] == "华东分公司" and values["我方名称"] == party["name"]
    assert values["合同金额"] == fields.money_text(total)
    assert values["合同金额大写"] == fields.rmb_upper(total)
    assert "智能门锁 X1" in values["标的清单"] and "合计" in values["标的清单"]
    assert values["交货期限"] == "合同签订后 30 天内"
    assert values["合同编号"] == contract["no"]
    assert contract["missing"] == []
    # 售后条款来自规章制度；依据里记下用到的知识和模型、Token。
    assert "质保期内非人为损坏免费维修" in contract["body"]
    ai = contract["ai"]
    used = [k for k in ai["knowledge"] if k["used"]]
    # 同一条制度检索到几段（质保、违约）时依据里只列一次。
    assert len({k["item_id"] for k in ai["knowledge"]}) == len(ai["knowledge"])
    assert [k["title"] for k in used] == ["售后服务制度"] and used[0]["policy"] is True
    assert ai["model"] == "fake-chat" and ai["prompt_tokens"] > 0
    assert ai["requirement"] == REQUIREMENT and ai["notes"]

    # 发给大模型的内容：需求、订单摘要和知识，没有客户的手机号；客户电话由系统填（管理员有权限），
    # 并记一条查看手机号的审计。
    chats = [r for r in fake_llm.requests if "messages" in r]
    prompt = json.dumps(chats[-1]["messages"], ensure_ascii=False)
    assert "13900001234" not in prompt and "售后服务制度" in prompt and "智能门锁 X1" in prompt
    assert values["客户电话"] == "13900001234"
    audits = await desk.sql(
        "SELECT detail FROM audit_logs WHERE tenant_id = $1 AND action = 'customer.view_sensitive'",
        desk.tenant_id,
    )
    assert any(json.loads(a["detail"]).get("for") == "contract" for a in audits)
    [call_row] = await desk.sql(
        "SELECT scene, status FROM llm_calls WHERE tenant_id = $1 AND scene = 'contract'",
        desk.tenant_id,
    )
    assert call_row["status"] == "ok"

    # 模型给不出正文时不建合同，编号也不占用。
    fake_llm.mode = "bad_json"
    response = await desk.client.post(
        f"{C}/generate", headers=desk.admin, json={"requirement": REQUIREMENT}
    )
    assert response.status_code == 503
    fake_llm.mode = "normal"
    second = await call(desk, "POST", f"{C}/generate", 201, requirement="采购一批门铃")
    assert int(second["no"][-4:]) == int(contract["no"][-4:]) + 1

    # 套餐不包含 AI 时不能 AI 起草（可以按模板新建）。
    await desk.sql(
        "UPDATE tenants SET settings = jsonb_set(coalesce(settings, '{}'), '{features}',"
        " '{\"ai\": false}') WHERE id = $1",
        desk.tenant_id,
    )
    response = await desk.client.post(
        f"{C}/generate", headers=desk.admin, json={"requirement": REQUIREMENT}
    )
    assert response.status_code == 403
    await call(desk, "POST", C, 201, title="手写的合同")


async def test_edit_finalize_sign_void_history_and_export(desk: Desk) -> None:
    template = await call(
        desk,
        "POST",
        f"{C}/templates",
        201,
        name="服务合同",
        body="# 服务合同\n甲方：{{客户名称}}\n服务内容：{{服务内容}}\n期限：{{服务期限}}\n"
        "## 价款\n| 项目 | 金额 |\n|---|---|\n| 服务费 | {{服务费}} |",
        fields=[{"name": "服务期限", "hint": "", "default": "一年"}],
    )
    customer_id = await customer(desk, "李女士")
    contract = await call(desk, "POST", C, 201, template_id=template["id"], customer_id=customer_id)
    cid = contract["id"]
    assert contract["field_values"]["客户名称"] == "李女士"
    assert contract["field_values"]["服务期限"] == "一年"
    assert contract["missing"] == ["服务内容", "服务费"]
    response = await desk.client.post(f"{C}/{cid}/finalize", headers=desk.admin)
    assert response.status_code == 409 and "服务内容、服务费" in response.text

    edited = await call(
        desk,
        "PATCH",
        f"{C}/{cid}",
        field_values={
            **contract["field_values"],
            "服务内容": "年度设备维护",
            "服务费": "12,000.00",
        },
        body=contract["body"] + "\n## 其他\n本合同一式两份。",
        amount="12000",
    )
    assert edited["missing"] == [] and Decimal(edited["amount"]) == Decimal("12000")
    final = await call(desk, "POST", f"{C}/{cid}/finalize")
    assert final["status"] == "final" and final["can_edit"] is False
    await call(desk, "PATCH", f"{C}/{cid}", 409, title="改名")
    reopened = await call(desk, "POST", f"{C}/{cid}/reopen")
    assert reopened["status"] == "draft"
    await call(desk, "PATCH", f"{C}/{cid}", title="年度维护服务合同")
    await call(desk, "POST", f"{C}/{cid}/finalize")

    # 导出 Word：标题、填好的填写项、表格。
    response = await desk.client.get(f"{C}/{cid}/docx", headers=desk.admin)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "attachment" in response.headers["content-disposition"]
    parsed = parsers.parse_document("c.docx", response.content)
    assert "年度设备维护" in parsed.text and "服务费 | 12,000.00" in parsed.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "word/footer1.xml" in archive.namelist()

    # 登记签署：扫描件只能是 PDF 或图片。
    sign = {"sign_date": str(date.today()), "start_date": str(date.today())}
    bad = {"filename": "scan.txt", "content_base64": b64(b"not a pdf")}
    await call(desk, "POST", f"{C}/{cid}/sign", 422, **sign, scan=bad)
    scan = {"filename": "签字版.pdf", "content_base64": b64(b"%PDF-1.4\n%fake\n")}
    end = date.today() + timedelta(days=20)
    signed = await call(desk, "POST", f"{C}/{cid}/sign", **sign, end_date=str(end), scan=scan)
    assert signed["status"] == "signed" and signed["scan_name"] == "签字版.pdf"
    assert signed["expiry"] == "expiring"
    link = await call(desk, "GET", f"{C}/{cid}/scan")
    assert link["filename"] == "签字版.pdf"
    page = await call(desk, "GET", f"{C}?view=expiring")
    assert [c["id"] for c in page["items"]] == [cid]
    assert page["counts"]["signed"] == 1 and page["counts"]["expiring"] == 1

    # 只有草稿可以删除；作废写原因。
    await call(desk, "DELETE", f"{C}/{cid}", 409)
    voided = await call(desk, "POST", f"{C}/{cid}/void", reason="客户取消合作")
    assert voided["status"] == "void" and voided["void_reason"] == "客户取消合作"

    history = await call(desk, "GET", f"/api/v1/history/contract/{cid}")
    actions = [v["action"] for v in reversed(history["versions"])]
    assert actions == [
        "create",
        "update",
        "finalize",
        "reopen",
        "update",
        "finalize",
        "sign",
        "void",
    ]
    first_edit = history["versions"][-2]
    assert "正文新增 ## 其他" in first_edit["summary"]

    # 另存为模板：填写项保留成 {{名称}}。
    saved = await call(desk, "POST", f"{C}/{cid}/save-as-template", name="维护服务模板")
    assert "{{服务内容}}" in saved["body"] and saved["name"] == "维护服务模板"


async def test_relinking_refills_builtin_fields(desk: Desk) -> None:
    customer_id, order = await order_for(desk)
    other = await customer(desk, "李女士")
    body = "# 门锁采购合同\n甲方：{{客户名称}}\n{{标的清单}}\n金额：{{合同金额}} 元"
    contract = await call(desk, "POST", C, 201, body=body, customer_id=other)
    cid = contract["id"]
    assert contract["field_values"]["客户名称"] == "李女士"
    assert contract["missing"] == ["标的清单", "合同金额"] and contract["amount"] is None

    # 换成订单（客户跟着订单）：跟着客户、订单的内置填写项重新填写，金额跟着订单；
    # 同时提交的旧值不算。
    relinked = await call(
        desk,
        "PATCH",
        f"{C}/{cid}",
        customer_id=customer_id,
        order_id=order["id"],
        field_values={**contract["field_values"], "客户名称": "李女士"},
    )
    values = relinked["field_values"]
    assert values["客户名称"] == "华东分公司" and "智能门锁 X1" in values["标的清单"]
    assert Decimal(relinked["amount"]) == Decimal(order["total"]) and relinked["missing"] == []

    # 正文里新加的内置填写项按数据填上，手填的其他值不动。
    added = await call(
        desk,
        "PATCH",
        f"{C}/{cid}",
        body=relinked["body"] + "\n编号：{{合同编号}}，付款：{{付款方式}}，交货：{{交货地点}}",
        field_values={**values, "交货地点": "上海仓"},
    )
    assert added["field_values"]["合同编号"] == contract["no"]
    assert added["field_values"]["付款方式"].startswith("预付定金")
    assert added["field_values"]["交货地点"] == "上海仓" and added["missing"] == []

    # 去掉订单：跟着订单的值清掉成为待填写，客户的值和金额不动。
    unlinked = await call(desk, "PATCH", f"{C}/{cid}", order_id=None)
    assert set(unlinked["missing"]) == {"标的清单", "合同金额", "付款方式"}
    assert unlinked["field_values"]["客户名称"] == "华东分公司"
    assert Decimal(unlinked["amount"]) == Decimal(order["total"])


async def test_scope_and_permissions(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    worker = await desk.agent("worker", roles=["worker"], online=False)
    mine = await call(desk, "POST", C, 201, headers=alice.headers, title="alice 的合同")
    path = f"{C}/{mine['id']}"

    await call(desk, "GET", path, 404, headers=bob.headers)
    assert (await call(desk, "GET", C, headers=bob.headers))["total"] == 0
    assert (await call(desk, "GET", C, headers=boss.headers))["total"] == 1
    await call(desk, "GET", C, 403, headers=worker.headers)
    await call(desk, "GET", f"/api/v1/history/contract/{mine['id']}", 404, headers=bob.headers)

    # 改负责人需要管理权限；交给 bob 后 bob 能看到、能处理。
    await call(desk, "PATCH", path, 403, headers=alice.headers, owner_id=str(bob.staff_id))
    moved = await call(desk, "PATCH", path, headers=boss.headers, owner_id=str(bob.staff_id))
    assert moved["owner_id"] == str(bob.staff_id)
    assert (await call(desk, "GET", path, headers=bob.headers))["can_manage"] is True
    # 建的人仍然能看到。
    await call(desk, "GET", path, headers=alice.headers)

    # 别人的客户不能关联。
    other = await customer(desk, "王先生")
    await call(desk, "POST", C, 404, headers=bob.headers, customer_id=other)
    # 合同设置：坐席能看，不能改。
    current = await call(desk, "GET", f"{C}/settings", headers=bob.headers)
    assert {f["name"] for f in current["builtin"]} >= {"合同编号", "标的清单", "合同金额大写"}
    await call(desk, "PUT", f"{C}/settings", 403, headers=bob.headers, **current["settings"])


async def test_wake_reminds_owners_of_expiring_contracts(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    contracts = {}
    for name, days in (("soon", 5), ("later", 20), ("far", 90)):
        made = await call(desk, "POST", C, 201, headers=alice.headers, title=name)
        await call(desk, "POST", f"{C}/{made['id']}/finalize", headers=alice.headers)
        end = date.today() + timedelta(days=days)
        await call(
            desk,
            "POST",
            f"{C}/{made['id']}/sign",
            headers=alice.headers,
            sign_date=str(date.today()),
            end_date=str(end),
        )
        contracts[name] = made["id"]

    await wake(desk, "daily", trigger="manual")
    found = {k: v for k, v in (await findings(desk)).items() if k.startswith("contract_expiring")}
    by_contract = {f["entity_id"]: f for f in found.values()}
    assert set(map(str, by_contract)) == {contracts["soon"], contracts["later"]}
    soon = by_contract[uuid.UUID(contracts["soon"])]
    assert soon["severity"] == "critical" and soon["status"] == "open"
    assert by_contract[uuid.UUID(contracts["later"])]["severity"] == "warning"
    assert soon["assignee_ids"] == [alice.staff_id]
    assert soon["title"].endswith("「soon」快到期了")

    # 作废后问题消除。
    soon_path = f"{C}/{contracts['soon']}/void"
    await call(desk, "POST", soon_path, headers=alice.headers, reason="提前终止")
    await wake(desk, "daily", trigger="manual")
    found = {k: v for k, v in (await findings(desk)).items() if k.startswith("contract_expiring")}
    assert {str(f["entity_id"]): f["status"] for f in found.values()} == {
        contracts["soon"]: "resolved",
        contracts["later"]: "open",
    }
