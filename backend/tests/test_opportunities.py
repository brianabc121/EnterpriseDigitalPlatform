"""商机（设计文档 §40）：员工转入、跟进（第一次跟进自动推进）、修改、换阶段、赢单、输单和重新跟进；
订单确认后自动赢单；AI 按会话自动转入或建议、确认和忽略、客户又来咨询；AI 写跟进话术；客户列表的
"商机"标签；阶段设置、看板和顶部数字；"商机该跟进了"的检查项；合并客户；查看范围和权限。"""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.opportunities import ai, service
from tests.desk import Agent, Desk, Visitor
from tests.fake_llm import FakeLLM
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_integration import api_key, bearer, outbox
from tests.test_orders import catalog, customer, new_order
from tests.test_wake import findings, notes, wake

P = "/api/v1/opportunities"


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
    verb: str,
    path: str,
    expected: int = 200,
    headers: dict[str, str] | None = None,
    **body: Any,
) -> Any:
    response = await desk.client.request(
        verb, path, headers=headers or desk.admin, json=body if body else None
    )
    assert response.status_code == expected, response.text
    return response.json() if response.content else None


async def listing(desk: Desk, headers: dict[str, str] | None = None, **params: Any) -> Any:
    response = await desk.client.get(P, headers=headers or desk.admin, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def tags(desk: Desk) -> dict[str, tuple[str | None, str | None]]:
    """客户列表里每个客户的"商机"标签：状态和阶段。"""
    page = await call(desk, "GET", "/api/v1/customers")
    return {c["id"]: (c["opportunity_status"], c["opportunity_stage"]) for c in page["items"]}


def kinds(detail: dict[str, Any]) -> list[str]:
    """时间线上各条的种类，早的在前。"""
    return [a["kind"] for a in reversed(detail["activities"])]


def of_kind(detail: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [a for a in reversed(detail["activities"]) if a["kind"] == kind]


async def today(desk: Desk) -> date:
    async with desk.ctx.db.tenant_session(desk.tenant_id) as session:
        return await service.today(session)


async def scan(desk: Desk) -> dict[str, int]:
    return await ai.scan(desk.ctx, tenant_id=desk.tenant_id)


async def stage_ids(desk: Desk) -> dict[str, str]:
    return {s["code"]: s["id"] for s in await call(desk, "GET", f"{P}/stages")}


async def closed_chat(
    desk: Desk,
    agent: Agent,
    text: str,
    stage: int,
    *,
    concerns: tuple[str, ...] = ("price",),
    visitor: Visitor | None = None,
) -> tuple[Visitor, str, str]:
    """访客咨询、坐席接待后结束会话，意图判断的最高意向记为 stage。返回访客、客户 ID 和会话 ID。"""
    visitor = visitor or await desk.visitor()
    await desk.say(visitor, text)
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == agent.staff_id
    closed = await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=agent.headers)
    assert closed.status_code == 200, closed.text
    await desk.flush()
    await desk.sql(
        "INSERT INTO session_intents"
        " (tenant_id, session_id, stage, peak_stage, peak_at, concerns, judged_at)"
        " VALUES ($1, $2, $3, $3, now(), $4, now())"
        " ON CONFLICT (session_id) DO UPDATE SET stage = $3, peak_stage = $3, peak_at = now(),"
        " concerns = $4, due_at = NULL",
        desk.tenant_id,
        chat["id"],
        stage,
        list(concerns),
    )
    return visitor, str(chat["customer_id"]), str(chat["id"])


async def audit_actions(desk: Desk) -> list[str]:
    rows = await desk.sql(
        "SELECT action FROM audit_logs WHERE tenant_id = $1 AND action LIKE 'opportunity.%'"
        " ORDER BY created_at, id",
        desk.tenant_id,
    )
    return [r["action"] for r in rows]


async def test_staff_converts_follows_up_wins_and_reopens(desk: Desk) -> None:
    customer_id = await customer(desk, "王先生")
    other = await customer(desk, "赵女士")
    day = await today(desk)
    made = await call(
        desk,
        "POST",
        P,
        201,
        customer_id=customer_id,
        level="high",
        interest=" 智能门锁 X1，10 把 ",
        concerns="觉得价格偏高",
    )
    assert (made["status"], made["source"], made["level"]) == ("active", "staff", "high")
    assert made["interest"] == "智能门锁 X1，10 把" and made["concerns"] == "觉得价格偏高"
    # 名称按想要什么，阶段是第一个进行中的阶段，概率是阶段的。
    assert (made["name"], made["stage_code"], made["stage_name"]) == (
        "智能门锁 X1，10 把",
        "new",
        "新线索",
    )
    assert (made["probability"], made["amount"], made["days_in_stage"]) == (10, None, 0)
    assert made["next_follow_at"] == str(day + timedelta(days=3))
    assert (made["owner_name"], made["created_by_name"]) == ("管理员", "管理员")
    assert kinds(made) == ["created"] and made["can_assign"] is True and made["can_manage"]
    # 同一客户只能有一条进行中的；下次跟进日期不能早于今天。
    await call(desk, "POST", P, 409, customer_id=customer_id)
    yesterday = str(day - timedelta(days=1))
    await call(desk, "POST", P, 422, customer_id=other, next_follow_at=yesterday)

    # 客户列表、客户资料里的"商机"标签（状态和阶段）。
    listed = await tags(desk)
    assert (listed[customer_id], listed[other]) == (("active", "新线索"), (None, None))
    detail = await call(desk, "GET", f"/api/v1/customers/{customer_id}")
    assert (detail["opportunity_status"], detail["opportunity_stage"]) == ("active", "新线索")
    info = await call(desk, "GET", f"{P}/customer/{customer_id}")
    assert info["opportunity"]["id"] == made["id"] and info["recent_deal_at"] is None
    assert info["opportunity"]["stage_name"] == "新线索"
    assert (await call(desk, "GET", f"{P}/customer/{other}"))["opportunity"] is None

    # 记一次跟进：不填下次跟进日期时按设置的默认天数；第一次跟进自动推进到"已沟通"。
    await call(desk, "PUT", f"{P}/settings", ai_mode="auto", min_stage=3, follow_days=5)
    followed = await call(
        desk,
        "POST",
        f"{P}/{made['id']}/followups",
        method="phone",
        content="电话沟通，客户想等国庆活动",
    )
    assert followed["follow_count"] == 1 and followed["last_followed_at"]
    assert followed["next_follow_at"] == str(day + timedelta(days=5))
    assert (followed["stage_code"], followed["probability"]) == ("contacted", 30)
    assert kinds(followed) == ["created", "followup", "stage"]
    [entry] = of_kind(followed, "followup")
    assert (entry["method"], entry["staff_name"], entry["content"]) == (
        "phone",
        "管理员",
        "电话沟通，客户想等国庆活动",
    )
    [advanced] = of_kind(followed, "stage")
    assert advanced["staff_id"] is None and advanced["properties"]["auto"] is True
    assert (advanced["properties"]["from"], advanced["properties"]["to"]) == ("new", "contacted")
    await call(desk, "POST", f"{P}/{made['id']}/followups", 422, content="  ")
    await call(
        desk, "POST", f"{P}/{made['id']}/followups", 422, content="x", next_follow_at=yesterday
    )
    # 备注不改下次跟进日期。
    noted = await call(
        desk, "POST", f"{P}/{made['id']}/activities", kind="note", content="客户是老板本人"
    )
    assert noted["next_follow_at"] == followed["next_follow_at"] and kinds(noted)[-1] == "note"
    updated = await call(
        desk,
        "PATCH",
        f"{P}/{made['id']}",
        level="medium",
        next_follow_at=str(day),
        amount="12000",
        expected_close_at=str(day + timedelta(days=10)),
    )
    assert updated["level"] == "medium" and updated["due_today"] is True
    assert updated["amount"] == "12000.00" and updated["expected_close_at"] == str(
        day + timedelta(days=10)
    )
    amount_change, close_change = of_kind(updated, "field")
    assert amount_change["title"] == "预计金额：（空） → 12000.00"
    assert amount_change["properties"] == {"field": "amount", "from": None, "to": "12000.00"}
    assert close_change["title"].startswith("预计成交日：（空） → ")
    page = await listing(desk, view="today")
    assert [i["id"] for i in page["items"]] == [made["id"]]
    assert (page["counts"]["active"], page["counts"]["today"], page["counts"]["overdue"]) == (
        1,
        1,
        0,
    )
    assert page["counts"]["closing"] == 1 and page["amount_visible"] is True
    assert (await listing(desk, q="王先生"))["total"] == 1
    assert (await listing(desk, q="门锁"))["total"] == 1
    assert (await listing(desk, q="没有这个人"))["total"] == 0

    # 这个客户的订单确认后自动赢单，记下订单；预计金额已经填了就不改。
    products = await catalog(desk)
    x1 = [{"product_id": products["LOCK-X1"], "quantity": 1}]
    order = await new_order(desk, desk.admin, customer_id, x1)
    await call(
        desk,
        "POST",
        f"/api/v1/orders/{order['id']}/confirm",
        payment_method="cod",
        notify_customer=False,
    )
    won = await call(desk, "GET", f"{P}/{made['id']}")
    assert (won["status"], won["stage_code"], won["order_id"], won["order_no"]) == (
        "won",
        "won",
        order["id"],
        order["no"],
    )
    assert won["amount"] == "12000.00"
    # 新建的订单直接提交审核：时间线先记一条提交审核（并自动推进到已报价），确认后自动赢单。
    submitted, confirmed = of_kind(won, "order")
    assert submitted["title"] == f"订单 {order['no']} 提交审核，金额 1299.00 元"
    assert of_kind(won, "stage")[-1]["properties"]["to"] == "quoted"
    assert confirmed["title"] == f"订单 {order['no']} 已确认，自动赢单"
    assert confirmed["properties"]["from"] == "quoted"
    assert (confirmed["linked_type"], confirmed["linked_id"]) == ("order", order["id"])
    page = await listing(desk, view="won")
    assert page["total"] == 1 and page["won_this_month"] == 1
    assert (await tags(desk))[customer_id] == (None, None)
    info = await call(desk, "GET", f"{P}/customer/{customer_id}")
    assert info["opportunity"]["status"] == "won" and info["recent_deal_at"] is not None
    # 已赢单的不能记跟进、不能输单、不能换阶段。
    await call(desk, "POST", f"{P}/{made['id']}/followups", 409, content="再联系")
    await call(desk, "POST", f"{P}/{made['id']}/lost", 409, reason_code="price")
    stages = await stage_ids(desk)
    await call(desk, "POST", f"{P}/{made['id']}/stage", 409, stage_id=stages["quoted"])

    # 重新跟进赢单的：新开一条商机（原记录保留），之前的订单不再算成交。
    fresh = await call(desk, "POST", f"{P}/{made['id']}/reopen")
    assert fresh["id"] != made["id"] and fresh["status"] == "active" and fresh["closed_at"] is None
    assert (fresh["stage_code"], fresh["name"]) == ("new", "王先生 的商机")
    assert fresh["next_follow_at"] == str(day + timedelta(days=5))
    assert (await scan(desk))["won"] == 0
    assert (await call(desk, "GET", f"{P}/{made['id']}"))["status"] == "won"
    assert (await call(desk, "GET", f"{P}/{fresh['id']}"))["status"] == "active"
    # 输单要选设置里的原因；输单的可以再重新跟进（回到已沟通）。
    await call(desk, "POST", f"{P}/{fresh['id']}/lost", 422, reason_code="nope")
    lost = await call(
        desk, "POST", f"{P}/{fresh['id']}/lost", reason_code="competitor", reason="已经买了别家的"
    )
    assert (lost["status"], lost["stage_code"], lost["lost_reason_code"]) == (
        "lost",
        "lost",
        "competitor",
    )
    assert (lost["lost_reason_name"], lost["lost_reason"]) == ("竞品", "已经买了别家的")
    again = await call(desk, "POST", f"{P}/{fresh['id']}/reopen", next_follow_at=str(day))
    assert (again["status"], again["stage_code"], again["lost_reason"]) == (
        "active",
        "contacted",
        None,
    )
    assert again["due_today"] is True and kinds(again)[-1] == "stage"
    # 手动赢单（关联的订单要是这个客户的）。
    other_order = await new_order(desk, desk.admin, other, x1)
    await call(desk, "POST", f"{P}/{fresh['id']}/won", 422, order_id=other_order["id"])
    manual = await call(desk, "POST", f"{P}/{fresh['id']}/won", order_id=order["id"])
    assert manual["status"] == "won" and manual["order_id"] == order["id"]
    assert manual["amount"] == order["total"]

    assert await audit_actions(desk) == [
        "opportunity.create",
        "opportunity.settings",
        "opportunity.reopen",
        "opportunity.lost",
        "opportunity.reopen",
        "opportunity.won",
    ]
    # 个人信息查询带上商机和跟进记录（跟进、备注、咨询）。
    data = await call(
        desk, "POST", f"/api/v1/customers/{customer_id}/personal-data", reason="客户来电查询"
    )
    first, second = data["opportunities"]
    assert (first["status"], first["level"]) == ("赢单", "中")
    assert first["interest"] == "智能门锁 X1，10 把"
    assert [(f["method"], f["staff"]) for f in first["followups"]] == [
        ("电话", "管理员"),
        ("其他", "管理员"),
    ]
    assert second["status"] == "赢单" and second["followups"] == []


async def test_stages_board_moves_and_stats(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    stages = await call(desk, "GET", f"{P}/stages")
    assert [s["code"] for s in stages] == [
        "new",
        "contacted",
        "quoted",
        "negotiating",
        "won",
        "lost",
    ]
    assert [s["kind"] for s in stages][-2:] == ["won", "lost"]
    ids = {s["code"]: s["id"] for s in stages}
    # 新增、修改、排序阶段（需要分配商机的权限）。
    await call(desk, "POST", f"{P}/stages", 403, headers=alice.headers, name="方案演示")
    demo = await call(
        desk, "POST", f"{P}/stages", 201, name="方案演示", probability=45, after_id=ids["contacted"]
    )
    assert (demo["position"], demo["kind"], demo["stale_days"]) == (2, "open", 7)
    assert [s["code"] for s in await call(desk, "GET", f"{P}/stages")][:4] == [
        "new",
        "contacted",
        demo["code"],
        "quoted",
    ]
    renamed = await call(
        desk,
        "PATCH",
        f"{P}/stages/{demo['id']}",
        name="演示",
        probability=50,
        clear_stale_days=True,
    )
    assert (renamed["name"], renamed["probability"], renamed["stale_days"]) == ("演示", 50, None)
    ordered = await call(
        desk,
        "PUT",
        f"{P}/stages/order",
        ids=[ids["new"], demo["id"], ids["contacted"], ids["quoted"], ids["negotiating"]],
    )
    assert [s["code"] for s in ordered][:3] == ["new", demo["code"], "contacted"]
    await call(desk, "PUT", f"{P}/stages/order", 422, ids=[ids["new"]])
    await call(desk, "DELETE", f"{P}/stages/{ids['won']}", 422)

    # 三条商机：两条在新线索、一条拖到演示；看板按列汇总。
    names = ("客户甲", "客户乙", "客户丙")
    customers = [await customer(desk, n) for n in names]
    made = [
        await call(desk, "POST", P, 201, customer_id=c, name=f"{n}的项目", amount=amount)
        for c, n, amount in zip(customers, names, ("1000", "2500.5", None), strict=True)
    ]
    moved = await call(desk, "POST", f"{P}/{made[2]['id']}/stage", stage_id=demo["id"], position=3)
    assert (moved["stage_code"], moved["stage_name"]) == (demo["code"], "演示")
    [stage_change] = of_kind(moved, "stage")
    assert (stage_change["properties"]["from"], stage_change["properties"]["to"]) == (
        "new",
        demo["code"],
    )
    assert stage_change["title"] == "新线索 → 演示"
    board = await call(desk, "GET", f"{P}/board")
    columns = {c["stage"]["code"]: c for c in board["columns"]}
    assert [c["stage"]["code"] for c in board["columns"]][:3] == ["new", demo["code"], "contacted"]
    assert (columns["new"]["total"], columns["new"]["amount_sum"]) == (2, "3500.50")
    assert [i["name"] for i in columns["new"]["items"]] == ["客户甲的项目", "客户乙的项目"]
    assert (columns[demo["code"]]["total"], columns[demo["code"]]["amount_sum"]) == (1, None)
    assert columns["won"]["total"] == 0 and board["amount_visible"] is True
    # 拖到输单要选原因；拖到赢单可以带订单。
    await call(desk, "POST", f"{P}/{made[0]['id']}/stage", 422, stage_id=ids["lost"])
    lost = await call(
        desk, "POST", f"{P}/{made[0]['id']}/stage", stage_id=ids["lost"], lost_reason_code="price"
    )
    assert (lost["status"], lost["lost_reason_name"]) == ("lost", "价格")
    products = await catalog(desk)
    order = await new_order(
        desk, desk.admin, customers[1], [{"product_id": products["LOCK-X1"], "quantity": 2}]
    )
    won = await call(
        desk, "POST", f"{P}/{made[1]['id']}/stage", stage_id=ids["won"], order_id=order["id"]
    )
    assert (won["status"], won["order_no"], won["amount"]) == ("won", order["no"], "2500.50")
    board = await call(desk, "GET", f"{P}/board")
    columns = {c["stage"]["code"]: c for c in board["columns"]}
    assert (columns["won"]["total"], columns["lost"]["total"], columns["new"]["total"]) == (1, 1, 0)
    assert columns["won"]["amount_sum"] == "2500.50"

    # 删除还有商机的阶段要指定并到哪个阶段；自动推进里指向它的规则清掉。
    await call(desk, "PUT", f"{P}/settings", auto_advance={"first_followup": demo["code"]})
    await call(desk, "DELETE", f"{P}/stages/{demo['id']}", 422)
    await call(desk, "DELETE", f"{P}/stages/{demo['id']}?merge_into={ids['quoted']}", 204)
    assert (await call(desk, "GET", f"{P}/{made[2]['id']}"))["stage_code"] == "quoted"
    settings = await call(desk, "GET", f"{P}/settings")
    assert settings["settings"]["auto_advance"]["first_followup"] is None
    assert [s["code"] for s in settings["stages"]] == [
        "new",
        "contacted",
        "quoted",
        "negotiating",
        "won",
        "lost",
    ]

    # 顶部数字和停滞：把丙的进入阶段时间改到 10 天前。
    await desk.sql(
        "UPDATE opportunities SET stage_entered_at = now() - interval '10 days',"
        " last_activity_at = now() - interval '10 days' WHERE id = $1",
        uuid.UUID(made[2]["id"]),
    )
    stats = await call(desk, "GET", f"{P}/stats")
    assert (stats["active"], stats["stale"], stats["won_this_month"]) == (1, 1, 1)
    assert stats["won_amount_this_month"] == "2500.50"
    page = await listing(desk, view="stale")
    assert [i["id"] for i in page["items"]] == [made[2]["id"]] and page["items"][0]["stale"] is True
    assert page["items"][0]["days_in_stage"] == 10
    assert (await listing(desk, stale=True))["total"] == 1

    # 预计金额只有管理者可见时，客服看不到金额。
    await call(desk, "PUT", f"{P}/settings", amount_visibility="managers")
    mine = await call(
        desk, "POST", "/api/v1/customers", 201, headers=alice.headers, display_name="孙先生"
    )
    own = await call(
        desk, "POST", P, 201, headers=alice.headers, customer_id=mine["id"], amount="300"
    )
    assert own["amount"] is None and own["amount_visible"] is False
    assert (await call(desk, "GET", f"{P}/{own['id']}"))["amount"] == "300.00"
    board = await call(desk, "GET", f"{P}/board", headers=alice.headers)
    assert board["amount_visible"] is False and all(
        c["amount_sum"] is None for c in board["columns"]
    )


async def test_ai_adopts_closed_sessions_and_records_returns(desk: Desk, fake_llm: FakeLLM) -> None:
    alice = await desk.agent("alice")
    visitor, customer_id, session_id = await closed_chat(
        desk, alice, "智能门锁多少钱？有点贵，我考虑一下，下周再说", 3
    )
    # 意向不够（有兴趣，设置要求意向明确）的不转入。
    await call(desk, "PUT", f"{P}/settings", ai_mode="auto", min_stage=4, follow_days=3)
    assert (await scan(desk))["created"] == 0
    await call(desk, "PUT", f"{P}/settings", ai_mode="auto", min_stage=3, follow_days=3)
    fake_llm.requests.clear()
    assert await scan(desk) == {"won": 0, "returns": 0, "ready": 0, "created": 1, "suggested": 0}
    [item] = (await listing(desk))["items"]
    day = await today(desk)
    assert (item["source"], item["status"], item["level"]) == ("ai", "active", "medium")
    assert item["customer_id"] == customer_id and item["session_id"] == session_id
    assert item["owner_id"] == str(alice.staff_id) and item["stage_code"] == "new"
    assert item["interest"] == "智能门锁多少钱？有点贵，我考虑一下，下周再说"
    assert item["name"] == item["interest"] and item["concerns"] == "价格、还要考虑"
    # 客户说下周再说：7 天后跟进。
    assert item["next_follow_at"] == str(day + timedelta(days=7))
    # AI 读的是脱敏后的会话；用量记在"商机"场景；时间线上记一条 AI 转入。
    [chat] = [r for r in fake_llm.requests if "messages" in r]
    assert chat["messages"][0]["content"].startswith("任务：整理意向客户")
    [row] = await desk.sql(
        "SELECT status FROM llm_calls WHERE tenant_id = $1 AND scene = 'opportunity'",
        desk.tenant_id,
    )
    assert row["status"] == "ok"
    detail = await call(desk, "GET", f"{P}/{item['id']}")
    assert kinds(detail) == ["ai"] and detail["activities"][0]["title"] == "AI 按会话转入"
    # 已经在名单里：不再转入。
    assert (await scan(desk))["created"] == 0

    # 客户又来咨询了：时间线上记一条（系统记的），意向更高时调高等级；同一会话只记一次。意图第一次
    # 到"准备下单"也记一条（每条商机一次）。
    _, _, again = await closed_chat(desk, alice, "我要下单，地址是浦东新区", 4, visitor=visitor)
    first_scan = await scan(desk)
    assert (first_scan["returns"], first_scan["ready"]) == (1, 1)
    second_scan = await scan(desk)
    assert (second_scan["returns"], second_scan["ready"]) == (0, 0)
    detail = await call(desk, "GET", f"{P}/{item['id']}")
    ready, entry = sorted(of_kind(detail, "session"), key=lambda a: a["linked_type"] or "")
    assert (entry["method"], entry["staff_id"], entry["session_id"]) == ("chat", None, again)
    assert entry["content"].startswith("客户又来咨询了（准备下单）")
    assert (ready["title"], ready["linked_type"], ready["linked_id"]) == (
        "客户准备下单了（意图判断）",
        "intent",
        again,
    )
    assert detail["level"] == "high" and detail["follow_count"] == 0

    # 只建议：AI 的建议员工确认后才进名单；忽略的 30 天内不再建议。
    await call(desk, "PUT", f"{P}/settings", ai_mode="suggest", min_stage=2, follow_days=3)
    bob_visitor, bob_customer, _ = await closed_chat(desk, alice, "门铃有货吗", 3)
    assert (await scan(desk))["suggested"] == 1
    page = await listing(desk, view="suggested")
    [suggestion] = page["items"]
    assert suggestion["status"] == "suggested" and page["counts"]["suggested"] == 1
    assert (await tags(desk))[bob_customer] == ("suggested", "新线索")
    # 待确认的商机不能换阶段，先确认。
    await call(desk, "POST", f"{P}/{suggestion['id']}/followups", 409, content="x")
    accepted = await call(desk, "POST", f"{P}/{suggestion['id']}/accept")
    assert accepted["status"] == "active" and kinds(accepted) == ["ai", "created"]
    await call(desk, "POST", f"{P}/{suggestion['id']}/accept", 409)
    await call(desk, "POST", f"{P}/{suggestion['id']}/lost", reason_code="other", reason="只是问问")

    _, carol_customer, _ = await closed_chat(desk, alice, "规格有哪些", 2)
    # 刚输单的客户 30 天内不再建议（bob），新的客户照常建议（carol）。
    await closed_chat(desk, alice, "门铃还有货吗", 3, visitor=bob_visitor)
    assert (await scan(desk))["suggested"] == 1
    [carol] = (await listing(desk, view="suggested"))["items"]
    assert carol["customer_id"] == carol_customer
    await call(desk, "POST", f"{P}/{carol['id']}/dismiss", 204)
    await call(desk, "GET", f"{P}/{carol['id']}", 404)
    assert (await listing(desk, view="all"))["counts"]["suggested"] == 0
    assert (await scan(desk))["suggested"] == 0

    # 关闭 AI 转入后不再转入。
    await call(desk, "PUT", f"{P}/settings", ai_mode="off", min_stage=2, follow_days=3)
    await closed_chat(desk, alice, "门锁怎么买", 3)
    assert (await scan(desk))["created"] + (await scan(desk))["suggested"] == 0

    # 大模型不可用时：用意图判断"在意什么"和默认天数。
    await call(desk, "PUT", f"{P}/settings", ai_mode="auto", min_stage=2, follow_days=4)
    fake_llm.mode = "down"
    _, dave_customer, _ = await closed_chat(
        desk, alice, "可视门铃怎么卖", 3, concerns=("delivery",)
    )
    assert (await scan(desk))["created"] >= 1
    fake_llm.mode = "normal"
    [dave] = (await listing(desk, customer_id=dave_customer))["items"]
    assert dave["interest"].startswith("关心发货时效") and dave["concerns"] is None
    assert dave["next_follow_at"] == str(day + timedelta(days=4))


async def test_message_scope_permissions_and_settings(desk: Desk, fake_llm: FakeLLM) -> None:
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)
    mine = await call(
        desk, "POST", "/api/v1/customers", 201, headers=alice.headers, display_name="孙先生"
    )
    made = await call(
        desk,
        "POST",
        P,
        201,
        headers=alice.headers,
        customer_id=mine["id"],
        interest="智能门锁",
        concerns="价格",
    )
    assert made["owner_id"] == str(alice.staff_id) and made["can_assign"] is False
    # 看不到这个客户的员工看不到、也不能跟进他的商机。
    assert (await listing(desk, headers=bob.headers))["total"] == 0
    await call(desk, "GET", f"{P}/{made['id']}", 404, headers=bob.headers)
    await call(desk, "POST", f"{P}/{made['id']}/followups", 404, headers=bob.headers, content="x")
    await call(desk, "POST", P, 404, headers=bob.headers, customer_id=mine["id"])
    # 把负责人改成别人需要分配商机的权限；设置和阶段也是。
    await call(
        desk,
        "PATCH",
        f"{P}/{made['id']}",
        403,
        headers=alice.headers,
        owner_id=str(bob.staff_id),
    )
    await call(
        desk,
        "POST",
        f"{P}/{made['id']}/assign",
        403,
        headers=alice.headers,
        owner_id=str(bob.staff_id),
    )
    moved = await call(desk, "POST", f"{P}/{made['id']}/assign", owner_id=str(bob.staff_id))
    assert moved["owner_name"] == "Bob" and of_kind(moved, "owner")[0]["title"] == "负责人改为 Bob"
    # 负责人总能看到自己负责的（客户不归他时也能看到、跟进），客户的归属坐席也还能看到。
    assert (await listing(desk, headers=bob.headers))["total"] == 1
    assert (await listing(desk, headers=bob.headers, view="mine"))["total"] == 1
    assert (await listing(desk, headers=alice.headers, view="mine"))["total"] == 0
    await call(desk, "POST", f"{P}/{made['id']}/followups", headers=bob.headers, content="电话回访")
    assert (await listing(desk, headers=alice.headers))["total"] == 1
    settings = await call(desk, "GET", f"{P}/settings", headers=alice.headers)
    assert settings["can_edit"] is False and len(settings["stages"]) == 6
    assert settings["settings"]["ai_mode"] == "auto" and settings["settings"]["follow_days"] == 3
    assert settings["settings"]["auto_advance"] == {
        "first_followup": "contacted",
        "quote": "quoted",
        "contract_final": "negotiating",
    }
    assert [r["code"] for r in settings["settings"]["lost_reasons"]] == [
        "price",
        "competitor",
        "no_need",
        "no_response",
        "other",
    ]
    await call(
        desk,
        "PUT",
        f"{P}/settings",
        403,
        headers=alice.headers,
        ai_mode="off",
        min_stage=2,
        follow_days=3,
    )
    await call(desk, "PUT", f"{P}/settings", 422, ai_mode="auto", min_stage=1, follow_days=3)
    await call(desk, "PUT", f"{P}/settings", 422, lost_reasons=[])

    # AI 写跟进话术：参考知识库，员工修改后自己发送。
    await call(
        desk,
        "POST",
        "/api/v1/kb/items",
        201,
        kind="faq",
        title="智能门锁国庆活动",
        content="国庆期间智能门锁 9 折。活动到 10 月 7 日。",
        publish=True,
    )
    fake_llm.requests.clear()
    message = await call(desk, "POST", f"{P}/{made['id']}/message", headers=alice.headers)
    assert message["text"].startswith("孙先生您好！上次您问到的智能门锁")
    assert "国庆期间智能门锁 9 折。" in message["text"]
    assert message["knowledge"] == ["智能门锁国庆活动"]
    [chat] = [r for r in fake_llm.requests if "messages" in r]
    assert chat["messages"][0]["content"].startswith("任务：意向客户跟进话术")
    fake_llm.mode = "down"
    await call(desk, "POST", f"{P}/{made['id']}/message", 503, headers=alice.headers)
    fake_llm.mode = "normal"
    # 套餐不包含 AI 时不能写话术。
    await desk.sql(
        "UPDATE tenants SET settings = jsonb_set(coalesce(settings, '{}'), '{features}',"
        " '{\"ai\": false}') WHERE id = $1",
        desk.tenant_id,
    )
    await call(desk, "POST", f"{P}/{made['id']}/message", 403, headers=alice.headers)
    # 过渡期的别名接口返回同样的数据。
    assert (await call(desk, "GET", f"/api/v1/prospects/{made['id']}"))["id"] == made["id"]


async def test_due_check_groups_by_owner_and_merge_keeps_one_record(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    names = ("客户甲", "客户乙", "客户丙")
    customers = [
        (await call(desk, "POST", "/api/v1/customers", 201, headers=alice.headers, display_name=n))[
            "id"
        ]
        for n in names
    ]
    made = [
        await call(desk, "POST", P, 201, headers=alice.headers, customer_id=c, interest=n)
        for c, n in zip(customers, names, strict=True)
    ]
    day = await today(desk)
    # 甲过了 5 天、乙今天该跟进、丙还没到。
    for opportunity, due in zip(
        made, (day - timedelta(days=5), day, day + timedelta(days=2)), strict=True
    ):
        await desk.sql(
            "UPDATE opportunities SET next_follow_at = $1 WHERE id = $2",
            due,
            uuid.UUID(opportunity["id"]),
        )
    page = await listing(desk, headers=alice.headers)
    assert (page["counts"]["overdue"], page["counts"]["today"]) == (1, 1)

    await wake(desk, "daily", trigger="manual")
    found = {k: v for k, v in (await findings(desk)).items() if k.startswith("prospect_due")}
    [finding] = found.values()
    assert finding["title"] == "Alice 有 2 条商机该跟进了"
    assert finding["severity"] == "critical" and finding["assignee_ids"] == [alice.staff_id]
    assert finding["detail"].startswith("客户甲、客户乙。其中 1 位已经过了下次跟进日期")
    assert finding["link"] == f"/opportunities?view=overdue&owner={alice.staff_id}"
    assert json.loads(finding["data"]) == {"due": 2, "overdue": 1}

    # 跟进以后自动消除。
    for opportunity in made[:2]:
        await call(
            desk,
            "POST",
            f"{P}/{opportunity['id']}/followups",
            headers=alice.headers,
            content="回访",
        )
    await wake(desk, "daily", trigger="manual")
    found = {k: v for k, v in (await findings(desk)).items() if k.startswith("prospect_due")}
    assert [f["status"] for f in found.values()] == ["resolved"]

    # 合并客户：同一客户只留一条跟进中的商机，时间线并过来；合同也并过来。
    contract = await call(
        desk, "POST", "/api/v1/contracts", 201, title="门锁采购合同", customer_id=customers[1]
    )
    await call(
        desk,
        "POST",
        f"/api/v1/customers/{customers[0]}/merge",
        source_ids=[customers[1], customers[2]],
    )
    rows = await listing(desk, view="all")
    [kept] = rows["items"]
    assert kept["customer_id"] == customers[0] and kept["interest"] == "客户甲"
    assert kept["follow_count"] == 2
    detail = await call(desk, "GET", f"{P}/{kept['id']}")
    assert len(of_kind(detail, "followup")) == 2 and len(of_kind(detail, "created")) == 3
    # 下次跟进取早的。
    assert kept["next_follow_at"] == min(
        p["next_follow_at"] for p in (await listing(desk))["items"]
    )
    moved = await call(desk, "GET", f"/api/v1/contracts/{contract['id']}")
    assert moved["customer_id"] == customers[0]
    [audit] = await desk.sql(
        "SELECT detail FROM audit_logs WHERE tenant_id = $1 AND action = 'customer.merge'",
        desk.tenant_id,
    )
    moved_counts = json.loads(audit["detail"])["moved"]
    assert moved_counts["opportunities"] == 2 and moved_counts["contracts"] == 1


async def test_orders_contracts_and_todos_write_the_timeline_and_advance(
    desk: Desk, fake_llm: FakeLLM
) -> None:
    """安排下一步的待办、"报价"待办完成 → 已报价；订单提交审核和收款记一条；合同起草挂上、定稿 →
    谈判中、签署 → 赢单；换负责人提醒新负责人；AI 小结；导出；推送事件。"""
    alice = await desk.agent("alice", online=False)
    products = await catalog(desk)
    customer_id = await customer(desk, "周总")
    day = await today(desk)
    made = await call(desk, "POST", P, 201, customer_id=customer_id, interest="门锁 20 把")
    assert (made["stage_code"], made["todos"]) == ("new", [])

    # 安排下一步：建一条关联这条商机的待办（默认回电 / 回访，处理人默认是负责人），同时改下次跟进
    # 日期；详情里显示没完成的待办，时间线记一条。
    due = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    planned = await call(
        desk,
        "POST",
        f"{P}/{made['id']}/todos",
        title="回访周总",
        due_at=due,
        next_follow_at=str(day + timedelta(days=1)),
    )
    [todo] = planned["todos"]
    assert (todo["title"], todo["type_name"], todo["assignee_name"], todo["status"]) == (
        "回访周总",
        "回电 / 回访",
        "管理员",
        "open",
    )
    assert planned["next_follow_at"] == str(day + timedelta(days=1))
    [created] = of_kind(planned, "todo")
    assert created["title"] == f"待办 {todo['no']}：回访周总"
    assert (created["linked_type"], created["linked_id"], created["staff_name"]) == (
        "todo",
        todo["id"],
        "管理员",
    )
    await call(desk, "POST", f"{P}/{made['id']}/todos", 422, type_code="nope")

    # "报价"待办完成后自动推进到已报价；完成的结果记在时间线上。
    quoted = await call(desk, "POST", f"{P}/{made['id']}/todos", type_code="quote", title="报价")
    quote_todo = next(t for t in quoted["todos"] if t["title"] == "报价")
    await call(desk, "POST", f"/api/v1/todos/{quote_todo['id']}/done", result="已报 2 万")
    detail = await call(desk, "GET", f"{P}/{made['id']}")
    assert detail["stage_code"] == "quoted"
    assert [t["title"] for t in detail["todos"]] == ["回访周总"]
    done = of_kind(detail, "todo")[-1]
    assert done["title"] == f"完成待办 {quote_todo['no']}：报价" and done["content"] == "已报 2 万"
    advanced = of_kind(detail, "stage")[-1]
    assert advanced["properties"]["auto"] is True
    assert advanced["properties"]["reason"] == f"报价待办 {quote_todo['no']} 完成"

    # 订单提交审核：时间线记一条（已经在已报价，不再推进）；登记收款也记一条。
    order = await new_order(
        desk,
        desk.admin,
        customer_id,
        [{"product_id": products["LOCK-X1"], "quantity": 2}],
        submit=True,
    )
    await call(
        desk, "POST", f"/api/v1/orders/{order['id']}/payments", amount="500", channel="wechat"
    )
    detail = await call(desk, "GET", f"{P}/{made['id']}")
    [submitted] = of_kind(detail, "order")
    assert submitted["title"] == f"订单 {order['no']} 提交审核，金额 2598.00 元"
    assert (submitted["linked_type"], submitted["linked_id"]) == ("order", order["id"])
    [paid] = of_kind(detail, "payment")
    assert (
        paid["title"] == f"订单 {order['no']} 收款 500.00 元" and detail["stage_code"] == "quoted"
    )

    # 合同：起草的挂到这条商机上；定稿自动推进到谈判中；签署自动赢单（预计金额取合同金额）。
    contract = await call(
        desk,
        "POST",
        "/api/v1/contracts",
        201,
        title="门锁采购合同",
        customer_id=customer_id,
        order_id=order["id"],
    )
    detail = await call(desk, "GET", f"{P}/{made['id']}")
    assert (detail["contract_id"], detail["contract_no"]) == (contract["id"], contract["no"])
    [drafted] = of_kind(detail, "contract")
    assert drafted["title"] == f"起草合同 {contract['no']}《门锁采购合同》"
    await call(desk, "POST", f"/api/v1/contracts/{contract['id']}/finalize")
    detail = await call(desk, "GET", f"{P}/{made['id']}")
    assert detail["stage_code"] == "negotiating"
    assert of_kind(detail, "stage")[-1]["properties"]["reason"] == f"合同 {contract['no']} 定稿"
    await call(desk, "POST", f"/api/v1/contracts/{contract['id']}/sign", sign_date=str(day))
    detail = await call(desk, "GET", f"{P}/{made['id']}")
    assert (detail["status"], detail["stage_code"], detail["amount"]) == ("won", "won", "2598.00")
    signed = of_kind(detail, "contract")[-1]
    assert signed["title"] == f"合同 {contract['no']} 已签署，自动赢单"
    assert (signed["properties"]["from"], signed["properties"]["to"]) == ("negotiating", "won")
    events = [e for e in await outbox(desk) if e.startswith("opportunity.")]
    assert events == [
        "opportunity.created",
        "opportunity.stage_changed",
        "opportunity.stage_changed",
        "opportunity.won",
    ]

    # 换负责人：站内信提醒新负责人；交给自己不提醒。
    await call(desk, "POST", f"{P}/{made['id']}/assign", owner_id=str(alice.staff_id))
    assert await notes(desk, "opportunity") == [
        ("商机「门锁 20 把」交给你负责", "管理员 把这条商机交给你负责")
    ]
    assert (await outbox(desk))[-1] == "opportunity.assigned"

    # AI 小结：三句话，记进时间线，用量记在"商机"场景。
    fake_llm.requests.clear()
    digest = await call(desk, "POST", f"{P}/{made['id']}/summary")
    assert digest["status"].startswith("门锁 20 把现在在赢单")
    assert (digest["cares"], digest["next"]) == ("没有记录", "建议约时间报价")
    assert digest["text"] == f"{digest['status']} {digest['cares']} {digest['next']}"
    [chat] = [r for r in fake_llm.requests if "messages" in r]
    assert chat["messages"][0]["content"].startswith("任务：商机小结")
    assert "【时间线】" in chat["messages"][1]["content"]
    detail = await call(desk, "GET", f"{P}/{made['id']}")
    [noted] = of_kind(detail, "ai")
    assert noted["title"] == "AI 小结" and noted["content"] == digest["text"]

    # 导出（opportunity:export）：CSV；客服没有权限。
    exported = await desk.client.get(f"{P}/export", headers=desk.admin, params={"view": "all"})
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"].startswith("text/csv")
    lines = exported.text.lstrip("﻿").splitlines()
    assert lines[0].startswith("客户,公司,商机,阶段,状态")
    assert len(lines) == 2 and lines[1].startswith("周总,,门锁 20 把,赢单,赢单,中,2598.00")
    denied = await desk.client.get(f"{P}/export", headers=alice.headers)
    assert denied.status_code == 403


async def test_open_api_leads_are_assigned_in_turn_and_pushed(desk: Desk) -> None:
    """企业系统创建线索：按手机号找到或者新建客户，负责人按设置轮流分给技能组；查询和回传；推送事件。"""
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)
    group = await call(
        desk,
        "POST",
        "/api/v1/skill-groups",
        201,
        name="销售",
        members=[{"staff_id": str(alice.staff_id)}, {"staff_id": str(bob.staff_id)}],
    )
    await call(
        desk,
        "PUT",
        f"{P}/settings",
        ai_mode="auto",
        min_stage=2,
        follow_days=3,
        assignment="round_robin",
        assignment_group_id=group["id"],
    )
    key = await api_key(desk, "opportunities:read", "opportunities:write")
    reader = await api_key(desk, "orders:read")

    async def post(body: dict[str, Any], expected: int = 201) -> Any:
        response = await desk.client.post("/open/v1/opportunities", headers=bearer(key), json=body)
        assert response.status_code == expected, response.text
        return response.json()

    async def patch(opportunity_id: str, body: dict[str, Any], expected: int = 200) -> Any:
        response = await desk.client.patch(
            f"/open/v1/opportunities/{opportunity_id}", headers=bearer(key), json=body
        )
        assert response.status_code == expected, response.text
        return response.json()

    first = await post(
        {
            "customer": {"name": "官网线索甲", "phone": "13800009001"},
            "interest": "想买 10 把门锁",
            "amount": "9999",
        }
    )
    assert (first["status"], first["stage"], first["source"], first["level"]) == (
        "active",
        "new",
        "api",
        "medium",
    )
    assert (first["owner_username"], first["amount"], first["customer_name"]) == (
        "alice",
        "9999.00",
        "官网线索甲",
    )
    second = await post(
        {"customer": {"name": "官网线索乙", "phone": "13800009002"}, "name": "乙的商机"}
    )
    assert second["owner_username"] == "bob" and second["name"] == "乙的商机"
    third = await post({"customer": {"name": "官网线索丙", "phone": "13800009003"}})
    assert third["owner_username"] == "alice" and third["name"] == "官网线索丙 的商机"
    # 同一客户（按手机号）已经有进行中的商机：返回已有的（200）；指定的负责人不存在时 422。
    again = await post({"customer": {"name": "官网线索甲", "phone": "13800009001"}}, 200)
    assert again["id"] == first["id"]
    await post(
        {"customer": {"name": "丁", "phone": "13800009004"}, "owner_username": "nobody"}, 422
    )
    await post({"name": "没有客户"}, 422)
    # 控制台里来源是"企业系统"，负责人看得到自己负责的。
    page = await listing(desk, headers=alice.headers)
    assert sorted(i["customer_name"] for i in page["items"]) == ["官网线索丙", "官网线索甲"]
    assert {i["source"] for i in page["items"]} == {"api"}

    # 查询：增量同步和单条；没有这个权限范围的密钥 403。
    listed = await desk.client.get(
        "/open/v1/opportunities", headers=bearer(key), params={"status": "active"}
    )
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["items"]) == 3 and listed.json()["next_cursor"] is None
    one = await desk.client.get(f"/open/v1/opportunities/{first['id']}", headers=bearer(key))
    assert one.status_code == 200 and one.json()["name"] == "想买 10 把门锁"
    denied = await desk.client.get("/open/v1/opportunities", headers=bearer(reader))
    assert denied.status_code == 403

    # 回传：换阶段、改金额；赢单、输单（要原因分类）；已关闭的不能再回传。
    moved = await patch(first["id"], {"stage": "quoted", "amount": "12000"})
    assert (moved["stage"], moved["amount"], moved["probability"]) == ("quoted", "12000.00", 60)
    await patch(first["id"], {"stage": "won"}, 422)
    await patch(first["id"], {"status": "lost"}, 422)
    lost = await patch(
        first["id"], {"status": "lost", "lost_reason_code": "price", "lost_reason": "预算不够"}
    )
    assert (lost["status"], lost["stage"], lost["lost_reason_code"]) == ("lost", "lost", "price")
    won = await patch(second["id"], {"status": "won"})
    assert won["status"] == "won" and won["closed_at"] is not None
    await patch(second["id"], {"status": "won"}, 409)
    detail = await call(desk, "GET", f"{P}/{first['id']}")
    assert [a["title"] for a in reversed(detail["activities"])] == [
        "企业系统转入，阶段：新线索",
        "新线索 → 已报价（企业系统回传）",
        "输单：价格（企业系统回传）",
    ]
    assert all(a["staff_id"] is None for a in detail["activities"])
    events = [e for e in await outbox(desk) if e.startswith("opportunity.")]
    assert events == [
        "opportunity.created",
        "opportunity.created",
        "opportunity.created",
        "opportunity.stage_changed",
        "opportunity.lost",
        "opportunity.won",
    ]


async def test_wake_flags_stale_overdue_and_unattended_opportunities(desk: Desk) -> None:
    """AI 唤醒：新线索没人跟、商机停滞（超过 2 倍严重）、预计成交日已过；处理后自动消除。"""
    names = ("甲", "乙", "丙", "丁")
    customers = [await customer(desk, n) for n in names]
    made = [
        await call(desk, "POST", P, 201, customer_id=c, interest=f"{n}的需求")
        for c, n in zip(customers, names, strict=True)
    ]
    day = await today(desk)
    stages = await stage_ids(desk)
    # 甲：转入 30 小时还没跟进；乙：在已沟通停了 15 天（阶段的停滞天数 7 天）；丙：预计成交日
    # 过了 3 天。
    await desk.sql(
        "UPDATE opportunities SET opened_at = now() - interval '30 hours',"
        " stage_entered_at = now() - interval '30 hours',"
        " last_activity_at = now() - interval '30 hours' WHERE id = $1",
        uuid.UUID(made[0]["id"]),
    )
    await call(desk, "POST", f"{P}/{made[1]['id']}/followups", content="聊过一次")
    await desk.sql(
        "UPDATE opportunities SET stage_entered_at = now() - interval '15 days',"
        " last_activity_at = now() - interval '15 days' WHERE id = $1",
        uuid.UUID(made[1]["id"]),
    )
    await call(
        desk, "PATCH", f"{P}/{made[2]['id']}", expected_close_at=str(day - timedelta(days=3))
    )

    await wake(desk, "daily", trigger="manual")
    found = await findings(desk)

    def hits(code: str) -> list[dict[str, Any]]:
        return [v for k, v in found.items() if k.startswith(code)]

    [unattended] = hits("opportunity_unattended")
    assert unattended["title"] == "甲 的新线索 30 小时没人跟进"
    assert unattended["severity"] == "warning" and str(unattended["entity_id"]) == made[0]["id"]
    assert unattended["detail"].endswith("员工转入，还没有记过跟进，负责人 管理员。")
    assert unattended["link"] == f"/opportunities?id={made[0]['id']}"
    [stale] = hits("opportunity_stale")
    assert stale["title"] == "乙 的商机在「已沟通」停了 15 天" and stale["severity"] == "critical"
    assert "这个阶段的停滞天数是 7 天" in stale["detail"]
    assert json.loads(stale["data"]) == {"days": 15, "stale_days": 7, "stage": "已沟通"}
    [overdue] = hits("opportunity_closing_overdue")
    assert overdue["title"] == "丙 的商机预计成交日已过 3 天" and overdue["severity"] == "warning"
    assert hits("prospect_due") == []

    # 记了跟进、换了阶段、改了预计成交日后自动消除。
    await call(desk, "POST", f"{P}/{made[0]['id']}/followups", content="打过电话了")
    await call(desk, "POST", f"{P}/{made[1]['id']}/stage", stage_id=stages["quoted"])
    await call(
        desk, "PATCH", f"{P}/{made[2]['id']}", expected_close_at=str(day + timedelta(days=3))
    )
    await wake(desk, "daily", trigger="manual")
    found = await findings(desk)
    assert {
        f["status"]
        for code in ("opportunity_unattended", "opportunity_stale", "opportunity_closing_overdue")
        for f in hits(code)
    } == {"resolved"}


async def test_sales_report_counts_funnel_amounts_and_outcomes(desk: Desk) -> None:
    """销售报表：漏斗（到过各阶段）、进行中的金额和加权金额、赢单率和周期、输单原因、按负责人、按来源；
    顶部数字多了本周要跟进的。"""
    day = await today(desk)
    stages = await stage_ids(desk)
    ids = [await customer(desk, n) for n in ("A", "B", "C", "D")]
    a = await call(
        desk, "POST", P, 201, customer_id=ids[0], amount="1000", expected_close_at=str(day)
    )
    b = await call(desk, "POST", P, 201, customer_id=ids[1], amount="2000")
    c = await call(desk, "POST", P, 201, customer_id=ids[2], amount="3000")
    await call(desk, "POST", P, 201, customer_id=ids[3])
    await call(desk, "POST", f"{P}/{a['id']}/stage", stage_id=stages["quoted"])
    await call(desk, "POST", f"{P}/{b['id']}/stage", stage_id=stages["negotiating"])
    await call(desk, "POST", f"{P}/{b['id']}/won")
    await call(desk, "POST", f"{P}/{c['id']}/lost", reason_code="price")

    report = await call(desk, "GET", "/api/v1/reports/sales")
    assert report["amount_visible"] is True
    funnel = {f["code"]: (f["count"], f["rate"]) for f in report["funnel"]}
    assert funnel == {
        "new": (4, None),
        "contacted": (2, 0.5),
        "quoted": (2, 1.0),
        "negotiating": (1, 0.5),
        "won": (1, 1.0),
    }
    amount = report["amount"]
    assert (amount["open_count"], amount["weighted"], amount["this_month"]) == (
        2,
        "600.00",
        "1000.00",
    )
    assert {b_["key"]: b_["amount"] for b_ in amount["by_stage"]}["quoted"] == "1000.00"
    win = report["win"]
    assert (win["closed"], win["won"], win["lost"], win["win_rate"], win["avg_amount"]) == (
        2,
        1,
        1,
        0.5,
        "2000.00",
    )
    assert win["avg_days"] == 0.0
    assert [(r["key"], r["label"], r["count"]) for r in report["lost_reasons"]] == [
        ("price", "价格", 1)
    ]
    [owner] = report["by_owner"]
    assert (owner["name"], owner["created"], owner["active"], owner["won"], owner["lost"]) == (
        "管理员",
        4,
        2,
        1,
        1,
    )
    assert owner["won_amount"] == "2000.00"
    [source] = report["by_source"]
    assert (source["key"], source["label"], source["count"], source["won"], source["win_rate"]) == (
        "staff",
        "员工转入",
        4,
        1,
        0.25,
    )
    # 期间不含今天时什么都没有。
    response = await desk.client.get(
        "/api/v1/reports/sales",
        headers=desk.admin,
        params={"start": str(day - timedelta(days=30)), "end": str(day - timedelta(days=1))},
    )
    assert response.status_code == 200, response.text
    empty = response.json()
    assert empty["funnel"][0]["count"] == 0 and empty["win"]["closed"] == 0

    stats = await call(desk, "GET", f"{P}/stats")
    week_end = day + timedelta(days=6 - day.weekday())
    assert stats["week"] == (2 if day + timedelta(days=3) <= week_end else 0)
    assert (stats["active"], stats["won_this_month"]) == (2, 1)
