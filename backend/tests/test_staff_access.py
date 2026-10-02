"""按员工设置的页面和权限（设计文档 §31）：新建、编辑员工时自定义看到的页面、登录后打开的页面和
功能权限；只记录和角色的差别；不能越权；租户管理员不能单独调整；有效权限用于所有判断和通知对象。"""

import json
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.permissions import Permission
from app.modules.kb.distribution import audience
from app.modules.todos.assign import staff_with
from tests.desk import Desk
from tests.factories import STAFF_PASSWORD, bearer, login
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import call

AGENT = {
    "dashboard:view", "workbench:use", "customer:read", "customer:create", "kb:read",
    "session:transfer", "todo:read", "todo:handle", "order:read", "order:create",
    "order:review", "order:payment", "contract:use", "task:use", "assistant:use",
}  # fmt: skip
AGENT_MENUS = [
    "dashboard", "workbench", "sessions", "todos", "orders", "contracts", "tasks", "customers",
    "knowledge", "assistant",
]  # fmt: skip


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


async def defaults(desk: Desk, headers: dict[str, str], *roles: str, status: int = 200) -> Any:
    response = await desk.client.get(
        "/api/v1/staff/access-defaults", headers=headers, params={"role_codes": list(roles)}
    )
    assert response.status_code == status, response.text
    return response.json()


async def create(
    desk: Desk,
    headers: dict[str, str],
    username: str,
    roles: list[str],
    access: dict[str, Any] | None,
    status: int = 201,
) -> Any:
    return await call(
        desk, headers, "POST", "/api/v1/staff", status, username=username,
        display_name=username.capitalize(), password=STAFF_PASSWORD, role_codes=roles,
        access=access,
    )  # fmt: skip


async def staff_login(desk: Desk, username: str) -> dict[str, str]:
    return bearer(await login(desk.client, "acme", username, STAFF_PASSWORD))


async def me(desk: Desk, headers: dict[str, str]) -> Any:
    return await call(desk, headers, "GET", "/api/v1/me")


async def listed(desk: Desk, username: str) -> Any:
    items = (await call(desk, desk.admin, "GET", "/api/v1/staff"))["items"]
    return next(item for item in items if item["username"] == username)


async def test_create_staff_with_own_pages_landing_page_and_permissions(desk: Desk) -> None:
    # 对话框的起点：角色给的页面和权限。
    start = await defaults(desk, desk.admin, "agent")
    assert start == {
        "profiles": ["agent"],
        "menus": AGENT_MENUS,
        "permissions": sorted(AGENT),
        "adjustable": True,
    }
    both = await defaults(desk, desk.admin, "agent", "keeper")
    assert both["profiles"] == ["agent", "keeper"]
    assert "warehouse" in both["menus"]
    assert {"inventory:manage", "warehouse:confirm"} <= set(both["permissions"])
    admin = await defaults(desk, desk.admin, "tenant_admin")
    assert admin["adjustable"] is False
    assert admin["permissions"] == sorted(str(p) for p in Permission)
    await defaults(desk, desk.admin, "no_such_role", status=422)

    # 客服小王：不看知识库和客户待办，多给"查看应收账款"，登录后打开订单。
    chosen = (AGENT - {"kb:read", "todo:read", "todo:handle"}) | {"finance:view"}
    access = {
        "menus": ["orders", "receivables", "customers", "workbench", "reports"],
        "home_menu": "orders",
        "permissions": sorted(chosen),
    }
    created = await create(desk, desk.admin, "xiao", ["agent"], access)
    assert created["access"] == {
        # 按菜单的先后保存。
        "menus": ["workbench", "orders", "receivables", "customers", "reports"],
        "home_menu": "orders",
        "extra_permissions": ["finance:view"],
        "revoked_permissions": ["kb:read", "todo:handle", "todo:read"],
    }
    assert created["permissions"] == sorted(chosen)
    assert (await listed(desk, "xiao"))["access"] == created["access"]

    xiao = await staff_login(desk, "xiao")
    profile = await me(desk, xiao)
    assert profile["permissions"] == sorted(chosen)
    # 没有"查看报表"的权限，"报表"不显示；首页的内容仍按岗位。
    assert profile["console"] == {
        "profiles": ["agent"],
        "menus": ["workbench", "orders", "receivables", "customers"],
        "home": "orders",
    }
    # 去掉的权限接口里也没有了，多给的可以用。
    assert (await desk.client.get("/api/v1/kb/items", headers=xiao)).status_code == 403
    assert (await desk.client.get("/api/v1/todos", headers=xiao)).status_code == 403
    await call(desk, xiao, "GET", "/api/v1/finance/receivables/summary")

    [audit] = await desk.sql(
        "SELECT detail FROM audit_logs WHERE action = 'staff.create' AND resource_id = $1",
        created["id"],
    )
    detail = json.loads(audit["detail"])
    assert detail["access"]["extra_permissions"] == ["finance:view"]
    assert detail["access"]["home_menu"] == "orders"

    # 按角色新建的员工没有自定义，登录后打开第一个菜单。
    plain = await create(desk, desk.admin, "plain", ["agent"], None)
    assert (plain["access"], set(plain["permissions"])) == (None, AGENT)
    assert (await me(desk, await staff_login(desk, "plain")))["console"]["home"] is None

    # 校验：登录后打开的页面必须是勾选的页面之一；页面至少一个；管理员不能单独调整。
    wrong_home = {**access, "home_menu": "dashboard"}
    await create(desk, desk.admin, "bad1", ["agent"], wrong_home, status=422)
    await create(desk, desk.admin, "bad2", ["agent"], {**access, "menus": []}, status=422)
    await create(desk, desk.admin, "bad3", ["tenant_admin"], access, status=422)
    bogus = {**access, "menus": ["orders", "nowhere"]}
    await create(desk, desk.admin, "bad4", ["agent"], bogus, status=422)


async def test_roles_stay_the_base_and_settings_can_be_reset(desk: Desk) -> None:
    helper = await call(
        desk, desk.admin, "POST", "/api/v1/roles", 201, code="helper", name="助理",
        permissions=["dashboard:view", "task:use", "customer:read"],
    )  # fmt: skip
    access = {
        "menus": ["dashboard", "tasks", "customers", "orders"],
        "home_menu": "customers",
        "permissions": ["dashboard:view", "customer:read", "order:read"],
    }
    lin = await create(desk, desk.admin, "lin", ["helper"], access)
    assert (lin["access"]["extra_permissions"], lin["access"]["revoked_permissions"]) == (
        ["order:read"],
        ["task:use"],
    )
    headers = await staff_login(desk, "lin")
    assert (await me(desk, headers))["console"]["menus"] == ["dashboard", "orders", "customers"]

    # 角色的权限变了，员工跟着变，去掉的仍然去掉。
    await call(
        desk, desk.admin, "PATCH", f"/api/v1/roles/{helper['id']}",
        permissions=["dashboard:view", "task:use", "customer:read", "assistant:use"],
    )  # fmt: skip
    assert set((await me(desk, headers))["permissions"]) == {
        "dashboard:view", "customer:read", "order:read", "assistant:use",
    }  # fmt: skip

    # 换角色不传 access：保留原来的设置。
    changed = await call(
        desk, desk.admin, "PATCH", f"/api/v1/staff/{lin['id']}", role_codes=["helper", "keeper"]
    )
    assert changed["access"]["revoked_permissions"] == ["task:use"]
    assert "task:use" not in changed["permissions"]
    assert {"inventory:manage", "warehouse:confirm"} <= set(changed["permissions"])
    # 页面仍按自己的（仓管岗位的"仓库"没有勾，不显示）；没有选岗位的自定义角色按主管。
    console = (await me(desk, headers))["console"]
    assert (console["profiles"], console["menus"]) == (
        ["supervisor", "keeper"],
        ["dashboard", "orders", "customers"],
    )

    # 登录后打开的页面没有权限时为空，打开第一个菜单。
    narrowed = {**access, "permissions": ["dashboard:view", "order:read"]}
    await call(desk, desk.admin, "PATCH", f"/api/v1/staff/{lin['id']}", access=narrowed)
    assert (await me(desk, headers))["console"] == {
        "profiles": ["supervisor", "keeper"],
        "menus": ["dashboard", "orders"],
        "home": None,
    }

    # 恢复按角色。
    reset = await call(desk, desk.admin, "PATCH", f"/api/v1/staff/{lin['id']}", access=None)
    assert reset["access"] is None
    console = (await me(desk, headers))["console"]
    assert console["home"] is None
    assert "warehouse" in console["menus"]
    assert "task:use" in (await me(desk, headers))["permissions"]
    updates = [
        json.loads(r["detail"])
        for r in await desk.sql(
            "SELECT detail FROM audit_logs WHERE action = 'staff.update' AND resource_id = $1"
            " ORDER BY created_at",
            lin["id"],
        )
    ]
    assert updates[-1]["access"] is None
    assert updates[-2]["access"]["menus"] == ["dashboard", "orders", "tasks", "customers"]

    # 改成租户管理员：清掉原来的设置；管理员不能单独调整。
    await call(desk, desk.admin, "PATCH", f"/api/v1/staff/{lin['id']}", access=access)
    promoted = await call(
        desk, desk.admin, "PATCH", f"/api/v1/staff/{lin['id']}", role_codes=["tenant_admin"]
    )
    assert promoted["access"] is None
    assert set(promoted["permissions"]) == {str(p) for p in Permission}
    await call(desk, desk.admin, "PATCH", f"/api/v1/staff/{lin['id']}", 422, access=access)
    [row] = await desk.sql(
        "SELECT menus, extra_permissions, revoked_permissions FROM staff WHERE id = $1",
        promoted["id"],
    )
    assert (row["menus"], row["extra_permissions"], row["revoked_permissions"]) == (None, [], [])


async def test_cannot_grant_beyond_own_permissions(desk: Desk) -> None:
    lead_perms = ["dashboard:view", "staff:read", "staff:manage", "customer:read", "task:use"]
    await call(
        desk, desk.admin, "POST", "/api/v1/roles", 201, code="team_lead", name="组长",
        permissions=lead_perms,
    )  # fmt: skip
    await call(
        desk, desk.admin, "POST", "/api/v1/roles", 201, code="clerk", name="文员",
        permissions=["dashboard:view", "customer:read", "settings:manage"],
    )  # fmt: skip
    lead = await desk.agent("lead", roles=["team_lead"], online=False)
    await call(
        desk, desk.admin, "POST", "/api/v1/roles", 201, code="viewer", name="查看",
        permissions=["dashboard:view"],
    )  # fmt: skip
    page = {"menus": ["dashboard", "customers"], "home_menu": None}

    # 多给的权限超出自己的：403；在自己的范围内：可以。
    await create(
        desk, lead.headers, "sun1", ["viewer"],
        {**page, "permissions": ["dashboard:view", "report:view"]}, status=403,
    )  # fmt: skip
    ok = await create(
        desk, lead.headers, "sun2", ["viewer"],
        {**page, "permissions": ["dashboard:view", "task:use"]},
    )  # fmt: skip
    assert ok["access"]["extra_permissions"] == ["task:use"]
    # 起点接口要有员工管理权限。
    await defaults(desk, lead.headers, "viewer")
    viewer = await desk.agent("view1", roles=["viewer"], online=False)
    await defaults(desk, viewer.headers, "viewer", status=403)

    # 管理员给员工多给了组长没有的权限：组长不能再管理这个员工。
    strong = await create(
        desk, desk.admin, "sun3", ["viewer"],
        {**page, "permissions": ["dashboard:view", "report:view"]},
    )  # fmt: skip
    await call(desk, lead.headers, "PATCH", f"/api/v1/staff/{strong['id']}", 403, display_name="改")

    # 管理员去掉了"设置"权限的文员：组长可以管理，但不能把去掉的权限恢复回来。
    clerk = await create(
        desk, desk.admin, "sun4", ["clerk"],
        {**page, "permissions": ["dashboard:view", "customer:read"]},
    )  # fmt: skip
    renamed = await call(
        desk, lead.headers, "PATCH", f"/api/v1/staff/{clerk['id']}", display_name="文员小李"
    )
    assert renamed["display_name"] == "文员小李"
    await call(desk, lead.headers, "PATCH", f"/api/v1/staff/{clerk['id']}", 403, access=None)
    await call(
        desk, lead.headers, "PATCH", f"/api/v1/staff/{clerk['id']}", 403,
        access={**page, "permissions": ["dashboard:view", "settings:manage"]},
    )  # fmt: skip
    await call(
        desk, lead.headers, "PATCH", f"/api/v1/staff/{clerk['id']}", 403, role_codes=["clerk"]
    )
    [row] = await desk.sql("SELECT revoked_permissions FROM staff WHERE id = $1", clerk["id"])
    assert row["revoked_permissions"] == ["settings:manage"]


async def test_notifications_and_checks_use_effective_permissions(desk: Desk) -> None:
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    # 去掉主管的"分派待办"；给客服多给"分派待办"、去掉"接待"。
    supervisor = set((await defaults(desk, desk.admin, "supervisor"))["permissions"])
    await call(
        desk, desk.admin, "PATCH", f"/api/v1/staff/{boss.staff_id}",
        access={"menus": ["dashboard"], "permissions": sorted(supervisor - {"todo:assign"})},
    )  # fmt: skip
    mei = await create(
        desk, desk.admin, "mei", ["agent"],
        {"menus": ["todos"], "permissions": sorted((AGENT - {"workbench:use"}) | {"todo:assign"})},
    )  # fmt: skip
    await desk.agent("plain", online=False)

    async with desk.ctx.db.tenant_session(desk.tenant_id) as session:
        assigners = await staff_with(session, Permission.TODO_ASSIGN)
        readers = [s.username for s in await audience(session)]
    [admin] = await desk.sql("SELECT id FROM staff WHERE username = 'admin'")
    assert assigners == [admin["id"], uuid.UUID(mei["id"])]
    assert "mei" not in readers
    assert {"admin", "boss", "plain"} <= set(readers)

    # 邀请协助：去掉了接待权限的员工不能被邀请。
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == alice.staff_id
    bob = await desk.agent("bob")
    path = f"/api/v1/sessions/{chat['id']}/assists"
    await call(desk, alice.headers, "POST", path, 422, staff_id=mei["id"])
    await call(desk, alice.headers, "POST", path, staff_id=str(bob.staff_id))
