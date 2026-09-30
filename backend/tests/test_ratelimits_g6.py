"""G6 按租户限流：员工接口、访客接口、OpenIM 回调（超过时由对账补上）和大模型调用；
运营后台调整限额。
"""

import json

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import REGISTRY

from app.core.config import Settings
from app.modules.conversation.reconcile import reconcile_all
from tests.desk import Desk
from tests.factories import (
    STAFF_PASSWORD,
    bearer,
    create_platform_admin,
    create_staff,
    login,
    platform_login,
    provision,
)
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_ai_reception import enable_ai


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


@pytest.fixture
async def ops(desk: Desk) -> dict[str, str]:
    await create_platform_admin(desk.app)
    return bearer(await platform_login(desk.client))


async def set_limits(desk: Desk, ops: dict[str, str], **values: int | None) -> dict:
    response = await desk.client.put(
        f"/platform/v1/tenants/{desk.tenant_id}/rate-limits", headers=ops, json=values
    )
    assert response.status_code == 200, response.text
    body: dict = response.json()
    return body


def limited(rule: str, tenant: str) -> float:
    return (
        REGISTRY.get_sample_value("edp_rate_limited_total", {"rule": rule, "tenant": tenant}) or 0
    )


async def test_staff_requests_are_limited_per_tenant(desk: Desk, ops: dict[str, str]) -> None:
    other = await provision(desk.app, "globex")
    other_admin = bearer(await login(desk.client, "globex"))
    await set_limits(desk, ops, api=5)
    before = limited("tenant-api", str(desk.tenant_id))

    codes = [
        (await desk.client.get("/api/v1/customers", headers=desk.admin)).status_code
        for _ in range(7)
    ]

    assert codes[:5] == [200] * 5
    assert codes[5:] == [429, 429]
    rejected = await desk.client.get("/api/v1/customers", headers=desk.admin)
    assert rejected.status_code == 429
    assert int(rejected.headers["Retry-After"]) >= 1
    assert "请求过于频繁" in rejected.json()["error"]["message"]
    assert limited("tenant-api", str(desk.tenant_id)) == before + 3
    # 其他租户不受影响。
    assert (await desk.client.get("/api/v1/customers", headers=other_admin)).status_code == 200
    assert other


async def test_visitors_are_limited_per_person(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desk.ctx.settings, "visitor_per_minute", 3)
    first = await desk.visitor()
    second = await desk.visitor()

    def state(token: str) -> httpx.Request:
        return desk.client.build_request(
            "GET", "/api/v1/visitor/session", headers={"X-Visitor-Token": token}
        )

    codes = [(await desk.client.send(state(first.visitor_token))).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    # 另一位访客不受影响。
    assert (await desk.client.send(state(second.visitor_token))).status_code == 200


async def test_callbacks_over_the_limit_are_recovered_by_reconcile(
    desk: Desk, ops: dict[str, str]
) -> None:
    visitor = await desk.visitor()
    await set_limits(desk, ops, webhook=1)
    deferred = REGISTRY.get_sample_value(
        "edp_webhooks_total",
        {"source": "openim", "tenant": str(desk.tenant_id), "outcome": "deferred"},
    )

    await desk.say(visitor, "第一条")
    await desk.say(visitor, "第二条")

    # 第一条之后的回调（系统提示、第二条）超过每分钟 1 次的上限，暂不入库。
    texts = {r["text_plain"] for r in await desk.sql("SELECT text_plain FROM messages")}
    assert "第一条" in texts and "第二条" not in texts
    now_deferred = REGISTRY.get_sample_value(
        "edp_webhooks_total",
        {"source": "openim", "tenant": str(desk.tenant_id), "outcome": "deferred"},
    )
    assert (now_deferred or 0) >= (deferred or 0) + 1

    report = await reconcile_all(desk.ctx.db, desk.ctx.im, bus=desk.ctx.bus)
    assert report.recovered >= 1
    texts = {r["text_plain"] for r in await desk.sql("SELECT text_plain FROM messages")}
    assert {"第一条", "第二条"} <= texts


async def test_llm_calls_over_the_limit_hand_over_to_agents(
    desk: Desk, ops: dict[str, str]
) -> None:
    await enable_ai(desk)
    alice = await desk.agent("alice")
    await set_limits(desk, ops, llm=1)
    visitor = await desk.visitor()

    await desk.say(visitor, "快递几天能到")

    # 超过每分钟的调用上限与模型不可用一样：本轮转人工。
    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"], chat["handoff_reason"]) == (
        "human_serving",
        alice.staff_id,
        "ai_unavailable",
    )
    calls = await desk.sql("SELECT status, error FROM llm_calls WHERE status = 'busy'")
    assert calls and "过于频繁" in calls[0]["error"]


async def test_platform_adjusts_tenant_limits(desk: Desk, ops: dict[str, str]) -> None:
    initial = await desk.client.get(
        f"/platform/v1/tenants/{desk.tenant_id}/rate-limits", headers=ops
    )
    assert initial.status_code == 200, initial.text
    body = initial.json()
    assert body["overrides"] == {"api": None, "visitor": None, "webhook": None, "llm": None}
    assert body["defaults"] == {"api": 6000, "visitor": 6000, "webhook": 6000, "llm": 600}
    assert body["effective"] == body["defaults"]
    assert body["visitor_per_minute"] == 120

    await desk.client.get("/api/v1/customers", headers=desk.admin)
    updated = await set_limits(desk, ops, api=100, llm=0)
    assert updated["overrides"] == {"api": 100, "visitor": None, "webhook": None, "llm": 0}
    assert updated["effective"] == {"api": 100, "visitor": 6000, "webhook": 6000, "llm": 0}
    assert updated["usage"]["api"] >= 1

    cleared = await set_limits(desk, ops)
    assert cleared["effective"] == cleared["defaults"]
    audit = await desk.sql(
        "SELECT detail FROM audit_logs WHERE action = 'tenant.rate_limits' ORDER BY created_at"
    )
    assert [json.loads(a["detail"]) for a in audit] == [{"api": 100, "llm": 0}, {}]

    invalid = await desk.client.put(
        f"/platform/v1/tenants/{desk.tenant_id}/rate-limits", headers=ops, json={"api": -1}
    )
    assert invalid.status_code == 422
    denied = await desk.client.get(
        f"/platform/v1/tenants/{desk.tenant_id}/rate-limits", headers=desk.admin
    )
    assert denied.status_code == 401
    staff = await create_staff(desk.client, desk.admin_token, "bob")
    assert staff and STAFF_PASSWORD
