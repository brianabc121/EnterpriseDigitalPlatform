"""商机（设计文档 §40）：员工转入、跟进（第一次跟进自动推进）、修改、换阶段、赢单、输单和重新跟进；
订单确认后自动赢单；AI 按会话自动转入或建议、确认和忽略、客户又来咨询；AI 写跟进话术；客户列表的
"商机"标签；阶段设置、看板和顶部数字；"商机该跟进了"的检查项；合并客户；查看范围和权限。"""

import json
import uuid
from datetime import date, timedelta
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
from tests.test_orders import catalog, customer, new_order
from tests.test_wake import findings, wake

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
    [confirmed] = of_kind(won, "order")
    assert confirmed["title"] == f"订单 {order['no']} 已确认，自动赢单"
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
    assert await scan(desk) == {"won": 0, "returns": 0, "created": 1, "suggested": 0}
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

    # 客户又来咨询了：时间线上记一条（系统记的），意向更高时调高等级；同一会话只记一次。
    _, _, again = await closed_chat(desk, alice, "我要下单，地址是浦东新区", 4, visitor=visitor)
    assert (await scan(desk))["returns"] == 1
    assert (await scan(desk))["returns"] == 0
    detail = await call(desk, "GET", f"{P}/{item['id']}")
    [entry] = of_kind(detail, "session")
    assert (entry["method"], entry["staff_id"], entry["session_id"]) == ("chat", None, again)
    assert entry["content"].startswith("客户又来咨询了（准备下单）")
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
    assert finding["link"] == f"/customers?tab=prospects&view=overdue&owner={alice.staff_id}"
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
