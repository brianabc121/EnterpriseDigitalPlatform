"""重置密码（设计文档 §38）：企业为全部角色的员工重置、平台为企业拥有者（管理员账号）重置、
登录后先设置新密码、重置和修改密码后之前的登录立即失效。"""

import string
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.core.config import Settings
from app.core.permissions import DEFAULT_ROLES, TENANT_ADMIN_ROLE
from app.core.security import (
    decode_access_token,
    encode_access_token,
    generate_password,
    password_stamp,
)
from app.modules.iam.deps import (
    get_access_claims,
    get_current_principal,
    get_principal_for_password_change,
)
from tests.desk import Desk
from tests.factories import (
    ADMIN_PASSWORD,
    STAFF_PASSWORD,
    bearer,
    create_platform_admin,
    create_staff,
    platform_login,
    provision,
)
from tests.factories import login as login_token
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls

REASON = "企业负责人来电，核对营业执照后申请重置"
SECRET = "k" * 32


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


async def _reset(desk: Desk, staff_id: Any, headers: dict[str, str], **body: Any) -> httpx.Response:
    return await desk.client.post(f"/api/v1/staff/{staff_id}/password", headers=headers, json=body)


async def _change(token: str, client: httpx.AsyncClient, current: str, new: str) -> httpx.Response:
    return await client.post(
        "/api/v1/me/password",
        headers=bearer(token),
        json={"current_password": current, "new_password": new},
    )


def _staff_row(listing: httpx.Response, username: str) -> dict[str, Any]:
    [row] = [s for s in listing.json()["items"] if s["username"] == username]
    return row


def test_generated_passwords_are_readable_and_mixed() -> None:
    allowed = set(string.ascii_letters + string.digits) - set("0O1lI")
    for _ in range(200):
        password = generate_password()
        assert len(password) == 12
        assert set(password) <= allowed
        assert any(c.isupper() for c in password)
        assert any(c.islower() for c in password)
        assert any(c.isdigit() for c in password)


def test_access_tokens_carry_the_password_stamp() -> None:
    staff_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    assert password_stamp(None) is None
    old = encode_access_token(staff_id=staff_id, tenant_id=tenant_id, secret=SECRET, ttl_seconds=60)
    assert decode_access_token(old, secret=SECRET).password_stamp is None
    stamp = password_stamp(datetime(2026, 10, 3, 8, 30, 15, 123456, tzinfo=UTC))
    assert stamp == 1_791_016_215_123_456
    token = encode_access_token(
        staff_id=staff_id, tenant_id=tenant_id, secret=SECRET, ttl_seconds=60, password_stamp=stamp
    )
    assert decode_access_token(token, secret=SECRET).password_stamp == stamp


def _routes(routes: list[Any]) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _routes(route.original_router.routes)


def _calls(route: APIRoute) -> set[Any]:
    found, stack = set(), [route.dependant]
    while stack:
        dependant = stack.pop()
        if dependant.call is not None:
            found.add(dependant.call)
        stack.extend(dependant.dependencies)
    return found


def test_every_staff_route_checks_for_a_forced_password_change(app: FastAPI) -> None:
    """要先设置新密码时，除了查看自己的信息和修改密码，员工的接口一律拒绝（§38.5）。"""
    staff_routes = [r for r in _routes(app.routes) if get_access_claims in _calls(r)]
    assert len(staff_routes) > 400
    unchecked = {
        (method, route.path)
        for route in staff_routes
        if get_current_principal not in _calls(route)
        for method in route.methods
    }
    assert unchecked == {("GET", "/api/v1/me"), ("POST", "/api/v1/me/password")}
    for route in staff_routes:
        if (route.path, *route.methods) in {("/api/v1/me", "GET"), ("/api/v1/me/password", "POST")}:
            assert get_principal_for_password_change in _calls(route)


async def test_reset_generates_a_temporary_password_and_forces_a_change(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    reset = await _reset(desk, alice.staff_id, desk.admin)
    assert reset.status_code == 200, reset.text
    temporary = reset.json()["temporary_password"]
    assert reset.json()["must_change_password"] is True
    assert isinstance(temporary, str) and len(temporary) == 12

    # 之前的登录立即失效（不用等访问令牌过期），IM 也下线。
    assert (await desk.client.get("/api/v1/me", headers=alice.headers)).status_code == 401
    assert any(user == alice.im_user for user, _ in desk.im.logged_out)
    listing = await desk.client.get("/api/v1/staff", headers=desk.admin)
    row = _staff_row(listing, "alice")
    assert row["must_change_password"] is True
    assert row["password_changed_at"] is not None

    assert (await _login(desk.client, "alice", STAFF_PASSWORD)).status_code == 401
    token = await login_token(desk.client, "acme", "alice", temporary)
    me = await desk.client.get("/api/v1/me", headers=bearer(token))
    assert me.status_code == 200, me.text
    assert me.json()["must_change_password"] is True
    info = me.json()["password_reset"]
    assert (info["by"], info["operator"], info["reason"]) == ("staff", "管理员", None)

    # 设置新密码之前，其他接口一律 403。
    blocked = await desk.client.get("/api/v1/customers", headers=bearer(token))
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "password_change_required"
    assert (await _change(token, desk.client, temporary, temporary)).status_code == 422
    changed = await _change(token, desk.client, temporary, "alice-own-pass")
    assert changed.status_code == 200, changed.text
    fresh = changed.json()["access_token"]

    me = await desk.client.get("/api/v1/me", headers=bearer(fresh))
    assert (me.json()["must_change_password"], me.json()["password_reset"]) == (False, None)
    assert (await desk.client.get("/api/v1/customers", headers=bearer(fresh))).status_code == 200
    # 修改密码之前的令牌（用临时密码登录的）失效。
    assert (await desk.client.get("/api/v1/me", headers=bearer(token))).status_code == 401
    listing = await desk.client.get("/api/v1/staff", headers=desk.admin)
    assert _staff_row(listing, "alice")["must_change_password"] is False

    [audit] = await desk.sql(
        "SELECT actor_type, detail FROM audit_logs WHERE action = 'staff.reset_password'"
    )
    assert audit["actor_type"] == "staff"
    assert '"generated": true' in audit["detail"] and '"must_change": true' in audit["detail"]


async def test_manual_password_without_a_forced_change(desk: Desk) -> None:
    bob = await desk.agent("bob", online=False)
    reset = await _reset(desk, bob.staff_id, desk.admin, password="bob-new-pass", must_change=False)
    assert reset.json() == {"temporary_password": None, "must_change_password": False}
    token = await login_token(desk.client, "acme", "bob", "bob-new-pass")
    assert (await desk.client.get("/api/v1/customers", headers=bearer(token))).status_code == 200
    short = await _reset(desk, bob.staff_id, desk.admin, password="short")
    assert short.status_code == 422


async def test_every_role_can_be_reset_but_not_yourself_or_higher_staff(desk: Desk) -> None:
    # 全部角色：企业所有者可以重置每一个系统角色的员工（包括以前分配过企业所有者角色的员工，
    # 这个角色现在只能由平台创建，§39.5）。
    for spec in DEFAULT_ROLES:
        username = f"user-{spec.code.replace('_', '-')}"
        if spec.code == TENANT_ADMIN_ROLE:
            staff_id = str((await desk.extra_admin(username)).staff_id)
        else:
            staff_id = await create_staff(desk.client, desk.admin_token, username, [spec.code])
        reset = await _reset(desk, staff_id, desk.admin)
        assert reset.status_code == 200, (spec.code, reset.text)
        assert reset.json()["temporary_password"]

    me = (await desk.client.get("/api/v1/me", headers=desk.admin)).json()
    own = await _reset(desk, me["id"], desk.admin)
    assert own.status_code == 422
    assert "修改密码" in own.json()["error"]["message"]

    # 只有管理员工权限的自定义角色：可以重置权限不高于自己的员工，不能重置管理员、坐席。
    for code, permissions in [("hr", ["staff:read", "staff:manage"]), ("viewer", ["staff:read"])]:
        role = await desk.client.post(
            "/api/v1/roles",
            headers=desk.admin,
            json={"code": code, "name": code, "permissions": permissions},
        )
        assert role.status_code == 201, role.text
    await create_staff(desk.client, desk.admin_token, "hrm", ["hr"])
    hr = bearer(await login_token(desk.client, "acme", "hrm", STAFF_PASSWORD))
    viewer_id = await create_staff(desk.client, desk.admin_token, "viewer", ["viewer"])
    agent_id = await create_staff(desk.client, desk.admin_token, "carol", ["agent"])
    assert (await _reset(desk, me["id"], hr)).status_code == 403
    assert (await _reset(desk, agent_id, hr)).status_code == 403
    assert (await _reset(desk, viewer_id, hr)).status_code == 200
    # 没有管理员工权限的员工不能重置。
    carol = bearer(await login_token(desk.client, "acme", "carol", STAFF_PASSWORD))
    assert (await _reset(desk, viewer_id, carol)).status_code == 403
    missing = await _reset(desk, uuid.uuid4(), desk.admin)
    assert missing.status_code == 404


async def test_platform_resets_the_owner_password(
    app: FastAPI, desk: Desk, fake_im: FakeOpenIM
) -> None:
    client = desk.client
    owner = (await client.get("/api/v1/me", headers=desk.admin)).json()
    # 企业里另一个有企业所有者角色的员工（以前分配的，§39.5）：平台重置后收到提醒。
    extra = await desk.extra_admin("root2")
    second_id, second = str(extra.staff_id), extra.token
    agent_id = await create_staff(client, desk.admin_token, "alice", ["agent"])
    await create_platform_admin(app)
    ops = bearer(await platform_login(client))
    base = f"/platform/v1/tenants/{desk.tenant_id}/admins"

    assert (await client.get(base)).status_code == 401
    assert (await client.get(base, headers=desk.admin)).status_code == 401
    listing = await client.get(base, headers=ops)
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert [(a["username"], a["owner"]) for a in items] == [("admin", True), ("root2", False)]
    assert items[0]["last_login_at"] is not None
    assert items[0]["must_change_password"] is False

    # 只能重置管理员账号；原因必填。
    body = {"reason": REASON}
    assert (
        await client.post(f"{base}/{agent_id}/password", headers=ops, json=body)
    ).status_code == 404
    blank = await client.post(f"{base}/{owner['id']}/password", headers=ops, json={"reason": " x "})
    assert blank.status_code == 422
    assert (await client.post(f"{base}/{owner['id']}/password", json=body)).status_code == 401

    reset = await client.post(f"{base}/{owner['id']}/password", headers=ops, json=body)
    assert reset.status_code == 200, reset.text
    temporary = reset.json()["temporary_password"]
    assert reset.json()["must_change_password"] is True and len(temporary) == 12
    assert (await client.get("/api/v1/me", headers=desk.admin)).status_code == 401
    assert any(user.endswith(uuid.UUID(owner["id"]).hex) for user, _ in fake_im.logged_out)

    # 留痕：企业的操作日志（平台运维，带原因），其他管理员收到提醒。
    [audit] = await desk.sql(
        "SELECT actor_type, resource_id, detail FROM audit_logs"
        " WHERE action = 'staff.reset_password' AND tenant_id = $1",
        desk.tenant_id,
    )
    assert (audit["actor_type"], audit["resource_id"]) == ("platform", owner["id"])
    assert REASON in audit["detail"]
    notices = await desk.sql(
        "SELECT staff_id, kind, title, body FROM staff_notifications WHERE kind = 'security'"
    )
    assert [n["staff_id"] for n in notices] == [uuid.UUID(second_id)]
    assert "admin" in notices[0]["title"] and REASON in notices[0]["body"]
    inbox = await client.get("/api/v1/notifications", headers=bearer(second))
    assert any("重置了管理员" in n["title"] for n in inbox.json()["items"])

    # 企业拥有者用临时密码登录后先设置新密码。
    token = await login_token(client, "acme", "admin", temporary)
    me = (await client.get("/api/v1/me", headers=bearer(token))).json()
    assert me["must_change_password"] is True
    info = me["password_reset"]
    assert (info["by"], info["operator"], info["reason"]) == ("platform", None, REASON)
    assert (await client.get("/api/v1/staff", headers=bearer(token))).status_code == 403
    changed = await _change(token, client, temporary, "owner-own-pass")
    assert changed.status_code == 200, changed.text
    staff = await client.get("/api/v1/staff", headers=bearer(changed.json()["access_token"]))
    assert staff.status_code == 200

    after = (await client.get(base, headers=ops)).json()["items"]
    assert after[0]["must_change_password"] is False
    assert after[0]["password_changed_at"] is not None
    assert (await _login(client, "admin", ADMIN_PASSWORD)).status_code == 401

    # 平台的审计日志里有运营人员的姓名。
    logs = await client.get(
        "/platform/v1/audit-logs", headers=ops, params={"action": "staff.reset_password"}
    )
    assert [(e["actor_type"], e["actor_name"]) for e in logs.json()["items"]] == [
        ("platform", "运营")
    ]


async def test_platform_reset_is_scoped_to_the_tenant(app: FastAPI, desk: Desk) -> None:
    client = desk.client
    other = await provision(app, "globex")
    other_admin = await login_token(client, "globex")
    other_id = (await client.get("/api/v1/me", headers=bearer(other_admin))).json()["id"]
    await create_platform_admin(app)
    ops = bearer(await platform_login(client))
    body = {"reason": REASON}

    crossed = await client.post(
        f"/platform/v1/tenants/{desk.tenant_id}/admins/{other_id}/password", headers=ops, json=body
    )
    assert crossed.status_code == 404
    assert (await client.get("/api/v1/me", headers=bearer(other_admin))).status_code == 200
    missing = await client.get(f"/platform/v1/tenants/{uuid.uuid4()}/admins", headers=ops)
    assert missing.status_code == 404

    await desk.sql("UPDATE tenants SET status = 'closed' WHERE id = $1", other)
    closed = await client.post(
        f"/platform/v1/tenants/{other}/admins/{other_id}/password", headers=ops, json=body
    )
    assert closed.status_code == 409
