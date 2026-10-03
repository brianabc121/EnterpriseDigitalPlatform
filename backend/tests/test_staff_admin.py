"""员工管理（修改、停用、重置密码、修改自己的密码）与自定义角色。"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.permissions import Permission
from tests.desk import Desk
from tests.factories import (
    ADMIN_PASSWORD,
    STAFF_PASSWORD,
    bearer,
    create_platform_admin,
    platform_login,
)
from tests.factories import login as login_token
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def _login(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/login",
        json={"tenant_code": "acme", "username": username, "password": password},
    )


async def _patch(desk: Desk, staff_id: Any, headers: dict[str, str], **body: Any) -> httpx.Response:
    return await desk.client.patch(f"/api/v1/staff/{staff_id}", headers=headers, json=body)


async def test_disabling_staff_logs_out_and_requeues_their_sessions(desk: Desk) -> None:
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == alice.staff_id
    # 已经回复过的会话，停用时也退回队列。
    replied = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages",
        headers=alice.headers,
        json={"client_msg_id": "a" * 16, "text": "您好"},
    )
    assert replied.status_code == 200, replied.text
    await desk.flush()
    refresh_cookie = desk.client.cookies.get("edp_refresh")

    disabled = await _patch(desk, alice.staff_id, desk.admin, status="disabled")
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    await desk.flush()

    assert (await desk.client.get("/api/v1/me", headers=alice.headers)).status_code == 401
    assert (await _login(desk.client, "alice", STAFF_PASSWORD)).status_code == 403
    [state] = await desk.sql("SELECT status FROM agent_states WHERE staff_id = $1", alice.staff_id)
    assert state["status"] == "offline"
    requeued = await desk.session_of(visitor)
    assert (requeued["status"], requeued["assignee_id"]) == ("queued", None)
    assert "requeued" in await desk.events_of(chat["id"])
    assert (alice.im_user, 5) in desk.im.logged_out
    tokens = await desk.sql(
        "SELECT count(*) AS n FROM refresh_tokens WHERE staff_id = $1 AND revoked_at IS NULL",
        alice.staff_id,
    )
    assert tokens[0]["n"] == 0
    assert refresh_cookie is not None
    [audit] = await desk.sql("SELECT detail FROM audit_logs WHERE action = 'staff.update'")
    assert '"status": "disabled"' in audit["detail"]

    # 重新启用：可以登录，会话由其他在线坐席接待。
    bob = await desk.agent("bob")
    assert (await desk.session_of(visitor))["assignee_id"] == bob.staff_id
    enabled = await _patch(desk, alice.staff_id, desk.admin, status="active")
    assert enabled.status_code == 200
    assert (await _login(desk.client, "alice", STAFF_PASSWORD)).status_code == 200


async def test_guards_on_staff_changes(desk: Desk, app: FastAPI) -> None:
    [admin] = await desk.sql("SELECT id FROM staff WHERE username = 'admin'")
    own = await _patch(desk, admin["id"], desk.admin, status="disabled")
    assert own.status_code == 422
    # 唯一的租户管理员不能去掉管理员角色。
    demote = await _patch(desk, admin["id"], desk.admin, role_codes=["agent"])
    assert demote.status_code == 409
    second = await desk.agent("root2", roles=["tenant_admin"], online=False)
    demote = await _patch(desk, admin["id"], second.headers, role_codes=["agent"])
    assert demote.status_code == 200, demote.text
    assert demote.json()["roles"] == ["agent"]
    # 现在 root2 是唯一的管理员：原管理员（只剩坐席权限）不能再管理员工。
    agent_try = await _patch(desk, second.staff_id, desk.admin, status="disabled")
    assert agent_try.status_code == 403
    desk.admin_token = second.token

    # 主管加上员工管理权限后，也不能管理或提拔到权限比自己高的员工。
    await desk.client.post(
        "/api/v1/roles",
        headers=second.headers,
        json={
            "code": "team_lead",
            "name": "组长",
            "permissions": ["dashboard:view", "staff:read", "staff:manage", "customer:read"],
        },
    )
    lead = await desk.agent("lead", roles=["team_lead"], online=False)
    carol = await desk.agent("carol", online=False)
    promote = await _patch(desk, carol.staff_id, lead.headers, role_codes=["tenant_admin"])
    assert promote.status_code == 403
    manage_admin = await _patch(desk, second.staff_id, lead.headers, display_name="改名")
    assert manage_admin.status_code == 403
    missing = await _patch(desk, carol.staff_id, second.headers, role_codes=["no_such_role"])
    assert missing.status_code == 422

    # 坐席额度用完后不能再启用停用的员工（启用中的：admin、root2、lead）。
    await _patch(desk, carol.staff_id, second.headers, status="disabled")
    await create_platform_admin(app)
    ops = bearer(await platform_login(desk.client))
    limits = await desk.client.put(
        f"/platform/v1/tenants/{desk.tenant_id}/overrides",
        headers=ops,
        json={"limits": {"seats": 3}},
    )
    assert limits.status_code == 200, limits.text
    full = await _patch(desk, carol.staff_id, second.headers, status="active")
    assert full.status_code == 409
    assert full.json()["error"]["code"] == "plan_limit"


async def test_reset_and_change_passwords(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    reset = await desk.client.post(
        f"/api/v1/staff/{alice.staff_id}/password",
        headers=desk.admin,
        json={"password": "brand-new-pass", "must_change": False},
    )
    assert reset.status_code == 200, reset.text
    assert reset.json() == {"temporary_password": None, "must_change_password": False}
    assert (await _login(desk.client, "alice", STAFF_PASSWORD)).status_code == 401
    token = await login_token(desk.client, "acme", "alice", "brand-new-pass")

    wrong = await desk.client.post(
        "/api/v1/me/password",
        headers=bearer(token),
        json={"current_password": "nope", "new_password": "another-pass-1"},
    )
    assert wrong.status_code == 422
    same = await desk.client.post(
        "/api/v1/me/password",
        headers=bearer(token),
        json={"current_password": "brand-new-pass", "new_password": "brand-new-pass"},
    )
    assert same.status_code == 422
    changed = await desk.client.post(
        "/api/v1/me/password",
        headers=bearer(token),
        json={"current_password": "brand-new-pass", "new_password": "another-pass-1"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["access_token"]
    assert "edp_refresh" in changed.headers.get("set-cookie", "")
    # 其他登录的刷新令牌失效，新的令牌可以刷新。
    active = await desk.sql(
        "SELECT count(*) AS n FROM refresh_tokens WHERE staff_id = $1 AND revoked_at IS NULL",
        alice.staff_id,
    )
    assert active[0]["n"] == 1
    refreshed = await desk.client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert (await _login(desk.client, "alice", "another-pass-1")).status_code == 200
    actions = [
        r["action"]
        for r in await desk.sql(
            "SELECT action FROM audit_logs WHERE action LIKE 'staff.%' ORDER BY created_at"
        )
    ]
    assert actions[-2:] == ["staff.reset_password", "staff.change_password"]
    # 员工不能重置别人的密码（修改密码之后之前的令牌失效，用换发的令牌）。
    denied = await desk.client.post(
        f"/api/v1/staff/{alice.staff_id}/password",
        headers=bearer(changed.json()["access_token"]),
        json={"password": "x" * 10},
    )
    assert denied.status_code == 403


async def test_custom_roles(desk: Desk) -> None:
    catalog = await desk.client.get("/api/v1/permissions", headers=desk.admin)
    assert {p["code"] for p in catalog.json()["items"]} == {str(p) for p in Permission}

    created = await desk.client.post(
        "/api/v1/roles",
        headers=desk.admin,
        json={
            "code": "quality",
            "name": "质检",
            "permissions": ["report:view", "session:read_all"],
        },
    )
    assert created.status_code == 201, created.text
    role = created.json()
    assert (role["is_system"], role["members"]) == (False, 0)
    duplicate = await desk.client.post(
        "/api/v1/roles",
        headers=desk.admin,
        json={"code": "quality", "name": "重复", "permissions": ["report:view"]},
    )
    assert duplicate.status_code == 409
    bad = await desk.client.post(
        "/api/v1/roles",
        headers=desk.admin,
        json={"code": "x", "name": "x", "permissions": ["no:such"]},
    )
    assert bad.status_code == 422

    qa = await desk.agent("qa1", roles=["quality"], online=False)
    me = (await desk.client.get("/api/v1/me", headers=qa.headers)).json()
    assert set(me["permissions"]) == {"report:view", "session:read_all"}
    updated = await desk.client.patch(
        f"/api/v1/roles/{role['id']}",
        headers=desk.admin,
        json={"permissions": ["report:view"], "name": "质检员"},
    )
    assert updated.status_code == 200, updated.text
    assert (updated.json()["name"], updated.json()["members"]) == ("质检员", 1)
    me = (await desk.client.get("/api/v1/me", headers=qa.headers)).json()
    assert me["permissions"] == ["report:view"]

    in_use = await desk.client.delete(f"/api/v1/roles/{role['id']}", headers=desk.admin)
    assert in_use.status_code == 409
    await _patch(desk, qa.staff_id, desk.admin, role_codes=["agent"])
    deleted = await desk.client.delete(f"/api/v1/roles/{role['id']}", headers=desk.admin)
    assert deleted.status_code == 204

    roles = (await desk.client.get("/api/v1/roles", headers=desk.admin)).json()["items"]
    system = next(r for r in roles if r["code"] == "agent")
    assert system["members"] == 1
    frozen = await desk.client.patch(
        f"/api/v1/roles/{system['id']}", headers=desk.admin, json={"name": "改名"}
    )
    assert frozen.status_code == 409

    # 有员工管理权限的主管不能创建超出自己权限的角色。
    await desk.client.post(
        "/api/v1/roles",
        headers=desk.admin,
        json={"code": "hr", "name": "人事", "permissions": ["staff:read", "staff:manage"]},
    )
    hr = await desk.agent("hr1", roles=["hr"], online=False)
    escalate = await desk.client.post(
        "/api/v1/roles",
        headers=hr.headers,
        json={"code": "boss", "name": "老板", "permissions": ["tenant:manage"]},
    )
    assert escalate.status_code == 403
    assert await login_token(desk.client, "acme", "admin", ADMIN_PASSWORD)
