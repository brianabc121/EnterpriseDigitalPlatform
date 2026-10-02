"""云打印机（设计文档 §29）：小票排版、打印机接入、领取和开领料单时自动打印、第 N 次打印、
重试与放弃。厂商用 tests/fake_printer.py 模拟（芯烨云和飞鹅云两种协议）。"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.print import delivery, ticket
from tests.desk import Desk
from tests.fake_openim import FakeOpenIM
from tests.fake_printer import FEIE_KEY, FEIE_USER, XPYUN_KEY, XPYUN_USER, FakePrinterCloud
from tests.support import DatabaseUrls
from tests.test_orders import call, catalog, customer
from tests.test_production import confirmed_order, worker
from tests.test_warehouse import material

PRINTERS = "/api/v1/print/printers"


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


def xpyun(**changes: Any) -> dict[str, Any]:
    body = {
        "name": "车间打印机",
        "brand": "xpyun",
        "account": XPYUN_USER,
        "key": XPYUN_KEY,
        "sn": "XPY0001",
        "uses": ["order", "requisition"],
        "copies": 1,
        "enabled": True,
    }
    body.update(changes)
    return body


async def jobs_of(
    desk: Desk, headers: dict[str, str], kind: str, ref_id: str
) -> list[dict[str, Any]]:
    page = await call(desk, headers, "GET", f"/api/v1/print/jobs?kind={kind}&ref_id={ref_id}")
    items: list[dict[str, Any]] = page["items"]
    return items


# ---- 排版（不需要数据库） ----


def test_ticket_layout_and_vendor_markup() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 10, 2, 10, 30, 5, tzinfo=UTC)
    assert ticket.width("智能门锁 X1") == 11
    assert ticket.pad("数量", 7, "right") == "   数量"
    assert ticket.wrap("一二三四五六七八九十一二三四五六七八九十一二三四五六", 48) == [
        "一二三四五六七八九十一二三四五六七八九十一二三四",
        "五六",
    ]
    lines = ticket.order_ticket(
        company="华盛门窗有限公司",
        order_no="SO20261002-0001",
        confirmed_at=now,
        customer="李女士",
        expected_at=None,
        worker="老王",
        items=[
            ticket.OrderLine("智能门锁 X1", "X1", "黑色 <左开>", 2, "套"),
            ticket.OrderLine("一个名字特别长的铝合金推拉窗带纱窗和防盗网组合套装", "", "", 1),
        ],
        customer_note="周五前要",
        internal_note="",
        printed_by="张三",
        seq=2,
        now=now,
        tz=tz,
    )
    text = ticket.plain(lines)
    assert "加工单" in text and "单号：SO20261002-0001" in text
    assert "确认时间：2026-10-02 18:30" in text and "加工人：老王" in text
    assert "第 2 次打印" in text and "打印人：张三" in text
    assert "型号/规格：X1 / 黑色 <左开>" in text and "客户要求：周五前要" in text
    assert "共 2 项，3 件" in text and "[二维码 SO20261002-0001]" in text
    # 每一行都不超过 48 列；长名称在本列内折行。
    assert all(ticket.width(line) <= ticket.WIDTH for line in text.splitlines())
    assert any(line.startswith("    ") and "防盗网" in line for line in text.splitlines())
    xp = ticket.markup(lines, "xpyun")
    assert xp.startswith("<CB>华盛门窗有限公司<BR></CB><CB>加工单<BR></CB><BR>")
    assert "<QRCODE s=6 e=L l=center>SO20261002-0001</QRCODE>" in xp and xp.endswith("<CUT>")
    assert "黑色 ＜左开＞" in xp and "<左开>" not in xp
    fe = ticket.markup(lines, "feie")
    assert fe.startswith("<CB>华盛门窗有限公司</CB><BR><CB>加工单</CB><BR><BR>")
    assert "<QR>SO20261002-0001</QR>" in fe
    # 领料单：建议数量和本次领用右对齐，签字栏。
    req = ticket.plain(
        ticket.requisition_ticket(
            company="华盛门窗有限公司",
            document_no="LL20261002-0001",
            order_no="SO20261002-0001",
            customer="李女士",
            submitted_at=now,
            created_by="老王",
            status_label="待确认",
            materials=[
                ticket.MaterialLine("铝合金型材", "6063", "米", Decimal("13"), Decimal("12.5"))
            ],
            note="",
            printed_by="老王",
            seq=1,
            now=now,
            tz=tz,
        )
    )
    assert "领料单" in req and "关联订单：SO20261002-0001（李女士）" in req
    assert "铝合金型材            6063      米      13  12.5" in req
    assert "领料人签字：" in req and "仓管签字：" in req and "第 1 次打印" in req
    # 存下来再读回来一样。
    assert ticket.load(ticket.dump(lines)) == lines


# ---- 接入打印机 ----


async def test_printer_setup_is_verified_at_the_vendor(
    desk: Desk, fake_printer: FakePrinterCloud
) -> None:
    # 账号、密钥、编号不对时保存不了，提示厂商返回的原因。
    bad = await desk.client.post(PRINTERS, headers=desk.admin, json=xpyun(key="wrong"))
    assert bad.status_code == 422 and "REQUEST_SIGN_FAILED" in bad.text, bad.text
    bad = await desk.client.post(PRINTERS, headers=desk.admin, json=xpyun(sn="ABC0001"))
    assert bad.status_code == 422 and "1008" in bad.text, bad.text
    assert not fake_printer.prints

    printer = await call(desk, desk.admin, "POST", PRINTERS, 201, **xpyun())
    assert (printer["status"], printer["status_label"], printer["brand_label"]) == (
        "online", "在线", "芯烨云",
    )  # fmt: skip
    assert "key" not in printer and ("xpyun", "XPY0001") in fake_printer.printers
    duplicate = await desk.client.post(PRINTERS, headers=desk.admin, json=xpyun(name="另一台"))
    assert duplicate.status_code == 409
    listed = await call(desk, desk.admin, "GET", PRINTERS)
    assert [p["sn"] for p in listed["items"]] == ["XPY0001"]

    # 飞鹅云：编号加机身 KEY；修改时密钥留空表示不改。
    feie = await call(
        desk, desk.admin, "POST", PRINTERS, 201,
        **xpyun(name="仓库打印机", brand="feie", account=FEIE_USER, key=FEIE_KEY, sn="FEIE0001",
                device_key="abcd1234", uses=["requisition"]),
    )  # fmt: skip
    assert feie["status"] == "online" and ("feie", "FEIE0001") in fake_printer.printers
    renamed = await call(
        desk, desk.admin, "PUT", f"{PRINTERS}/{feie['id']}",
        **xpyun(name="仓库 A 打印机", brand="feie", account=FEIE_USER, key=None, sn="FEIE0001",
                device_key="abcd1234", uses=["requisition"], copies=2),
    )  # fmt: skip
    assert (renamed["name"], renamed["copies"]) == ("仓库 A 打印机", 2)

    # 测试打印：走队列，发送后厂商收到两家各自的标记；确认后变成"已打印"。
    for item in (printer, renamed):
        job = await call(desk, desk.admin, "POST", f"{PRINTERS}/{item['id']}/test")
        assert (job["status"], job["kind_label"], job["seq"]) == ("queued", "测试页", 1)
        assert "打印测试" in job["content"] and f"打印机：{item['name']}" in job["content"]
    assert await delivery.deliver_due(desk.ctx) == {"sent": 2, "retrying": 0, "dead": 0}
    contents = {p.brand: p.content for p in fake_printer.prints}
    assert contents["xpyun"].startswith("<CB>") and "<BR></CB>" in contents["xpyun"]
    assert "</CB><BR>" in contents["feie"] and "打印测试" in contents["feie"]
    assert fake_printer.prints[1].copies == 2
    assert await delivery.confirm_sent(desk.ctx) == 2
    page = await call(desk, desk.admin, "GET", "/api/v1/print/jobs?kind=test")
    assert {j["status"] for j in page["items"]} == {"printed"}
    assert all(j["printed_at"] for j in page["items"])

    # 状态检查：厂商说离线、缺纸。
    fake_printer.set_status("XPY0001", "abnormal")
    checked = await call(desk, desk.admin, "POST", f"{PRINTERS}/{printer['id']}/check")
    assert (checked["status"], checked["status_label"]) == ("abnormal", "缺纸或异常")
    fake_printer.set_status("FEIE0001", "offline")
    assert await delivery.check_printers(desk.ctx, now=datetime(2030, 1, 1, tzinfo=UTC)) == 2
    listed = await call(desk, desk.admin, "GET", PRINTERS)
    statuses = {p["sn"]: p["status"] for p in listed["items"]}
    assert statuses == {"XPY0001": "abnormal", "FEIE0001": "offline"}

    # 没有打印机权限的员工管不了打印机，但能看可用的打印机。
    agent = await desk.agent("mei", roles=["agent"], online=False)
    denied = await desk.client.get(PRINTERS, headers=agent.headers)
    assert denied.status_code == 403
    options = await call(desk, agent.headers, "GET", "/api/v1/print/options?kind=order")
    assert (len(options["items"]), options["auto"]) == (2, True)

    # 删除：厂商那边也删掉；审计记了增删改和测试打印。
    await call(desk, desk.admin, "DELETE", f"{PRINTERS}/{feie['id']}", 204)
    assert ("feie", "FEIE0001") not in fake_printer.printers
    actions = [
        r["action"]
        for r in await desk.sql(
            "SELECT action FROM audit_logs WHERE action LIKE 'print.%' ORDER BY created_at"
        )
    ]
    assert actions == [
        "print.printer_create",
        "print.printer_create",
        "print.printer_update",
        "print.test",
        "print.test",
        "print.printer_delete",
    ]


# ---- 领取、开领料单时自动打印 ----


async def test_claim_and_requisition_print_with_sequence_and_retries(
    desk: Desk, fake_printer: FakePrinterCloud
) -> None:
    products = await catalog(desk)
    frame = await material(desk, "AL-6063", "铝合金型材")
    wang = await worker(desk, "wang")
    laoli = await worker(desk, "laoli")
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    order = await confirmed_order(desk, products, await customer(desk, "李女士"))
    printer = await call(desk, desk.admin, "POST", PRINTERS, 201, **xpyun())

    # 领取：加工单排队（和领取一起提交），调度任务发送；小票上有加工人、打印人和第 1 次打印，
    # 没有价格。
    await call(desk, wang.headers, "POST", f"/api/v1/production/orders/{order['id']}/claim")
    [job] = await jobs_of(desk, wang.headers, "order", order["id"])
    assert (job["status"], job["seq"]) == ("queued", 1)
    assert (job["source_label"], job["requested_by_name"]) == ("领取订单", "Wang")
    assert await delivery.deliver_due(desk.ctx) == {"sent": 1, "retrying": 0, "dead": 0}
    [printed] = fake_printer.prints
    assert printed.sn == "XPY0001" and "加工单" in printed.content
    assert order["no"] in printed.content and "加工人：Wang" in printed.content
    assert "打印人：Wang" in printed.content and "第 1 次打印" in printed.content
    assert "智能门锁 X1" in printed.content and "2套" not in printed.content
    assert "1299" not in printed.content and "199" not in printed.content
    events = [
        r["type"]
        for r in await desk.sql(
            "SELECT type FROM order_events WHERE order_id = $1 ORDER BY created_at",
            uuid.UUID(order["id"]),
        )
    ]
    assert events[-2:] == ["claimed", "printed"]

    # 手工重打：第 2 次；10 秒内再打一次被拒绝；别的工人看不到这张订单的打印记录。
    again = await call(desk, wang.headers, "POST", f"/api/v1/print/orders/{order['id']}")
    assert [j["seq"] for j in again["jobs"]] == [2]
    assert "第 2 次打印" in again["jobs"][0]["content"]
    repeated = await desk.client.post(f"/api/v1/print/orders/{order['id']}", headers=wang.headers)
    assert repeated.status_code == 409
    other = await desk.client.get(
        f"/api/v1/print/jobs?kind=order&ref_id={order['id']}", headers=laoli.headers
    )
    assert other.status_code == 403
    assert len(await jobs_of(desk, boss.headers, "order", order["id"])) == 2

    # 开领料单：领料单排队并发送；开单人是工人。
    document = await call(
        desk, wang.headers, "POST", "/api/v1/warehouse/documents", 201,
        kind="requisition", order_id=order["id"],
        lines=[{"product_id": frame["id"], "quantity": 2.5}],
    )  # fmt: skip
    [requisition] = await jobs_of(desk, wang.headers, "requisition", document["id"])
    assert (requisition["seq"], requisition["source_label"]) == (1, "开领料单")
    assert await delivery.deliver_due(desk.ctx) == {"sent": 2, "retrying": 0, "dead": 0}
    texts = fake_printer.contents()
    assert any("领料单" in t and document["no"] in t and "开单人：Wang" in t for t in texts)
    assert any("铝合金型材" in t and "2.5" in t for t in texts)
    assert any("第 2 次打印" in t and order["no"] in t for t in texts)

    # 主管指派给别的工人：也打一张（第 3 次），打印人是主管。
    await call(desk, wang.headers, "POST", f"/api/v1/production/orders/{order['id']}/release")
    await call(
        desk, boss.headers, "POST", f"/api/v1/production/orders/{order['id']}/assign",
        worker_id=str(laoli.staff_id),
    )  # fmt: skip
    assigned = await jobs_of(desk, boss.headers, "order", order["id"])
    latest = assigned[0]
    assert (latest["seq"], latest["source_label"], latest["requested_by_name"]) == (
        3, "指派加工", "Boss",
    )  # fmt: skip

    # 网络抖动：发送失败按退避重试；配置错误（账号不对）直接放弃并通知打印人，打印机标成配置错误。
    fake_printer.fail_count = 1
    assert await delivery.deliver_due(desk.ctx) == {"sent": 0, "retrying": 1, "dead": 0}
    retrying = (await jobs_of(desk, boss.headers, "order", order["id"]))[0]
    assert retrying["status"] == "retrying" and "HTTP 500" in retrying["last_error"]
    # 还没到重试时间。
    assert await delivery.deliver_due(desk.ctx) == {"sent": 0, "retrying": 0, "dead": 0}
    await desk.sql("UPDATE print_jobs SET next_attempt_at = now() WHERE status = 'retrying'")
    await desk.sql("UPDATE printers SET account = 'nobody@example.com'")
    assert await delivery.deliver_due(desk.ctx) == {"sent": 0, "retrying": 0, "dead": 1}
    dead = (await jobs_of(desk, boss.headers, "order", order["id"]))[0]
    assert dead["status"] == "dead" and "REQUEST_USER_NOT_REGISTER" in dead["last_error"]
    [misconfigured] = (await call(desk, desk.admin, "GET", PRINTERS))["items"]
    assert misconfigured["status"] == "misconfigured"
    notes = await call(desk, boss.headers, "GET", "/api/v1/notifications")
    failed = [n for n in notes["items"] if n["kind"] == "print_failed"]
    assert failed and f"加工单 {order['no']} 打印失败" in failed[0]["title"]

    # 改回账号后重新发送：沿用第 3 次。
    await desk.sql("UPDATE printers SET account = $1", XPYUN_USER)
    resent = await call(desk, desk.admin, "POST", f"/api/v1/print/jobs/{dead['id']}/resend")
    assert (resent["status"], resent["seq"]) == ("queued", 3)
    assert await delivery.deliver_due(desk.ctx) == {"sent": 1, "retrying": 0, "dead": 0}
    assert sum("第 3 次打印" in t for t in fake_printer.contents()) == 1
    worker_denied = await desk.client.post(
        f"/api/v1/print/jobs/{dead['id']}/resend", headers=laoli.headers
    )
    assert worker_denied.status_code == 403

    # 停用打印机后不再自动打印。
    await call(
        desk, desk.admin, "PUT", f"{PRINTERS}/{printer['id']}", **xpyun(key=None, enabled=False)
    )
    second = await confirmed_order(desk, products, await customer(desk, "王先生"))
    await call(desk, laoli.headers, "POST", f"/api/v1/production/orders/{second['id']}/claim")
    assert await jobs_of(desk, laoli.headers, "order", second["id"]) == []
