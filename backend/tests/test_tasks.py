"""个人待办（设计文档 §27.2）：每个员工自己的事项，管理员看全员、交办；状态流转；到期和逾期提醒；
每日汇总；租户设置；与客户待办的数量。"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.tasks import notify
from tests.desk import Agent, Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls

TZ = ZoneInfo("Asia/Shanghai")


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
    desk: Desk, headers: dict[str, str], method: str, path: str, status: int = 200, **body: Any
) -> Any:
    response = await desk.client.request(method, path, headers=headers, json=body or None)
    assert response.status_code == status, (method, path, response.text)
    return response.json() if response.content else None


async def create(desk: Desk, headers: dict[str, str], title: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = await call(
        desk, headers, "POST", "/api/v1/tasks", 201, title=title, **extra
    )
    return result


async def notes(desk: Desk, staff: Agent) -> list[tuple[str, str]]:
    rows = await desk.sql(
        "SELECT kind, title FROM staff_notifications WHERE staff_id = $1 ORDER BY created_at, id",
        staff.staff_id,
    )
    return [(r["kind"], r["title"]) for r in rows]


async def test_everyone_has_their_own_list_and_admins_see_all(desk: Desk) -> None:
    wang = await desk.agent("wang", roles=["worker"], online=False)
    mei = await desk.agent("mei", roles=["agent"], online=False)
    cang = await desk.agent("cang", roles=["keeper"], online=False)

    # 工人、客服、仓管各自新建：编号按天递增，来源是"自己新建"。
    first = await create(desk, wang.headers, "打磨 20 个把手", priority="high")
    assert first["no"].startswith("T") and first["no"].endswith("-0001")
    assert (first["source"], first["status"], first["owner_id"]) == (
        "self",
        "open",
        str(wang.staff_id),
    )
    assert first["allowed"]["edit"] is True
    second = await create(desk, mei.headers, "回访张总")
    assert second["no"].endswith("-0002")
    await create(desk, cang.headers, "盘点铝材")

    # 彼此独立：只看到自己的，别人的看不到也改不了。
    mine = await call(desk, wang.headers, "GET", "/api/v1/tasks?view=mine")
    assert [t["title"] for t in mine["items"]] == ["打磨 20 个把手"]
    assert mine["total"] == 1
    await call(desk, wang.headers, "GET", f"/api/v1/tasks/{second['id']}", 404)
    await call(desk, wang.headers, "PATCH", f"/api/v1/tasks/{second['id']}", 404, title="改")
    await call(desk, mei.headers, "GET", "/api/v1/tasks?view=all", 403)
    await call(desk, mei.headers, "GET", "/api/v1/tasks/overview", 403)

    # 管理员看全员：列表、按人筛选、每人的汇总。
    everyone = await call(desk, desk.admin, "GET", "/api/v1/tasks?view=all")
    assert sorted(t["title"] for t in everyone["items"]) == [
        "回访张总",
        "打磨 20 个把手",
        "盘点铝材",
    ]
    only_wang = await call(
        desk, desk.admin, "GET", f"/api/v1/tasks?view=all&owner_id={wang.staff_id}"
    )
    assert [t["owner_name"] for t in only_wang["items"]] == ["Wang"]
    overview = await call(desk, desk.admin, "GET", "/api/v1/tasks/overview")
    by_name = {r["name"]: r for r in overview["items"]}
    assert by_name["Wang"]["open"] == 1 and by_name["Mei"]["open"] == 1
    assert by_name["管理员"]["open"] == 0
    # 管理员可以代为修改和完成。
    edited = await call(
        desk, desk.admin, "PATCH", f"/api/v1/tasks/{first['id']}", title="打磨 30 个把手"
    )
    assert edited["title"] == "打磨 30 个把手"

    # 交办：工人不能，管理员可以；接收人收到提醒，事项来源是"交办"，交办人在"我交办的"里看到。
    await call(
        desk, wang.headers, "POST", "/api/v1/tasks", 403, title="x", owner_id=str(mei.staff_id)
    )
    assigned = await create(
        desk,
        desk.admin,
        "整理本周报价单",
        owner_id=str(mei.staff_id),
        due_at="2026-10-09T18:00:00+08:00",
    )
    assert (assigned["source"], assigned["owner_name"], assigned["created_by_name"]) == (
        "assigned",
        "Mei",
        "管理员",
    )
    assert ("task_assigned", "管理员 交办：整理本周报价单") in await notes(desk, mei)
    mei_list = await call(desk, mei.headers, "GET", "/api/v1/tasks?view=mine&status=open")
    assert [t["title"] for t in mei_list["items"]] == ["整理本周报价单", "回访张总"]
    by_admin = await call(desk, desk.admin, "GET", "/api/v1/tasks?view=assigned")
    assert [t["title"] for t in by_admin["items"]] == ["整理本周报价单"]
    # 交办给停用的员工、不存在的员工被拒绝。
    await call(
        desk, desk.admin, "POST", "/api/v1/tasks", 422, title="x", owner_id=str(uuid.uuid4())
    )


async def test_status_transitions(desk: Desk) -> None:
    mei = await desk.agent("mei", roles=["agent"], online=False)
    task = await create(desk, mei.headers, "回访张总")
    done = await call(desk, mei.headers, "POST", f"/api/v1/tasks/{task['id']}/done", note="已回访")
    assert (done["status"], done["done_note"]) == ("done", "已回访")
    assert done["done_at"] is not None
    # 完成的不能修改、不能取消；重新打开后可以。
    await call(desk, mei.headers, "PATCH", f"/api/v1/tasks/{task['id']}", 409, title="改")
    await call(desk, mei.headers, "POST", f"/api/v1/tasks/{task['id']}/cancel", 409)
    reopened = await call(desk, mei.headers, "POST", f"/api/v1/tasks/{task['id']}/reopen")
    assert (reopened["status"], reopened["done_note"], reopened["done_at"]) == ("open", None, None)
    cancelled = await call(desk, mei.headers, "POST", f"/api/v1/tasks/{task['id']}/cancel")
    assert cancelled["status"] == "cancelled"
    await call(desk, mei.headers, "POST", f"/api/v1/tasks/{task['id']}/done", 409)
    listed = await call(desk, mei.headers, "GET", "/api/v1/tasks?view=mine&status=cancelled")
    assert [t["no"] for t in listed["items"]] == [task["no"]]
    # 搜索按编号或标题。
    found = await call(desk, mei.headers, "GET", f"/api/v1/tasks?view=mine&q={task['no']}")
    assert found["total"] == 1


async def test_due_reminders_overdue_and_daily_digest(desk: Desk) -> None:
    wang = await desk.agent("wang", roles=["worker"], online=False)
    now = datetime.now(UTC)
    soon = await create(
        desk, wang.headers, "下午三点交货", due_at=(now + timedelta(minutes=30)).isoformat()
    )
    assert soon["remind_before_minutes"] == 60
    late = await create(
        desk, wang.headers, "昨天就该做的", due_at=(now - timedelta(hours=2)).isoformat()
    )
    assert late["overdue"] is True
    far = await create(desk, wang.headers, "下周的事", due_at=(now + timedelta(days=5)).isoformat())
    assert far["overdue"] is False

    assert await notify.run_timers(desk.ctx, now=now) == 2
    kinds = await notes(desk, wang)
    assert ("task_due", "快到期：下午三点交货") in kinds
    assert ("task_overdue", "已逾期：昨天就该做的") in kinds
    # 每种提醒只发一次。
    assert await notify.run_timers(desk.ctx, now=now) == 0
    # 改了截止时间重新计算；到期后再提醒逾期。
    await call(
        desk,
        wang.headers,
        "PATCH",
        f"/api/v1/tasks/{soon['id']}",
        due_at=(now + timedelta(minutes=10)).isoformat(),
    )
    assert await notify.run_timers(desk.ctx, now=now) == 1
    assert await notify.run_timers(desk.ctx, now=now + timedelta(minutes=11)) == 1
    assert [k for k, _ in await notes(desk, wang)].count("task_overdue") == 2

    # 今日汇总：工作日上班后发一次（全天服务按 9 点），只发给有事项的人。
    counts = await call(desk, wang.headers, "GET", "/api/v1/tasks/counts")
    assert (counts["open"], counts["overdue"], counts["work_todos"]) == (3, 1, None)
    morning = datetime.now(TZ).replace(hour=10, minute=0, second=0, microsecond=0)
    assert await notify.run_digest(desk.ctx, now=morning) == 1
    assert await notify.run_digest(desk.ctx, now=morning) == 0
    digest = [t for k, t in await notes(desk, wang) if k == "task_digest"]
    assert len(digest) == 1 and digest[0].startswith("今日个人待办")
    admin_rows = await desk.sql(
        "SELECT kind FROM staff_notifications WHERE kind = 'task_digest' AND staff_id <> $1",
        wang.staff_id,
    )
    assert admin_rows == []
    # 关闭汇总后不再发。
    await call(
        desk,
        desk.admin,
        "PUT",
        "/api/v1/tenant/tasks-settings",
        remind_before_minutes=30,
        digest_enabled=False,
    )
    tomorrow = morning + timedelta(days=1)
    assert await notify.run_digest(desk.ctx, now=tomorrow) == 0
    # 新的事项按新的默认值提醒。
    created = await create(
        desk, wang.headers, "按新设置提醒", due_at=(now + timedelta(hours=3)).isoformat()
    )
    assert created["remind_before_minutes"] == 30
    await call(desk, wang.headers, "GET", "/api/v1/tenant/tasks-settings", 403)


async def test_counts_include_work_todos_for_agents(desk: Desk) -> None:
    mei = await desk.agent("mei", roles=["agent"], online=False)
    types = await call(desk, desk.admin, "GET", "/api/v1/todo-types")
    other = next(t for t in types["items"] if t["code"] == "other")
    todo = await call(
        desk, desk.admin, "POST", "/api/v1/todos", 201,
        type_id=other["id"], title="寄资料", assignee_id=str(mei.staff_id),
    )  # fmt: skip
    assert todo["assignee_id"] == str(mei.staff_id)
    await create(desk, mei.headers, "整理资料")
    counts = await call(desk, mei.headers, "GET", "/api/v1/tasks/counts")
    assert (counts["open"], counts["work_todos"]) == (1, 1)
    options = await call(desk, mei.headers, "GET", "/api/v1/tasks/staff")
    assert "Mei" in [s["name"] for s in options["items"]]
