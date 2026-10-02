"""意向客户（设计文档 §35）：员工转入、跟进、修改、成交、放弃和重新跟进；订单确认后自动成交；
AI 按会话自动转入或建议、确认和忽略、客户又来咨询；AI 写跟进话术；客户列表的"意向"标签；
"意向客户该跟进了"的检查项；合并客户；查看范围和权限。"""

import json
import uuid
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.prospects import ai, service
from tests.desk import Agent, Desk, Visitor
from tests.fake_llm import FakeLLM
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import catalog, customer, new_order
from tests.test_wake import findings, wake

P = "/api/v1/prospects"


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


async def tags(desk: Desk) -> dict[str, str | None]:
    """客户列表里每个客户的"意向"标签。"""
    page = await call(desk, "GET", "/api/v1/customers")
    return {c["id"]: c["prospect_status"] for c in page["items"]}


async def today(desk: Desk) -> date:
    async with desk.ctx.db.tenant_session(desk.tenant_id) as session:
        return await service.today(session)


async def scan(desk: Desk) -> dict[str, int]:
    return await ai.scan(desk.ctx, tenant_id=desk.tenant_id)


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
        "SELECT action FROM audit_logs WHERE tenant_id = $1 AND action LIKE 'prospect.%'"
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
    assert made["next_follow_at"] == str(day + timedelta(days=3))
    assert (made["follower_name"], made["created_by_name"]) == ("管理员", "管理员")
    assert made["followups"] == [] and made["can_assign"] is True
    # 同一客户只能有一条跟进中的；下次跟进日期不能早于今天。
    await call(desk, "POST", P, 409, customer_id=customer_id)
    yesterday = str(day - timedelta(days=1))
    await call(desk, "POST", P, 422, customer_id=other, next_follow_at=yesterday)

    # 客户列表、客户资料里的"意向"。
    listed = await tags(desk)
    assert (listed[customer_id], listed[other]) == ("active", None)
    detail = await call(desk, "GET", f"/api/v1/customers/{customer_id}")
    assert detail["prospect_status"] == "active"
    info = await call(desk, "GET", f"{P}/customer/{customer_id}")
    assert info["prospect"]["id"] == made["id"] and info["recent_deal_at"] is None
    assert (await call(desk, "GET", f"{P}/customer/{other}"))["prospect"] is None

    # 记一次跟进：不填下次跟进日期时按设置的默认天数。
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
    [entry] = followed["followups"]
    assert (entry["method"], entry["staff_name"], entry["content"]) == (
        "phone",
        "管理员",
        "电话沟通，客户想等国庆活动",
    )
    await call(desk, "POST", f"{P}/{made['id']}/followups", 422, content="  ")
    await call(
        desk, "POST", f"{P}/{made['id']}/followups", 422, content="x", next_follow_at=yesterday
    )
    updated = await call(
        desk, "PATCH", f"{P}/{made['id']}", level="medium", next_follow_at=str(day)
    )
    assert updated["level"] == "medium" and updated["due_today"] is True
    page = await listing(desk, view="today")
    assert [i["id"] for i in page["items"]] == [made["id"]]
    assert (page["counts"]["active"], page["counts"]["today"], page["counts"]["overdue"]) == (
        1,
        1,
        0,
    )
    assert (await listing(desk, q="王先生"))["total"] == 1
    assert (await listing(desk, q="没有这个人"))["total"] == 0

    # 这个客户的订单确认后自动成交，记下订单。
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
    assert (won["status"], won["order_id"], won["order_no"]) == ("won", order["id"], order["no"])
    page = await listing(desk, view="won")
    assert page["total"] == 1 and page["won_this_month"] == 1
    assert (await tags(desk))[customer_id] is None
    info = await call(desk, "GET", f"{P}/customer/{customer_id}")
    assert info["prospect"]["status"] == "won" and info["recent_deal_at"] is not None
    # 已成交的不能记跟进、不能放弃。
    await call(desk, "POST", f"{P}/{made['id']}/followups", 409, content="再联系")
    await call(desk, "POST", f"{P}/{made['id']}/lost", 409, reason="不要了")

    # 重新跟进（新的需求）：之前的订单不再算成交。
    reopened = await call(desk, "POST", f"{P}/{made['id']}/reopen")
    assert reopened["status"] == "active" and reopened["closed_at"] is None
    assert reopened["next_follow_at"] == str(day + timedelta(days=5))
    assert (await scan(desk))["won"] == 0
    assert (await call(desk, "GET", f"{P}/{made['id']}"))["status"] == "active"
    # 放弃要写原因；放弃的可以再重新跟进。
    await call(desk, "POST", f"{P}/{made['id']}/lost", 422, reason=" ")
    lost = await call(desk, "POST", f"{P}/{made['id']}/lost", reason="已经买了别家的")
    assert (lost["status"], lost["lost_reason"]) == ("lost", "已经买了别家的")
    again = await call(desk, "POST", f"{P}/{made['id']}/reopen", next_follow_at=str(day))
    assert (again["status"], again["lost_reason"], again["due_today"]) == ("active", None, True)
    # 手动标记成交（关联的订单要是这个客户的）。
    other_order = await new_order(desk, desk.admin, other, x1)
    await call(desk, "POST", f"{P}/{made['id']}/won", 422, order_id=other_order["id"])
    manual = await call(desk, "POST", f"{P}/{made['id']}/won", order_id=order["id"])
    assert manual["status"] == "won" and manual["order_id"] == order["id"]

    assert await audit_actions(desk) == [
        "prospect.create",
        "prospect.settings",
        "prospect.reopen",
        "prospect.lost",
        "prospect.reopen",
        "prospect.won",
    ]
    # 个人信息查询带上意向记录和跟进记录。
    data = await call(
        desk, "POST", f"/api/v1/customers/{customer_id}/personal-data", reason="客户来电查询"
    )
    [record] = data["prospects"]
    assert (record["status"], record["level"]) == ("已成交", "中")
    assert record["interest"] == "智能门锁 X1，10 把"
    assert [(f["method"], f["staff"]) for f in record["followups"]] == [("电话", "管理员")]


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
    assert item["follower_id"] == str(alice.staff_id)
    assert item["interest"] == "智能门锁多少钱？有点贵，我考虑一下，下周再说"
    assert item["concerns"] == "价格、还要考虑"
    # 客户说下周再说：7 天后跟进。
    assert item["next_follow_at"] == str(day + timedelta(days=7))
    # AI 读的是脱敏后的会话；用量记在"意向客户"场景。
    [chat] = [r for r in fake_llm.requests if "messages" in r]
    assert chat["messages"][0]["content"].startswith("任务：整理意向客户")
    [row] = await desk.sql(
        "SELECT status FROM llm_calls WHERE tenant_id = $1 AND scene = 'prospect'", desk.tenant_id
    )
    assert row["status"] == "ok"
    # 已经在名单里：不再转入。
    assert (await scan(desk))["created"] == 0

    # 客户又来咨询了：记一条跟进（系统记的），意向更高时调高等级；同一会话只记一次。
    _, _, again = await closed_chat(desk, alice, "我要下单，地址是浦东新区", 4, visitor=visitor)
    assert (await scan(desk))["returns"] == 1
    assert (await scan(desk))["returns"] == 0
    detail = await call(desk, "GET", f"{P}/{item['id']}")
    [entry] = detail["followups"]
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
    assert (await tags(desk))[bob_customer] == "suggested"
    accepted = await call(desk, "POST", f"{P}/{suggestion['id']}/accept")
    assert accepted["status"] == "active"
    await call(desk, "POST", f"{P}/{suggestion['id']}/accept", 409)
    await call(desk, "POST", f"{P}/{suggestion['id']}/lost", reason="只是问问")

    _, carol_customer, _ = await closed_chat(desk, alice, "规格有哪些", 2)
    # 刚放弃的客户 30 天内不再建议（bob），新的客户照常建议（carol）。
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
    assert made["follower_id"] == str(alice.staff_id) and made["can_assign"] is False
    # 看不到这个客户的员工看不到、也不能跟进他的意向记录。
    assert (await listing(desk, headers=bob.headers))["total"] == 0
    await call(desk, "GET", f"{P}/{made['id']}", 404, headers=bob.headers)
    await call(desk, "POST", f"{P}/{made['id']}/followups", 404, headers=bob.headers, content="x")
    await call(desk, "POST", P, 404, headers=bob.headers, customer_id=mine["id"])
    # 把跟进人改成别人需要分配客户的权限；设置也是。
    await call(
        desk,
        "PATCH",
        f"{P}/{made['id']}",
        403,
        headers=alice.headers,
        follower_id=str(bob.staff_id),
    )
    moved = await call(desk, "PATCH", f"{P}/{made['id']}", follower_id=str(bob.staff_id))
    assert moved["follower_name"] == "Bob"
    # 跟进人总能看到自己跟进的（客户不归他时也能看到、跟进），客户的归属坐席也还能看到。
    assert (await listing(desk, headers=bob.headers))["total"] == 1
    await call(desk, "POST", f"{P}/{made['id']}/followups", headers=bob.headers, content="电话回访")
    assert (await listing(desk, headers=alice.headers))["total"] == 1
    settings = await call(desk, "GET", f"{P}/settings", headers=alice.headers)
    assert settings == {
        "settings": {"ai_mode": "auto", "min_stage": 2, "follow_days": 3},
        "can_edit": False,
    }
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


async def test_due_check_groups_by_follower_and_merge_keeps_one_record(desk: Desk) -> None:
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
    for prospect, due in zip(
        made, (day - timedelta(days=5), day, day + timedelta(days=2)), strict=True
    ):
        await desk.sql(
            "UPDATE customer_prospects SET next_follow_at = $1 WHERE id = $2",
            due,
            uuid.UUID(prospect["id"]),
        )
    page = await listing(desk, headers=alice.headers)
    assert (page["counts"]["overdue"], page["counts"]["today"]) == (1, 1)

    await wake(desk, "daily", trigger="manual")
    found = {k: v for k, v in (await findings(desk)).items() if k.startswith("prospect_due")}
    [finding] = found.values()
    assert finding["title"] == "Alice 有 2 位意向客户该跟进了"
    assert finding["severity"] == "critical" and finding["assignee_ids"] == [alice.staff_id]
    assert finding["detail"].startswith("客户甲、客户乙。其中 1 位已经过了下次跟进日期")
    assert finding["link"] == f"/customers?tab=prospects&view=overdue&follower={alice.staff_id}"
    assert json.loads(finding["data"]) == {"due": 2, "overdue": 1}

    # 跟进以后自动消除。
    for prospect in made[:2]:
        await call(
            desk, "POST", f"{P}/{prospect['id']}/followups", headers=alice.headers, content="回访"
        )
    await wake(desk, "daily", trigger="manual")
    found = {k: v for k, v in (await findings(desk)).items() if k.startswith("prospect_due")}
    assert [f["status"] for f in found.values()] == ["resolved"]

    # 合并客户：同一客户只留一条跟进中的意向记录，跟进记录并过来；合同也并过来。
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
    assert len(detail["followups"]) == 2
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
    assert moved_counts["customer_prospects"] == 2 and moved_counts["contracts"] == 1
