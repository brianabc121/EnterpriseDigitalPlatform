"""企业 token 计费（设计文档 §37）：按月汇总（合计、上月同期、每天、按场景 / 模型 / 员工）、
近 7 天明细的筛选和导出、网关记下触发的员工、自带密钥、权限和菜单、租户隔离。
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.errors import Unprocessable
from app.modules.ai import gateway
from app.modules.tokens import service
from tests.desk import Desk
from tests.factories import provision
from tests.fake_llm import FakeLLM
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls

T = "/api/v1/tokens"
SHANGHAI = ZoneInfo("Asia/Shanghai")


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
    path: str,
    expected: int = 200,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    response = await desk.client.get(path, headers=headers or desk.admin, params=params)
    assert response.status_code == expected, response.text
    return response.json()


async def record(
    desk: Desk,
    scene: str,
    *,
    at: datetime,
    prompt: int = 0,
    completion: int = 0,
    cost: float = 0.0,
    staff_id: uuid.UUID | None = None,
    provider: str = "fake",
    model: str = "fake-chat",
    status: str = "ok",
    error: str | None = None,
    tenant_id: uuid.UUID | None = None,
) -> None:
    await desk.sql(
        "INSERT INTO llm_calls (id, tenant_id, scene, provider, model, prompt_tokens,"
        " completion_tokens, latency_ms, status, error, cost, staff_id, created_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, 800, $8, $9, $10, $11, $12)",
        uuid.uuid4(),
        tenant_id or desk.tenant_id,
        scene,
        provider,
        model,
        prompt,
        completion,
        status,
        error,
        cost,
        staff_id,
        at,
    )


async def admin_id(desk: Desk) -> uuid.UUID:
    return uuid.UUID((await call(desk, "/api/v1/me"))["id"])


async def seed(desk: Desk, agent_id: uuid.UUID, admin: uuid.UUID) -> datetime:
    """这个月的 6 次调用（几秒前），另有上个月 1 号的一次（在上月同期里，也早于 7 天）。"""
    now = datetime.now(UTC)
    await record(desk, "reply", at=now - timedelta(seconds=1), prompt=100, completion=20, cost=0.5)
    await record(
        desk,
        "suggest",
        at=now - timedelta(seconds=2),
        prompt=300,
        completion=50,
        cost=1.25,
        staff_id=agent_id,
    )
    await record(
        desk,
        "test",
        at=now - timedelta(seconds=3),
        prompt=200,
        completion=30,
        cost=0.8,
        staff_id=admin,
    )
    await record(
        desk,
        "embed",
        at=now - timedelta(seconds=4),
        prompt=40,
        cost=0.04,
        provider="embed",
        model="fake-embed",
    )
    await record(
        desk,
        "reply",
        at=now - timedelta(seconds=5),
        status="error",
        error="timeout",
    )
    await record(
        desk,
        "reply",
        at=now - timedelta(seconds=6),
        prompt=10,
        completion=5,
        provider="tenant",
        model="own-chat",
    )
    month_start = datetime.combine(
        now.astimezone(SHANGHAI).date().replace(day=1), datetime.min.time(), SHANGHAI
    )
    previous_start = datetime.combine(
        service._first_of_previous(month_start.date()), datetime.min.time(), SHANGHAI
    )
    await record(
        desk, "reply", at=previous_start + timedelta(seconds=1), prompt=70, completion=7, cost=0.3
    )
    return now


def test_month_ranges_follow_the_tenant_calendar() -> None:
    now = datetime(2026, 3, 31, 2, 0, tzinfo=UTC)  # 上海 3 月 31 日 10:00
    current = service.month_range(None, SHANGHAI, now)
    assert (current.key, current.first, current.last) == (
        "2026-03",
        date(2026, 3, 1),
        date(2026, 3, 31),
    )
    assert current.start == datetime(2026, 3, 1, tzinfo=SHANGHAI) and current.end == now
    # 和上个月的同一时段比较，2 月短，不超过 2 月底。
    assert current.previous_start == datetime(2026, 2, 1, tzinfo=SHANGHAI)
    assert current.previous_end == datetime(2026, 3, 1, tzinfo=SHANGHAI)

    early = service.month_range(None, SHANGHAI, datetime(2026, 1, 10, 4, 0, tzinfo=UTC))
    assert early.previous_start == datetime(2025, 12, 1, tzinfo=SHANGHAI)
    assert early.previous_end == datetime(2025, 12, 10, 12, 0, tzinfo=SHANGHAI)

    past = service.month_range("2026-02", SHANGHAI, now)
    assert (past.first, past.last) == (date(2026, 2, 1), date(2026, 2, 28))
    assert past.end == datetime(2026, 3, 1, tzinfo=SHANGHAI)
    assert (past.previous_start, past.previous_end) == (
        datetime(2026, 1, 1, tzinfo=SHANGHAI),
        datetime(2026, 2, 1, tzinfo=SHANGHAI),
    )
    with pytest.raises(Unprocessable):
        service.month_range("2026-04", SHANGHAI, now)
    with pytest.raises(Unprocessable):
        service.month_range("2026-13", SHANGHAI, now)


async def test_monthly_summary(desk: Desk) -> None:
    agent = await desk.agent("amy", roles=["agent"], online=False)
    admin = await admin_id(desk)
    now = await seed(desk, agent.staff_id, admin)
    today = now.astimezone(SHANGHAI).date()

    summary = await call(desk, f"{T}/summary")
    assert (summary["month"], summary["today"], summary["timezone"]) == (
        f"{today:%Y-%m}",
        today.isoformat(),
        "Asia/Shanghai",
    )
    totals = summary["totals"]
    assert totals == {
        "calls": 6,
        "failed": 1,
        "prompt_tokens": 650,
        "completion_tokens": 105,
        "tokens": 755,
        "cost": pytest.approx(2.59),
    }
    assert summary["previous"]["calls"] == 1 and summary["previous"]["tokens"] == 77
    assert len(summary["days"]) == today.day
    assert summary["days"][-1]["day"] == today.isoformat()
    assert summary["days"][-1]["tokens"] == 755
    assert sum(d["calls"] for d in summary["days"]) == 6

    scenes = {g["key"]: g for g in summary["by_scene"]}
    assert scenes["reply"]["label"] == "AI 接待回复"
    assert (scenes["reply"]["calls"], scenes["reply"]["failed"], scenes["reply"]["tokens"]) == (
        3,
        1,
        135,
    )
    assert scenes["embed"]["label"] == "知识向量化" and scenes["embed"]["tokens"] == 40
    assert summary["by_scene"][0]["key"] == "suggest"  # tokens 最多的在前

    models = {g["key"]: g["label"] for g in summary["by_model"]}
    assert models["tenant/own-chat"] == "own-chat（自带密钥）"
    assert models["fake/fake-chat"] == "fake-chat（fake）"

    staff = {g["label"]: g for g in summary["by_staff"]}
    assert staff["Amy"]["tokens"] == 350 and staff["Amy"]["cost"] == pytest.approx(1.25)
    assert staff["系统（AI 接待、定时任务）"]["calls"] == 4
    assert summary["own_key"] is False

    # 以前的月份：上个月整月。
    previous = await call(desk, f"{T}/summary", params={"month": _previous(today)})
    assert previous["totals"]["calls"] == 1 and previous["days"][0]["tokens"] == 77
    await call(desk, f"{T}/summary", 422, params={"month": "2099-01"})
    await call(desk, f"{T}/summary", 422, params={"month": "202610"})

    # 企业改用自己的接口密钥后标出来。
    await desk.sql(
        "INSERT INTO ai_settings (tenant_id, byo_llm) VALUES ($1, $2::jsonb)"
        " ON CONFLICT (tenant_id) DO UPDATE SET byo_llm = EXCLUDED.byo_llm",
        desk.tenant_id,
        '{"enabled": true, "base_url": "https://llm.example.com/v1", "chat_model": "own-chat"}',
    )
    assert (await call(desk, f"{T}/summary"))["own_key"] is True


def _previous(today: date) -> str:
    return f"{service._first_of_previous(today.replace(day=1)):%Y-%m}"


async def test_recent_calls_filters_and_export(desk: Desk) -> None:
    agent = await desk.agent("amy", roles=["agent"], online=False)
    admin = await admin_id(desk)
    now = await seed(desk, agent.staff_id, admin)
    await record(desk, "extract", at=now - timedelta(days=8), prompt=999, completion=1, cost=9)

    # 8 天前的和上个月的不在明细里。
    page = await call(desk, f"{T}/calls")
    assert page["total"] == 6 and len(page["items"]) == 6
    assert all(i["scene"] != "extract" for i in page["items"])
    assert page["items"][0]["scene"] == "reply" and page["items"][0]["tokens"] == 120
    assert (page["tokens"], page["cost"]) == (755, pytest.approx(2.59))
    first = page["items"][0]
    assert (first["scene_label"], first["staff_name"], first["own_key"]) == (
        "AI 接待回复",
        None,
        False,
    )
    own = next(i for i in page["items"] if i["provider"] == "tenant")
    assert own["own_key"] is True and own["cost"] == 0
    assert {"suggest", "test", "embed", "reply"} <= {s["key"] for s in page["scenes"]}
    staff = {s["id"]: s["name"] for s in page["staff"]}
    assert staff[str(agent.staff_id)] == "Amy" and staff["system"] == "系统"

    only = await call(desk, f"{T}/calls", params={"scene": "suggest"})
    assert only["total"] == 1 and only["items"][0]["staff_name"] == "Amy"
    assert only["tokens"] == 350 and only["cost"] == pytest.approx(1.25)
    failed = await call(desk, f"{T}/calls", params={"status": "failed"})
    assert failed["total"] == 1 and failed["items"][0]["error"] == "timeout"
    mine = await call(desk, f"{T}/calls", params={"staff_id": str(agent.staff_id)})
    assert [i["scene"] for i in mine["items"]] == ["suggest"]
    system = await call(desk, f"{T}/calls", params={"staff_id": "system"})
    assert all(i["staff_id"] is None for i in system["items"]) and system["total"] == 4
    await call(desk, f"{T}/calls", 422, params={"staff_id": "not-a-uuid"})
    paged = await call(desk, f"{T}/calls", params={"limit": 2, "offset": 2})
    assert len(paged["items"]) == 2 and paged["total"] == page["total"]

    response = await desk.client.get(f"{T}/calls/export", headers=desk.admin)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    text = response.content.decode("utf-8")
    assert text.startswith("﻿时间,场景,供应商,模型,输入 tokens")
    lines = text.strip().splitlines()
    assert len(lines) == 1 + 6
    assert any("自带密钥" in line and "own-chat" in line for line in lines)
    assert any("知识向量化" in line for line in lines)
    assert any("Amy" in line and "坐席助手建议回复" in line and "0.0125" in line for line in lines)
    [audit] = await desk.sql(
        "SELECT detail FROM audit_logs WHERE action = 'token.export' AND tenant_id = $1",
        desk.tenant_id,
    )
    assert '"rows": 6' in audit["detail"]


async def test_gateway_records_who_triggered_the_call(
    desk: Desk, app: FastAPI, fake_llm: FakeLLM
) -> None:
    admin = await admin_id(desk)
    response = await desk.client.post(
        "/api/v1/ai/test", headers=desk.admin, json={"question": "快递几天能到"}
    )
    assert response.status_code == 200, response.text
    rows = await desk.sql("SELECT staff_id FROM llm_calls WHERE scene = 'test'")
    assert rows and all(row["staff_id"] == admin for row in rows)

    # 后台任务（没有员工的请求）记为系统。
    await gateway.chat(
        app.state.ctx, desk.tenant_id, [{"role": "user", "content": "你好"}], scene="summary"
    )
    [system] = await desk.sql("SELECT staff_id FROM llm_calls WHERE scene = 'summary'")
    assert system["staff_id"] is None
    summary = await call(desk, f"{T}/summary")
    labels = {g["label"] for g in summary["by_staff"]}
    assert "系统（AI 接待、定时任务）" in labels and len(labels) == 2


async def test_permissions_menus_and_isolation(desk: Desk, app: FastAPI) -> None:
    agent = await desk.agent("amy", roles=["agent"], online=False)
    # 财务岗位的默认权限和系统角色"财务"都有 token:view。
    profiles = await call(desk, "/api/v1/roles/profile-permissions")
    defaults = next(p for p in profiles["items"] if p["profile"] == "finance")["permissions"]
    assert "token:view" in defaults
    roles = (await call(desk, "/api/v1/roles"))["items"]
    assert "token:view" in next(r for r in roles if r["code"] == "finance")["permissions"]
    finance = await desk.agent("fay", roles=["finance"], online=False)
    for path in (f"{T}/summary", f"{T}/calls", f"{T}/calls/export"):
        assert (await desk.client.get(path, headers=agent.headers)).status_code == 403
        assert (await desk.client.get(path, headers=finance.headers)).status_code == 200
    me = (await desk.client.get("/api/v1/me", headers=finance.headers)).json()
    assert "tokens" in me["console"]["menus"] and "token:view" in me["permissions"]
    admin_menus = (await call(desk, "/api/v1/me"))["console"]["menus"]
    assert "tokens" in admin_menus
    agent_me = (await desk.client.get("/api/v1/me", headers=agent.headers)).json()
    assert "tokens" not in agent_me["console"]["menus"]

    other = await provision(app, "beta")
    await record(desk, "reply", at=datetime.now(UTC), prompt=5000, completion=1, tenant_id=other)
    totals = (await call(desk, f"{T}/summary"))["totals"]
    assert totals["tokens"] == 0 and totals["calls"] == 0
    assert (await call(desk, f"{T}/calls"))["total"] == 0
