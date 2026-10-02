"""AI 唤醒（设计文档 §33）：数据巡检的检查项、问题的生命周期、通知和简报、增量更新索引跳过没有变化
的检查项、设置、唤醒记录和接口权限。"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app import cli
from app.core.config import Settings
from app.modules.wake import checks, queue, runner
from app.modules.wake import service as wake_service
from app.modules.wake.models import RunKind, RunTrigger
from app.modules.wake.settings import WakeSettings
from tests.desk import Desk
from tests.fake_llm import FakeLLM
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import catalog, customer, new_order

WAKE = "/api/v1/wake"
HOURLY = sorted(c.code for c in checks.CHECKS if c.hourly)
WORKDAYS = {"tz": "Asia/Shanghai", "days": {str(d): [["09:00", "18:00"]] for d in range(1, 6)}}


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def wake(
    desk: Desk,
    kind: str = RunKind.HOURLY,
    *,
    now: datetime | None = None,
    trigger: str = RunTrigger.SCHEDULE,
) -> dict[str, Any]:
    """登记一次唤醒并执行（时间可以指定），返回统计。"""
    now = now or datetime.now(UTC)
    async with desk.ctx.db.tenant_session(desk.tenant_id) as session:
        run = await queue.enqueue(session, desk.tenant_id, kind, trigger, now=now)
        await session.commit()
        run_id = run.id
    outcome = await runner.inspect(
        desk.ctx, run_id, desk.tenant_id, kind, now, force=trigger == RunTrigger.MANUAL
    )
    return outcome.stats


async def findings(desk: Desk) -> dict[str, Any]:
    rows = await desk.sql(
        "SELECT * FROM wake_findings WHERE tenant_id = $1 ORDER BY created_at", desk.tenant_id
    )
    return {r["fingerprint"]: r for r in rows}


async def notes(desk: Desk, kind: str) -> list[tuple[str, str | None]]:
    rows = await desk.sql(
        "SELECT title, body FROM staff_notifications WHERE tenant_id = $1 AND kind = $2"
        " ORDER BY created_at, id",
        desk.tenant_id,
        kind,
    )
    return [(r["title"], r["body"]) for r in rows]


async def admin_id(desk: Desk) -> uuid.UUID:
    [row] = await desk.sql(
        "SELECT id FROM staff WHERE tenant_id = $1 AND username = 'admin'", desk.tenant_id
    )
    return uuid.UUID(str(row["id"]))


async def pending_order(
    desk: Desk, hours: float, products: dict[str, str] | None = None
) -> dict[str, Any]:
    """一张提交审核 hours 小时的订单（直接改库，审核流程见 test_orders.py）。"""
    products = products or await catalog(desk)
    order = await new_order(
        desk, desk.admin, await customer(desk), [{"product_id": products["LOCK-X1"], "quantity": 1}]
    )
    await desk.sql(
        "UPDATE orders SET status = 'pending_review', submitted_at = now() - $2::interval"
        " WHERE id = $1",
        uuid.UUID(order["id"]),
        timedelta(hours=hours),
    )
    return order


async def put_settings(desk: Desk, **changes: Any) -> httpx.Response:
    current = (await desk.client.get(f"{WAKE}/settings", headers=desk.admin)).json()
    body = {**current["settings"], **changes}
    return await desk.client.put(f"{WAKE}/settings", headers=desk.admin, json=body)


async def test_all_checks_run_on_an_empty_tenant(desk: Desk) -> None:
    stats = await wake(desk, RunKind.DAILY, trigger=RunTrigger.MANUAL)
    assert (stats["checks"], stats["ran"], stats["errors"]) == (len(checks.CHECKS), 19, [])
    assert (stats["found"], stats["new"]) == (0, 0)
    # 简报：没有问题时也发一句，让管理员知道 AI 醒来过。
    [(title, body)] = await notes(desk, runner.KIND_BRIEF)
    assert title == "AI 巡检日报：今天没有发现需要处理的问题"
    assert body == "今天没有发现需要处理的问题。"


async def test_hourly_check_notifies_once_and_skips_when_nothing_changed(desk: Desk) -> None:
    order = await pending_order(desk, hours=5)
    stats = await wake(desk)
    assert (stats["checks"], stats["ran"], stats["skipped"]) == (len(HOURLY), len(HOURLY), 0)
    assert (stats["found"], stats["new"], stats["notified"]) == (1, 1, 1)
    [finding] = (await findings(desk)).values()
    assert finding["fingerprint"] == f"order_review:order:{order['id']}"
    assert (finding["status"], finding["severity"]) == ("open", "warning")
    assert finding["title"] == f"订单 {order['no']} 提交审核超过 4 小时还没审核"
    assert finding["link"] == f"/orders?id={order['id']}"
    assert finding["assignee_ids"] == [await admin_id(desk)]
    assert "since" in finding["data"]
    assert await notes(desk, runner.KIND_FINDING) == [
        (f"AI 巡检：{finding['title']}", finding["detail"])
    ]

    # 一小时后再检查：增量更新索引里订单等表都没有变化、也没到时限，全部跳过（不再查询这些表），
    # 也不重复通知。
    later = datetime.now(UTC) + timedelta(minutes=50)
    again = await wake(desk, now=later)
    assert (again["ran"], again["skipped"], again["new"]) == (0, len(HOURLY), 0)
    assert len(await notes(desk, runner.KIND_FINDING)) == 1

    # 等满 24 小时：数据没变，但到了变严重的时刻（检查时登记的），重新检查并再通知一次。
    raised = await wake(desk, now=datetime.now(UTC) + timedelta(hours=20))
    assert raised["ran"] == 1 and raised["raised"] == 1
    [finding] = (await findings(desk)).values()
    assert (finding["severity"], finding["title"]) == (
        "critical",
        f"订单 {order['no']} 提交审核超过 24 小时还没审核",
    )
    assert len(await notes(desk, runner.KIND_FINDING)) == 2

    # 订单审核了：订单表有了新的变化，检查项重新运行，问题自动消除。
    await desk.sql(
        "UPDATE orders SET status = 'confirmed', confirmed_at = now() WHERE id = $1",
        uuid.UUID(order["id"]),
    )
    resolved = await wake(desk, now=datetime.now(UTC) + timedelta(hours=21))
    assert resolved["ran"] == 1 and resolved["resolved"] == 1
    [finding] = (await findings(desk)).values()
    assert (finding["status"], finding["resolve_note"]) == (
        "resolved",
        "检查不再发现这个问题，已自动消除",
    )


async def test_due_time_wakes_a_check_without_data_changes(desk: Desk) -> None:
    """还没到时限的对象：检查时登记"到时限的时刻"，数据不变时到点才再检查。"""
    order = await pending_order(desk, hours=3)
    first = await wake(desk)
    assert first["found"] == 0
    [state] = await desk.sql(
        "SELECT * FROM wake_check_state WHERE tenant_id = $1 AND check_code = 'order_review'",
        desk.tenant_id,
    )
    [row] = await desk.sql("SELECT submitted_at FROM orders WHERE id = $1", uuid.UUID(order["id"]))
    assert state["next_due_at"] == row["submitted_at"] + timedelta(hours=4)
    assert (await wake(desk, now=datetime.now(UTC) + timedelta(minutes=30)))["ran"] == 0
    second = await wake(desk, now=datetime.now(UTC) + timedelta(hours=2))
    assert (second["ran"], second["new"]) == (1, 1)


async def test_daily_brief_escalation_and_disabled_checks(desk: Desk, fake_llm: FakeLLM) -> None:
    products = await catalog(desk)
    bell = f"/api/v1/products/{products['BELL-D1']}"
    alert = await desk.client.put(
        bell,
        headers=desk.admin,
        json={"code": "BELL-D1", "name": "可视门铃 D1", "retail_price": "199", "stock_alert": 5},
    )
    assert alert.status_code == 200, alert.text
    stocked = await desk.client.post(
        f"{bell}/stock", headers=desk.admin, json={"mode": "set", "quantity": 2}
    )
    assert stocked.status_code == 200, stocked.text
    await pending_order(desk, hours=30, products=products)
    stats = await wake(desk, RunKind.DAILY)
    assert stats["errors"] == [] and stats["new"] == 2
    assert stats["brief_source"] == "llm" and stats["llm_calls"] == 1
    assert stats["open"] == {"critical": 1, "warning": 1, "info": 0}
    by_check = {f["check_code"]: f for f in (await findings(desk)).values()}
    assert by_check["stock_low"]["title"] == "成品「可视门铃 D1」库存不足"
    assert by_check["order_review"]["severity"] == "critical"
    # 简报：大模型按巡检结果写（已脱敏），最要紧的是严重的那个。
    [(title, body)] = await notes(desk, runner.KIND_BRIEF)
    assert title == "AI 巡检日报：2 个问题待处理（严重 1）"
    assert body is not None and body.startswith("今天共 2 个问题待处理。最要紧的是[严重][订单]")
    brief_request = fake_llm.requests[-1]
    assert brief_request["messages"][0]["content"].startswith("任务：巡检简报")

    # 超过 2 天没处理：升级给管理员（只升级一次），写在简报里。
    await desk.sql(
        "UPDATE wake_findings SET first_seen_at = now() - interval '3 days' WHERE tenant_id = $1",
        desk.tenant_id,
    )
    escalated = await wake(desk, RunKind.DAILY, now=datetime.now(UTC) + timedelta(days=1))
    assert escalated["escalated"] == 2
    report = fake_llm.requests[-1]["messages"][1]["content"]
    assert "今天升级给管理员" in report and "超过 2 天没处理" in report
    again = await wake(desk, RunKind.DAILY, now=datetime.now(UTC) + timedelta(days=2))
    assert again["escalated"] == 0

    # 关闭检查项：之前发现的问题消除，状态删除（再打开时重新检查）。
    response = await put_settings(desk, checks={"stock_low": {"enabled": False}})
    assert response.status_code == 200, response.text
    off = await wake(desk, RunKind.DAILY, now=datetime.now(UTC) + timedelta(days=3))
    assert off["checks"] == len(checks.CHECKS) - 1 and off["resolved"] == 1
    stock = next(f for f in (await findings(desk)).values() if f["check_code"] == "stock_low")
    assert (stock["status"], stock["resolve_note"]) == ("resolved", runner.RETIRED_NOTE)


async def test_brief_falls_back_when_the_model_is_down(desk: Desk, fake_llm: FakeLLM) -> None:
    await pending_order(desk, hours=5)
    fake_llm.mode = "down"
    stats = await wake(desk, RunKind.DAILY)
    assert (stats["brief_source"], stats["llm_calls"]) == ("template", 0)
    [(_, body)] = await notes(desk, runner.KIND_BRIEF)
    assert body is not None and body.startswith("目前有 1 个问题待处理（注意 1），今天新出现 1 个")


async def test_a_failing_check_keeps_its_findings(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pending_order(desk, hours=5)
    await wake(desk)
    state_sql = (
        "SELECT checked_at FROM wake_check_state WHERE tenant_id = $1"
        " AND check_code = 'order_review'"
    )
    [before] = await desk.sql(state_sql, desk.tenant_id)

    async def broken(scope: checks.Scope) -> list[checks.Hit]:
        raise RuntimeError("boom")

    original = checks.BY_CODE["order_review"]
    replaced = checks.Check(**{**original.__dict__, "run": broken})
    monkeypatch.setitem(checks.BY_CODE, "order_review", replaced)
    monkeypatch.setattr(
        checks, "CHECKS", tuple(replaced if c.code == "order_review" else c for c in checks.CHECKS)
    )
    stats = await wake(desk, trigger=RunTrigger.MANUAL)
    assert stats["errors"] == ["order_review"] and stats["resolved"] == 0
    [finding] = (await findings(desk)).values()
    assert finding["status"] == "open"
    # 出错的这次不记检查状态（仍然是上次成功检查时的）。
    assert await desk.sql(state_sql, desk.tenant_id) == [before]


async def test_findings_api_mine_ignore_resolve_and_permissions(desk: Desk) -> None:
    order = await pending_order(desk, hours=5)
    await wake(desk)
    # 加工岗位的员工：没有审核订单的权限（不是负责人），也没有"设置"权限。
    alice = await desk.agent("alice", roles=["worker"], online=False)

    mine = (await desk.client.get(f"{WAKE}/findings", headers=desk.admin)).json()
    assert mine["total"] == 1 and mine["open"]["total"] == 1
    item = mine["items"][0]
    assert (item["mine"], item["can_handle"], item["check_title"]) == (True, True, "订单待审核超时")
    assert item["assignees"][0]["name"] == "管理员"
    # 坐席：只能看自己负责的；看全部、处理别人的问题、打开 AI 唤醒页面都不行。
    assert (await desk.client.get(f"{WAKE}/findings", headers=alice.headers)).json()["total"] == 0
    denied = [
        await desk.client.get(f"{WAKE}/findings?view=all", headers=alice.headers),
        await desk.client.post(
            f"{WAKE}/findings/{item['id']}/ignore", headers=alice.headers, json={"days": 7}
        ),
        await desk.client.post(f"{WAKE}/findings/{item['id']}/resolve", headers=alice.headers),
        await desk.client.get(f"{WAKE}/overview", headers=alice.headers),
        await desk.client.get(f"{WAKE}/settings", headers=alice.headers),
        await desk.client.post(f"{WAKE}/runs", headers=alice.headers, json={"kind": "daily"}),
    ]
    assert [r.status_code for r in denied] == [403] * 6

    # 忽略 7 天：期间仍然存在也不再提醒；到期后仍然存在就重新打开、再通知。
    ignored = await desk.client.post(
        f"{WAKE}/findings/{item['id']}/ignore",
        headers=desk.admin,
        json={"days": 7, "note": "客户在核对地址"},
    )
    assert ignored.status_code == 200, ignored.text
    assert (ignored.json()["status"], ignored.json()["ignore_note"]) == (
        "ignored",
        "客户在核对地址",
    )
    await wake(desk, trigger=RunTrigger.MANUAL)
    assert len(await notes(desk, runner.KIND_FINDING)) == 1
    reopened = await wake(desk, now=datetime.now(UTC) + timedelta(days=8))
    assert reopened["new"] == 1
    [finding] = (await findings(desk)).values()
    assert (finding["status"], finding["ignored_until"]) == ("open", None)
    assert len(await notes(desk, runner.KIND_FINDING)) == 2

    # 标记已处理：这个检查项下次一定重新检查（不按索引跳过），仍然发现就重新打开。
    done = await desk.client.post(
        f"{WAKE}/findings/{item['id']}/resolve", headers=desk.admin, json={"note": "已审核"}
    )
    assert done.json()["status"] == "resolved" and done.json()["resolved_by"]["name"] == "管理员"
    back = await wake(desk, now=datetime.now(UTC) + timedelta(days=8, minutes=5))
    assert (back["ran"], back["new"]) == (1, 1)
    await desk.sql(
        "UPDATE orders SET status = 'confirmed', confirmed_at = now() WHERE id = $1",
        uuid.UUID(order["id"]),
    )
    await wake(desk, now=datetime.now(UTC) + timedelta(days=8, minutes=10))
    listed = (
        await desk.client.get(f"{WAKE}/findings?view=all&status=resolved", headers=desk.admin)
    ).json()
    assert listed["total"] == 1 and listed["open"]["total"] == 0


async def test_settings_runs_overview_and_manual_wake(desk: Desk) -> None:
    catalog_ = (await desk.client.get(f"{WAKE}/settings", headers=desk.admin)).json()
    assert catalog_["settings"]["daily_time"] == "08:30"
    codes = [c["code"] for c in catalog_["checks"]]
    assert codes == [c.code for c in checks.CHECKS]
    review = next(c for c in catalog_["checks"] if c["code"] == "order_review")
    assert (review["hourly"], review["available"], review["domains"]) == (True, True, ["orders"])
    assert review["params"] == [
        {"name": "hours", "label": "超过", "unit": "小时", "default": 4, "minimum": 1,
         "maximum": 72, "value": 4},
    ]  # fmt: skip

    bad = await put_settings(desk, checks={"order_review": {"params": {"hours": 100}}})
    assert bad.status_code == 422 and "1–72小时" in bad.text
    unknown = await put_settings(desk, checks={"nope": {}})
    assert unknown.status_code == 422
    saved = await put_settings(
        desk,
        daily_time="09:15",
        checks={
            "order_review": {"enabled": True, "params": {"hours": 8}},
            "todo_overdue": {"enabled": True, "params": {"count": 5}},
        },
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    # 只保存和默认不同的。
    assert body["settings"]["checks"] == {"order_review": {"enabled": True, "params": {"hours": 8}}}
    assert next(c for c in body["checks"] if c["code"] == "order_review")["params"][0]["value"] == 8

    # 立即唤醒：排进队列，实时消费进程领取后全部重新检查。
    await pending_order(desk, hours=5)
    queued = await desk.client.post(f"{WAKE}/runs", headers=desk.admin, json={"kind": "daily"})
    assert queued.status_code == 200, queued.text
    assert (queued.json()["status"], queued.json()["trigger"]) == ("queued", "manual")
    assert await runner.run_due(desk.ctx) == 1
    runs = (await desk.client.get(f"{WAKE}/runs", headers=desk.admin)).json()
    [run] = runs["items"]
    assert (run["status"], run["kind_label"], run["created_by"]["name"]) == (
        "done",
        "每日巡检",
        "管理员",
    )
    # 口径改成 8 小时：5 小时的订单不算问题。
    assert run["stats"]["ran"] == len(checks.CHECKS) and run["stats"]["found"] == 0
    assert run["summary"] == "今天没有发现需要处理的问题。"

    overview = (await desk.client.get(f"{WAKE}/overview", headers=desk.admin)).json()
    assert overview["available"] and overview["enabled"]
    assert overview["brief"]["id"] == run["id"] and overview["pending"] == []
    assert overview["next_daily"] is not None and overview["next_kb"] is not None
    labels = {d["domain"]: d["label"] for d in overview["data_index"]}
    assert labels["orders"] == "订单" and overview["tracked"] >= len(labels)

    # 关闭 AI 唤醒后不能立即唤醒。
    await put_settings(desk, enabled=False)
    closed = await desk.client.post(f"{WAKE}/runs", headers=desk.admin, json={"kind": "daily"})
    assert closed.status_code == 422


def test_due_slots_follow_business_hours_and_catch_up() -> None:
    settings = WakeSettings()
    tz_offset = timedelta(hours=8)
    # 周四 10:05（上海）：工作时间内，每日巡检已过 08:30，知识库整理本周一已过（补上）。
    thursday = datetime(2026, 10, 1, 10, 5, tzinfo=UTC) - tz_offset
    assert runner.due_slots(settings, WORKDAYS, thursday) == [
        ("hourly", "2026-10-01T10"),
        ("daily", "2026-10-01"),
        ("kb", "2026-W40"),
    ]
    # 周六：不在工作日，不做每小时检查和每日巡检（改成每天时周末也巡检）。
    saturday = datetime(2026, 10, 3, 10, 5, tzinfo=UTC) - tz_offset
    assert runner.due_slots(settings, WORKDAYS, saturday) == [("kb", "2026-W40")]
    every_day = settings.model_copy(update={"daily_workdays_only": False})
    assert ("daily", "2026-10-03") in runner.due_slots(every_day, WORKDAYS, saturday)
    # 早上 07:00 还没到每日巡检的时间；关闭后什么都不登记。
    early = datetime(2026, 10, 1, 7, 0, tzinfo=UTC) - tz_offset
    assert runner.due_slots(settings, WORKDAYS, early) == [("kb", "2026-W40")]
    assert runner.due_slots(settings.model_copy(update={"enabled": False}), None, thursday) == []


def test_next_times() -> None:
    settings = WakeSettings()
    tz_offset = timedelta(hours=8)
    friday_evening = datetime(2026, 10, 2, 18, 30, tzinfo=UTC) - tz_offset
    current = {("daily", "2026-10-02"), ("kb", "2026-W40")}
    times = wake_service.next_times(settings, WORKDAYS, friday_evening, current)
    local = {k: v.astimezone(UTC) + tz_offset if v else None for k, v in times.items()}
    # 下周一 09:00 第一次每小时检查、08:30 每日巡检、08:00 知识库整理。
    assert local == {
        "hourly": datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
        "daily": datetime(2026, 10, 5, 8, 30, tzinfo=UTC),
        "kb": datetime(2026, 10, 5, 8, 0, tzinfo=UTC),
    }
    # 今天的每日巡检还没登记（例如刚把时间改早了）：一分钟内就会唤醒。
    pending = wake_service.next_times(settings, WORKDAYS, friday_evening, {("kb", "2026-W40")})
    assert pending["daily"] == friday_evening


async def test_dispatch_registers_each_slot_once_and_workers_claim_them(desk: Desk) -> None:
    now = datetime.now(UTC)
    first = await runner.dispatch(desk.ctx, now=now)
    again = await runner.dispatch(desk.ctx, now=now + timedelta(seconds=30))
    rows = await desk.sql(
        "SELECT kind, trigger, status FROM wake_runs WHERE tenant_id = $1 ORDER BY kind",
        desk.tenant_id,
    )
    # 测试租户全天服务：每小时检查一定到期；每日巡检、知识库整理看现在几点。
    assert first == len(rows) >= 1 and again == 0
    assert {r["trigger"] for r in rows} == {"schedule"}
    assert "hourly" in {r["kind"] for r in rows}
    handled = await runner.run_due(desk.ctx, limit=10)
    assert handled == len(rows)
    statuses = await desk.sql("SELECT status FROM wake_runs WHERE tenant_id = $1", desk.tenant_id)
    assert {r["status"] for r in statuses} == {"done"}


async def test_cli_wake_run_skips_unchanged_checks(desk: Desk, settings: Settings) -> None:
    """命令行立即唤醒：不加 --force 时和定时唤醒一样按增量更新索引跳过没有变化的检查项。"""
    first = await cli.wake_run(settings, "acme", "daily", False)
    assert first["status"] == "done" and first["stats"]["ran"] == len(checks.CHECKS)
    again = await cli.wake_run(settings, "acme", "daily", False)
    assert (again["stats"]["ran"], again["stats"]["skipped"]) == (0, len(checks.CHECKS))
    forced = await cli.wake_run(settings, "acme", "hourly", True)
    assert forced["stats"]["ran"] == len(HOURLY)
    with pytest.raises(SystemExit):
        await cli.wake_run(settings, "nope", "daily", False)
