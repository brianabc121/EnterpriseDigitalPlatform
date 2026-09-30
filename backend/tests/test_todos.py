"""待办（设计文档 §24）：AI 登记与待确认、员工新建与处理、分派、时限提醒与升级、去重合并、
进度查询、会话后解析与 AI 预填、敏感字段、数据范围、类型设置、访客端的服务进度。"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.todos import extract, notify
from tests.desk import Agent, Desk, Visitor
from tests.fake_llm import FakeLLM
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_ai_gateway_g5 import _settings, _tools_provider
from tests.test_ai_reception import bot_texts, enable_ai

TAX_NO = "91310000MA1FL8XQ30"


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def ai_desk(desk: Desk, app: FastAPI) -> Desk:
    """AI 接待，模型支持工具调用。"""
    await enable_ai(desk)
    await _tools_provider(desk, app)
    await _settings(desk, tools_enabled=True)
    return desk


async def types(desk: Desk) -> dict[str, dict[str, Any]]:
    response = await desk.client.get("/api/v1/todo-types", headers=desk.admin)
    assert response.status_code == 200, response.text
    return {t["code"]: t for t in response.json()["items"]}


async def update_type(desk: Desk, code: str, **changes: Any) -> dict[str, Any]:
    current = (await types(desk))[code]
    body = {
        k: v
        for k, v in current.items()
        if k not in ("id", "preset", "system", "created_at", "updated_at")
    }
    body.update(changes)
    response = await desk.client.put(
        f"/api/v1/admin/todo-types/{current['id']}", headers=desk.admin, json=body
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def group(desk: Desk, name: str, members: list[tuple[Agent, bool]]) -> str:
    response = await desk.client.post(
        "/api/v1/skill-groups",
        headers=desk.admin,
        json={
            "name": name,
            "members": [{"staff_id": str(a.staff_id), "is_lead": lead} for a, lead in members],
        },
    )
    assert response.status_code == 201, response.text
    group_id: str = response.json()["id"]
    return group_id


async def notes(desk: Desk, staff: Agent) -> list[tuple[str, str]]:
    rows = await desk.sql(
        "SELECT kind, title FROM staff_notifications WHERE staff_id = $1 ORDER BY created_at, id",
        staff.staff_id,
    )
    return [(r["kind"], r["title"]) for r in rows]


async def customer_of(desk: Desk, visitor: Visitor) -> str:
    [row] = await desk.sql("SELECT customer_id FROM rooms WHERE id = $1", visitor.room_id)
    return str(row["customer_id"])


async def todo_rows(desk: Desk) -> list[Any]:
    return await desk.sql("SELECT * FROM todos ORDER BY created_at, id")


async def events(desk: Desk, todo_id: Any) -> list[str]:
    rows = await desk.sql(
        "SELECT type FROM todo_events WHERE todo_id = $1 ORDER BY created_at, id", todo_id
    )
    return [r["type"] for r in rows]


async def act(desk: Desk, headers: dict[str, str], todo_id: Any, action: str, **body: Any) -> Any:
    response = await desk.client.post(
        f"/api/v1/todos/{todo_id}/{action}", headers=headers, json=body
    )
    assert response.status_code == 200, (action, response.text)
    return response.json()


# ---- AI 登记、待确认、提醒与升级（P6 验收） ----


async def test_ai_asks_for_required_fields_then_registers_a_pending_invoice(
    desk: Desk, app: FastAPI, fake_llm: FakeLLM
) -> None:
    await ai_desk(desk, app)
    fin = await desk.agent("fin", online=False)
    fin2 = await desk.agent("fin2", online=False)
    lead = await desk.agent("lead", roles=["supervisor"], online=False)
    finance = await group(desk, "财务", [(fin, False), (fin2, False), (lead, True)])
    await update_type(
        desk,
        "invoice",
        assign_rule={
            "steps": ["skill_group"],
            "skill_group_id": finance,
            "group_mode": "least_loaded",
        },
    )
    visitor = await desk.visitor()

    # 缺少必填的税号：不登记，AI 追问（这一轮不计入 AI 接待轮次）。
    fake_llm.tool_plan = [
        (
            "create_todo",
            {
                "type": "invoice",
                "title": "开具增值税专用发票",
                "detail": "客户要开一张专票",
                "fields": {"invoice_type": "增值税专用发票", "invoice_title": "星河科技"},
            },
        )
    ]
    await desk.say(visitor, "帮我开一张专票，抬头星河科技")
    assert await todo_rows(desk) == []
    assert "税号" in bot_texts(desk, visitor)[-1]
    [state] = await desk.sql("SELECT turns FROM ai_session_states")
    assert state["turns"] == 0

    fake_llm.tool_plan = [
        (
            "create_todo",
            {
                "type": "invoice",
                "title": "开具增值税专用发票",
                "detail": "客户要开一张专票",
                "fields": {
                    "invoice_type": "增值税专用发票",
                    "invoice_title": "星河科技",
                    "tax_no": TAX_NO,
                },
            },
        )
    ]
    await desk.say(visitor, f"税号是 {TAX_NO}")
    assert bot_texts(desk, visitor)[-1] == "已为您记录开票申请，客服确认后会尽快为您处理。"
    [todo] = await todo_rows(desk)
    assert (todo["status"], todo["source"], todo["assignee_id"]) == (
        "pending",
        "ai_chat",
        fin.staff_id,
    )
    assert todo["no"].startswith("TD") and len(todo["evidence_message_ids"]) == 1
    assert json.loads(todo["fields"])["tax_no"] == TAX_NO
    # 确认人收到待确认提醒；待确认的不出现在待办列表里。
    assert await notes(desk, fin) == [("todo_pending", "待确认：开票「开具增值税专用发票」")]
    pending = await desk.client.get("/api/v1/todos?view=pending", headers=fin.headers)
    assert pending.json()["total"] == 1
    assert (await desk.client.get("/api/v1/todos", headers=fin.headers)).json()["total"] == 0
    counts = (await desk.client.get("/api/v1/todos/counts", headers=fin.headers)).json()
    assert (counts["pending"], counts["mine"]) == (1, 0)
    # 不是交给自己确认的看不到，也不能确认。
    denied = await desk.client.post(
        f"/api/v1/todos/{todo['id']}/confirm", headers=fin2.headers, json={}
    )
    assert denied.status_code == 404

    # 确认人确认，并交给 fin2 处理：处理人立即收到提醒，截止时间从确认时开始计算。
    before = datetime.now(UTC)
    confirmed = await act(desk, fin.headers, todo["id"], "confirm", assignee_id=str(fin2.staff_id))
    assert (confirmed["status"], confirmed["assignee_id"]) == ("open", str(fin2.staff_id))
    due = datetime.fromisoformat(confirmed["due_at"])
    assert timedelta(hours=23) < due - before < timedelta(hours=25)
    assert await notes(desk, fin2) == [("todo_assigned", "新待办：开票「开具增值税专用发票」")]
    [event] = await desk.sql(
        "SELECT payload FROM todo_events WHERE todo_id = $1 AND type = 'confirmed'", todo["id"]
    )
    assert json.loads(event["payload"])["modified"] is True

    # 截止前提醒一次，逾期时提醒一次，逾期超过 4 个工作小时升级给组长。
    report = await notify.run_timers(desk.ctx, now=due - timedelta(minutes=60))
    assert report["due"] == 1
    report = await notify.run_timers(desk.ctx, now=due + timedelta(minutes=1))
    assert (report["due"], report["overdue"], report["escalated"]) == (0, 1, 0)
    report = await notify.run_timers(desk.ctx, now=due + timedelta(hours=4, minutes=1))
    assert report["escalated"] == 1
    assert [k for k, _ in await notes(desk, fin2)] == ["todo_assigned", "todo_due", "todo_overdue"]
    assert await notes(desk, lead) == [
        ("todo_escalated", "待办逾期升级：开票「开具增值税专用发票」")
    ]
    # 每种提醒只发一次。
    report = await notify.run_timers(desk.ctx, now=due + timedelta(hours=5))
    assert (report["due"], report["overdue"], report["escalated"]) == (0, 0, 0)
    assert await events(desk, todo["id"]) == [
        "created",
        "confirmed",
        "reminded",
        "reminded",
        "escalated",
    ]


async def test_duplicates_merge_retries_dedupe_and_hourly_limit(
    desk: Desk, app: FastAPI, fake_llm: FakeLLM
) -> None:
    await ai_desk(desk, app)
    owner = await desk.agent("owner", online=False)
    visitor = await desk.visitor()
    await desk.sql(
        "UPDATE customers SET owner_id = $1 WHERE id = $2",
        owner.staff_id,
        uuid.UUID(await customer_of(desk, visitor)),
    )
    call = ("create_todo", {"type": "callback", "title": "回电", "detail": "客户希望尽快回电话"})

    fake_llm.tool_plan = [call, call]  # 同一次调用重试：不重复创建
    await desk.say(visitor, "麻烦给我回个电话")
    [todo] = await todo_rows(desk)
    assert (todo["assignee_id"], todo["nudge_count"], todo["priority"]) == (
        owner.staff_id,
        0,
        "normal",
    )

    # 客户再次提出同一件事：合并为一次催促；催促达到 2 次，优先级提高一级。
    for text, count in (("怎么还没人给我回电话", 1), ("快点回电话", 2)):
        fake_llm.tool_plan = [
            (
                "create_todo",
                {"type": "callback", "title": "回电", "detail": f"客户希望尽快回电话，{text}"},
            )
        ]
        await desk.say(visitor, text)
        [todo] = await todo_rows(desk)
        assert todo["nudge_count"] == count
    assert todo["priority"] == "high"
    assert "已经登记过这件事" in json.dumps(fake_llm.requests[-1], ensure_ascii=False)

    # 每个会话每小时最多登记 3 条（这里已有 1 条）。
    for code, title in (("quote", "报价"), ("send_materials", "寄资料"), ("other", "查维修记录")):
        fields = {"product": "X1"} if code == "quote" else {}
        if code == "send_materials":
            fields = {"material": "产品手册"}
        fake_llm.tool_plan = [
            ("create_todo", {"type": code, "title": title, "detail": title, "fields": fields})
        ]
        await desk.say(visitor, f"还有一件事：{title}")
    assert len(await todo_rows(desk)) == 3
    tool_outputs = [
        m["content"]
        for r in fake_llm.requests
        for m in r.get("messages", [])
        if m.get("role") == "tool"
    ]
    assert "达到上限" in tool_outputs[-1]


async def test_lookup_reports_progress_and_counts_as_a_nudge(
    desk: Desk, app: FastAPI, fake_llm: FakeLLM
) -> None:
    await ai_desk(desk, app)
    visitor = await desk.visitor()
    fake_llm.tool_plan = [
        ("create_todo", {"type": "callback", "title": "回电确认安装时间", "detail": "客户要回电"})
    ]
    await desk.say(visitor, "请回电")
    fake_llm.tool_plan = [("lookup_todos", {})]
    await desk.say(visitor, "我的回电安排了吗")
    assert bot_texts(desk, visitor)[-1] == "回电 / 回访「回电确认安装时间」：已登记，等待客服确认"
    [todo] = await todo_rows(desk)
    assert todo["nudge_count"] == 1

    # 另一位匿名访客查不到别人的待办。
    other = await desk.visitor()
    fake_llm.tool_plan = [("lookup_todos", {})]
    await desk.say(other, "我的回电安排了吗")
    tool_outputs = [
        m["content"]
        for r in fake_llm.requests
        for m in r.get("messages", [])
        if m.get("role") == "tool"
    ]
    assert tool_outputs[-1] == "没有查到客户登记过的事项。"


async def test_complaint_registers_and_hands_off_with_supervisor_notified(
    desk: Desk, app: FastAPI, fake_llm: FakeLLM
) -> None:
    await ai_desk(desk, app)
    agent = await desk.agent("alice")
    visitor = await desk.visitor()
    fake_llm.tool_plan = [
        (
            "create_todo",
            {"type": "complaint", "title": "投诉配送延误", "detail": "客户投诉配送延误"},
        )
    ]
    await desk.say(visitor, "配送延误了三天，你们要给个说法")
    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", agent.staff_id)
    [todo] = await todo_rows(desk)
    # 投诉处理默认优先级为高，没有配置技能组时进入公共待认领池，提醒能分派待办的人（管理员）。
    assert (todo["status"], todo["priority"], todo["assignee_id"]) == ("pending", "high", None)
    [admin] = await desk.sql("SELECT id FROM staff WHERE username = 'admin'")
    kinds = await desk.sql("SELECT kind FROM staff_notifications WHERE staff_id = $1", admin["id"])
    assert [k["kind"] for k in kinds] == ["todo_pending"]


# ---- 员工新建与处理 ----


async def test_staff_created_todo_goes_straight_to_the_list_and_is_handled(
    desk: Desk,
) -> None:
    alice = await desk.agent("alice")
    bob = await desk.agent("bob", online=False)
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    customer_id = await customer_of(desk, visitor)
    kinds = await types(desk)

    created = await desk.client.post(
        "/api/v1/todos",
        headers=alice.headers,
        json={
            "type_id": kinds["callback"]["id"],
            "title": "回电确认安装时间",
            "detail": "客户希望周六上午安装",
            "session_id": str(chat["id"]),
            "fields": {"phone": "13800001111"},
        },
    )
    assert created.status_code == 201, created.text
    todo = created.json()
    # 员工新建的直接进入待办列表；规则第一步是会话坐席，就是 Alice 自己，不需要提醒。
    assert (todo["status"], todo["assignee_id"], todo["customer_id"]) == (
        "open",
        str(alice.staff_id),
        customer_id,
    )
    assert todo["fields"] == [
        {"key": "phone", "label": "回电号码", "value": "138****1111", "sensitive": True}
    ]
    assert await notes(desk, alice) == []
    mine = await desk.client.get("/api/v1/todos?view=mine", headers=alice.headers)
    assert mine.json()["total"] == 1

    await act(desk, alice.headers, todo["id"], "start")
    await act(desk, alice.headers, todo["id"], "wait", note="等客户确认地址")
    resumed = await act(desk, alice.headers, todo["id"], "resume")
    assert resumed["status"] == "in_progress" and resumed["first_response_at"]
    later = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    moved = await act(
        desk, alice.headers, todo["id"], "reschedule", due_at=later, reason="客户改期"
    )
    assert datetime.fromisoformat(moved["due_at"]) == datetime.fromisoformat(later)
    await act(
        desk,
        alice.headers,
        todo["id"],
        "comments",
        text="@bob 帮忙确认库存",
        mentions=[str(bob.staff_id)],
    )
    assert await notes(desk, bob) == [
        ("todo_mention", "Alice 在待办「回电确认安装时间」里提到了你")
    ]
    # Bob 不是处理人，不能完成。
    denied = await desk.client.post(
        f"/api/v1/todos/{todo['id']}/done", headers=bob.headers, json={"result": "x"}
    )
    assert denied.status_code in (403, 404)

    # 完成并通知客户：网页访客收到系统消息（类型的完成通知模板）。
    done = await desk.client.post(
        f"/api/v1/todos/{todo['id']}/done",
        headers=alice.headers,
        json={"result": "已约好周六上午 10 点", "notify_customer": True},
    )
    assert done.status_code == 200, done.text
    assert (done.json()["status"], done.headers["X-Customer-Notice"]) == ("done", "sent")
    await desk.flush()
    assert desk.notices(visitor)[-1] == "您好，关于您的回电需求：已约好周六上午 10 点"

    # 7 天内客户再次提出：重新打开原待办，重新计时。
    reopened = await act(desk, alice.headers, todo["id"], "reopen", reason="客户说没接到电话")
    assert (reopened["status"], reopened["closed_at"]) == ("open", None)
    cancelled = await act(desk, alice.headers, todo["id"], "cancel", reason="客户不需要了")
    assert cancelled["status"] == "cancelled"
    assert await events(desk, todo["id"]) == [
        "created",
        "started",
        "waiting",
        "resumed",
        "rescheduled",
        "commented",
        "done",
        "customer_notified",
        "reopened",
        "cancelled",
    ]
    detail = (await desk.client.get(f"/api/v1/todos/{todo['id']}", headers=alice.headers)).json()
    assert [e["type"] for e in detail["events"]][-1] == "cancelled"
    assert detail["events"][0]["actor_name"] == "Alice"


async def test_assignment_rules_pool_claim_and_transfer(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)
    carol = await desk.agent("carol", online=False)
    after_sales = await group(desk, "售后", [(alice, False), (bob, True)])
    await update_type(
        desk, "after_sales", assign_rule={"steps": ["skill_group"], "skill_group_id": after_sales}
    )
    kinds = await types(desk)
    customer = await desk.client.post(
        "/api/v1/customers", headers=desk.admin, json={"display_name": "星河科技"}
    )
    body = {
        "type_id": kinds["after_sales"]["id"],
        "title": "换货",
        "customer_id": customer.json()["id"],
        "fields": {"request": "换货"},
    }
    created = await desk.client.post("/api/v1/todos", headers=desk.admin, json=body)
    assert created.status_code == 201, created.text
    todo = created.json()
    # 放进售后组的待认领池：组员收到提醒，能看到、能认领；组外的 Carol 看不到。
    assert (todo["assignee_id"], todo["skill_group_id"]) == (None, after_sales)
    assert [k for k, _ in await notes(desk, alice)] == ["todo_assigned"]
    pool = await desk.client.get("/api/v1/todos?view=pool", headers=alice.headers)
    assert pool.json()["total"] == 1
    hidden = await desk.client.get(f"/api/v1/todos/{todo['id']}", headers=carol.headers)
    assert hidden.status_code == 404
    claimed = await act(desk, alice.headers, todo["id"], "claim")
    assert claimed["assignee_id"] == str(alice.staff_id)
    # 认领之后组内其他人不再看到。
    again = await desk.client.post(f"/api/v1/todos/{todo['id']}/claim", headers=bob.headers)
    assert again.status_code == 404

    # 处理人把自己的待办转交给 Carol（附说明）；坐席不能改派别人的待办。
    moved = await act(
        desk, alice.headers, todo["id"], "assign", assignee_id=str(carol.staff_id), note="交接"
    )
    assert (moved["assignee_id"], moved["assigned_by"]) == (
        str(carol.staff_id),
        str(alice.staff_id),
    )
    assert [k for k, _ in await notes(desk, carol)] == ["todo_assigned"]
    denied = await desk.client.post(
        f"/api/v1/todos/{todo['id']}/assign",
        headers=alice.headers,
        json={"assignee_id": str(bob.staff_id)},
    )
    assert denied.status_code in (403, 404)
    assigned = await desk.client.get("/api/v1/todos?view=assigned", headers=alice.headers)
    assert assigned.json()["total"] == 1
    [audit] = await desk.sql("SELECT detail FROM audit_logs WHERE action = 'todo.assign'")
    assert json.loads(audit["detail"])["note"] == "交接"

    # 停用处理人：未完成的待办按规则重新分派（排除这名员工）。
    disabled = await desk.client.patch(
        f"/api/v1/staff/{carol.staff_id}", headers=desk.admin, json={"status": "disabled"}
    )
    assert disabled.status_code == 200, disabled.text
    [row] = await desk.sql("SELECT assignee_id, skill_group_id FROM todos")
    assert (row["assignee_id"], str(row["skill_group_id"])) == (None, after_sales)


async def test_sensitive_fields_are_encrypted_masked_and_revealed_with_audit(
    desk: Desk,
) -> None:
    alice = await desk.agent("alice", online=False)
    kinds = await types(desk)
    customer = await desk.client.post(
        "/api/v1/customers", headers=desk.admin, json={"display_name": "王先生"}
    )
    created = await desk.client.post(
        "/api/v1/todos",
        headers=desk.admin,
        json={
            "type_id": kinds["visit"]["id"],
            "title": "上门安装",
            "customer_id": customer.json()["id"],
            "assignee_id": str(alice.staff_id),
            "fields": {"address": "上海市浦东新区世纪大道 100 号", "phone": "13900002222"},
        },
    )
    assert created.status_code == 201, created.text
    todo = created.json()
    values = {f["key"]: f["value"] for f in todo["fields"]}
    assert values == {"address": "上海市浦东新****", "phone": "139****2222"}
    [row] = await desk.sql("SELECT fields::text AS fields FROM todos")
    assert "13900002222" not in row["fields"] and "世纪大道" not in row["fields"]

    # 缺少必填字段、格式不对时不能保存。
    bad = await desk.client.post(
        "/api/v1/todos",
        headers=desk.admin,
        json={
            "type_id": kinds["visit"]["id"],
            "title": "上门安装",
            "customer_id": customer.json()["id"],
            "fields": {"phone": "请打座机"},
        },
    )
    assert bad.status_code == 422
    assert "缺少上门地址" in bad.text and "联系电话的格式不正确" in bad.text

    # 查看完整内容需要查看敏感信息的权限，并记审计日志。
    denied = await desk.client.post(f"/api/v1/todos/{todo['id']}/reveal", headers=alice.headers)
    assert denied.status_code == 403
    revealed = await desk.client.post(f"/api/v1/todos/{todo['id']}/reveal", headers=desk.admin)
    assert {f["key"]: f["value"] for f in revealed.json()["fields"]} == {
        "address": "上海市浦东新区世纪大道 100 号",
        "phone": "13900002222",
    }
    [audit] = await desk.sql("SELECT detail FROM audit_logs WHERE action = 'todo.view_sensitive'")
    assert json.loads(audit["detail"])["fields"] == ["address", "phone"]

    # 客户的个人信息查询包含待办（明文）；删除请求删除待办。
    data = await desk.client.post(
        f"/api/v1/customers/{customer.json()['id']}/personal-data",
        headers=desk.admin,
        json={"reason": "客户申请"},
    )
    assert data.status_code == 200, data.text
    [exported] = data.json()["todos"]
    assert exported["fields"]["phone"] == "13900002222"
    erased = await desk.client.post(
        f"/api/v1/customers/{customer.json()['id']}/erase",
        headers=desk.admin,
        json={"confirm_name": "王先生", "reason": "客户申请"},
    )
    assert erased.status_code == 200, erased.text
    assert erased.json()["todos"] == 1
    assert await todo_rows(desk) == []


async def test_reject_merge_and_batch_confirm(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    customer = await desk.client.post(
        "/api/v1/customers", headers=desk.admin, json={"display_name": "星河科技"}
    )
    customer_id = customer.json()["id"]
    await desk.sql(
        "UPDATE customers SET owner_id = $1 WHERE id = $2", alice.staff_id, uuid.UUID(customer_id)
    )
    [kind] = await desk.sql("SELECT id FROM todo_types WHERE code = 'other'")
    ids = []
    for n in range(4):
        [row] = await desk.sql(
            "INSERT INTO todos (id, tenant_id, no, type_id, title, detail, customer_id, source,"
            " status, assignee_id, created_by_type)"
            " VALUES ($1, $2, $3, $4, $5, '', $6, 'ai_summary', 'pending', $7, 'ai') RETURNING id",
            uuid.uuid4(),
            desk.tenant_id,
            f"TD20260930-{n + 1:04d}",
            kind["id"],
            f"事项{n}",
            uuid.UUID(customer_id),
            alice.staff_id,
        )
        ids.append(str(row["id"]))
    rejected = await act(desk, alice.headers, ids[0], "discard", reason="not_real", note="只是问问")
    assert (rejected["status"], rejected["reject_reason"]) == ("rejected", "not_real")
    batch = await desk.client.post(
        "/api/v1/todos/batch",
        headers=alice.headers,
        json={"ids": [ids[1], ids[2], ids[0]], "action": "confirm"},
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["done"] == [ids[1], ids[2]]
    assert [f["id"] for f in batch.json()["failed"]] == [ids[0]]
    merged = await act(desk, alice.headers, ids[3], "merge", target_id=ids[1])
    assert (merged["status"], merged["reject_reason"], merged["close_note"]) == (
        "rejected",
        "duplicate",
        "合并到 TD20260930-0002",
    )
    [target] = await desk.sql("SELECT nudge_count FROM todos WHERE id = $1", uuid.UUID(ids[1]))
    assert target["nudge_count"] == 1


# ---- 会话后解析与 AI 预填 ----


async def test_post_session_extraction_and_prefill(
    desk: Desk, app: FastAPI, fake_llm: FakeLLM
) -> None:
    await _tools_provider(desk, app)
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, f"我要开发票，抬头是星河科技，税号 {TAX_NO}")
    await desk.say(visitor, "【低置信】顺便问下能不能批量采购，报价多少")
    chat = await desk.session_of(visitor)
    sent = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages",
        headers=alice.headers,
        json={"client_msg_id": uuid.uuid4().hex, "text": "好的，我明天给您回电话确认"},
    )
    assert sent.status_code == 200, sent.text
    closed = await desk.client.post(f"/api/v1/sessions/{chat['id']}/close", headers=alice.headers)
    assert closed.status_code == 200, closed.text
    await desk.flush()

    assert await extract.run_pending(desk.ctx) == 2
    rows = await desk.sql(
        "SELECT t.code, d.status, d.source, d.assignee_id, d.confidence, d.fields::text AS fields"
        " FROM todos d JOIN todo_types t ON t.id = d.type_id ORDER BY t.code"
    )
    assert [(r["code"], r["status"], r["source"]) for r in rows] == [
        ("callback", "pending", "ai_summary"),
        ("invoice", "pending", "ai_summary"),
    ]
    # 坐席答应客户的事由他本人确认；置信度低的（报价）不进入待确认页。
    assert rows[0]["assignee_id"] == alice.staff_id
    assert TAX_NO in rows[1]["fields"]
    [record] = await desk.sql("SELECT status, created, skipped FROM todo_extractions")
    assert tuple(record) == ("done", 2, 1)
    assert await extract.run_pending(desk.ctx) == 0

    # AI 预填：粘贴客户的话，返回待办表单（不保存）。
    prefill = await desk.client.post(
        "/api/v1/todos/extract",
        headers=alice.headers,
        json={"text": f"帮我开票，抬头是明月科技，税号 {TAX_NO}"},
    )
    assert prefill.status_code == 200, prefill.text
    [suggestion] = prefill.json()["items"]
    assert (suggestion["type_code"], suggestion["fields"]["invoice_title"]) == (
        "invoice",
        "明月科技",
    )
    assert suggestion["missing"] == []
    assert len(await todo_rows(desk)) == 2


# ---- 设置与访客端 ----


async def test_types_admin_settings_and_visitor_progress(desk: Desk) -> None:
    kinds = await types(desk)
    assert {
        "callback",
        "leave_message",
        "invoice",
        "complaint",
        "order_review",
        "collection",
    } <= set(kinds)
    assert kinds["order_review"]["system"] and not kinds["order_review"]["ai_enabled"]
    created = await desk.client.post(
        "/api/v1/admin/todo-types",
        headers=desk.admin,
        json={
            "code": "contract",
            "name": "合同寄送",
            "fields": [
                {"key": "company", "label": "公司", "required": True},
                {"key": "copies", "label": "份数", "type": "number"},
            ],
        },
    )
    assert created.status_code == 201, created.text
    duplicate = await desk.client.post(
        "/api/v1/admin/todo-types", headers=desk.admin, json={"code": "contract", "name": "x"}
    )
    assert duplicate.status_code == 409
    bad = await desk.client.post(
        "/api/v1/admin/todo-types",
        headers=desk.admin,
        json={"code": "bad", "name": "x", "fields": [{"key": "a", "label": "A", "type": "option"}]},
    )
    assert bad.status_code == 422
    # 系统类型不能开启 AI 登记；预置类型不能删除；用过的自定义类型只能停用。
    review = await update_type(desk, "order_review", ai_enabled=True)
    assert review["ai_enabled"] is False
    preset = await desk.client.delete(
        f"/api/v1/admin/todo-types/{kinds['callback']['id']}", headers=desk.admin
    )
    assert preset.status_code == 409
    deleted = await desk.client.delete(
        f"/api/v1/admin/todo-types/{created.json()['id']}", headers=desk.admin
    )
    assert deleted.status_code == 204

    settings = (await desk.client.get("/api/v1/admin/todo-settings", headers=desk.admin)).json()
    assert (settings["ai_hourly_limit"], settings["visitor_progress"]) == (3, False)

    # 访客端的服务进度默认关闭；开启后只看到自己的待办。
    visitor = await desk.visitor()
    headers = {"X-Visitor-Token": visitor.visitor_token}
    await desk.client.post(
        "/api/v1/visitor/tickets",
        headers=headers,
        json={"content": "请回电", "contact": "13800000000"},
    )
    progress = await desk.client.get("/api/v1/visitor/progress", headers=headers)
    assert progress.json() == {"enabled": False, "items": []}
    await desk.client.put(
        "/api/v1/admin/todo-settings",
        headers=desk.admin,
        json={**settings, "visitor_progress": True},
    )
    [item] = (await desk.client.get("/api/v1/visitor/progress", headers=headers)).json()["items"]
    assert (item["type_name"], item["status_label"]) == ("留言", "已受理，等待处理")
    other = await desk.visitor()
    others = await desk.client.get(
        "/api/v1/visitor/progress", headers={"X-Visitor-Token": other.visitor_token}
    )
    assert others.json()["items"] == []

    # 没有待办权限的角色看不到待办。
    km = await desk.agent("kmgr", roles=["knowledge_manager"], online=False)
    assert (await desk.client.get("/api/v1/todos", headers=km.headers)).status_code == 403


async def test_pending_reminder_and_daily_digest(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    customer = await desk.client.post(
        "/api/v1/customers", headers=desk.admin, json={"display_name": "星河科技"}
    )
    await desk.sql(
        "UPDATE customers SET owner_id = $1 WHERE id = $2",
        alice.staff_id,
        uuid.UUID(customer.json()["id"]),
    )
    [kind] = await desk.sql("SELECT id FROM todo_types WHERE code = 'other'")
    now = datetime.now(UTC)
    await desk.sql(
        "INSERT INTO todos (id, tenant_id, no, type_id, title, customer_id, source, status,"
        " assignee_id, created_by_type, notified_at, pending_remind_at)"
        " VALUES ($1, $2, 'TD20260930-0001', $3, '待确认的事', $4, 'ai_summary', 'pending', $5,"
        " 'ai', $6, $7)",
        uuid.uuid4(),
        desk.tenant_id,
        kind["id"],
        uuid.UUID(customer.json()["id"]),
        alice.staff_id,
        now,
        now + timedelta(hours=2),
    )
    assert (await notify.run_timers(desk.ctx, now=now + timedelta(hours=1)))["pending"] == 0
    assert (await notify.run_timers(desk.ctx, now=now + timedelta(hours=2, minutes=1)))[
        "pending"
    ] == 1
    assert [k for k, _ in await notes(desk, alice)] == ["todo_pending"]

    # 每个工作日上班后发一次今日待办汇总（没有配置工作时间时按 9 点）。
    morning = datetime(2026, 10, 8, 9, 30, tzinfo=UTC) + timedelta(hours=-8)
    assert await notify.run_digest(desk.ctx, now=morning - timedelta(hours=1)) == 0
    assert await notify.run_digest(desk.ctx, now=morning) == 1
    assert await notify.run_digest(desk.ctx, now=morning + timedelta(hours=1)) == 0
    assert (await notes(desk, alice))[-1] == ("todo_digest", "今日待办（10-08）")
    [body] = await desk.sql(
        "SELECT body FROM staff_notifications WHERE kind = 'todo_digest' AND staff_id = $1",
        alice.staff_id,
    )
    assert body["body"] == "待确认 1 条，今日到期 0 条，已逾期 0 条，待认领 0 条"
