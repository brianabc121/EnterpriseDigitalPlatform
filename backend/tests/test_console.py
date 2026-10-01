"""按岗位的控制台（设计文档 §25.15）：员工的岗位和菜单、"仓管"角色、自定义角色的岗位、企业调整
每个岗位的菜单。"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from tests.desk import Desk
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_orders import call
from tests.test_warehouse import material

ALL = [
    "dashboard", "workbench", "sessions", "todos", "orders", "products", "production",
    "warehouse", "tasks", "customers", "knowledge", "ai", "assistant", "staff", "reports",
    "broadcasts", "wecom", "audit", "settings",
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


async def console(desk: Desk, headers: dict[str, str]) -> dict[str, Any]:
    me = await call(desk, headers, "GET", "/api/v1/me")
    result: dict[str, Any] = me["console"]
    return result


async def test_each_role_has_its_own_console(desk: Desk) -> None:
    admin = await console(desk, desk.admin)
    assert admin == {"profiles": ["admin"], "menus": ALL}
    agent = await desk.agent("mei", roles=["agent"], online=False)
    assert await console(desk, agent.headers) == {
        "profiles": ["agent"],
        "menus": [
            "dashboard",
            "workbench",
            "sessions",
            "todos",
            "orders",
            "tasks",
            "customers",
            "knowledge",
            "assistant",
        ],
    }
    boss = await desk.agent("boss", roles=["supervisor"], online=False)
    # 主管：全部菜单，但没有权限的（加工、AI 接待、员工、企业微信、操作日志、设置）不显示。
    assert await console(desk, boss.headers) == {
        "profiles": ["supervisor"],
        "menus": [
            "dashboard", "workbench", "sessions", "todos", "orders", "products", "warehouse",
            "tasks", "customers", "knowledge", "assistant", "reports", "broadcasts",
        ],
    }  # fmt: skip
    kate = await desk.agent("kate", roles=["knowledge_manager"], online=False)
    assert await console(desk, kate.headers) == {
        "profiles": ["knowledge"],
        "menus": ["dashboard", "tasks", "knowledge", "assistant"],
    }

    # 工人：没有仓管角色的员工时，最早创建的工人担任仓管（另外获得确认单据和库存的权限）。
    wang = await desk.agent("wang", roles=["worker"], online=False)
    assert await console(desk, wang.headers) == {
        "profiles": ["keeper", "worker"],
        "menus": ["production", "warehouse", "tasks", "assistant"],
    }
    laoli = await desk.agent("laoli", roles=["worker"], online=False)
    assert await console(desk, laoli.headers) == {
        "profiles": ["worker"],
        "menus": ["production", "tasks", "assistant"],
    }

    # 客服兼仓管：菜单合在一起。
    both = await desk.agent("chen", roles=["agent", "keeper"], online=False)
    assert await console(desk, both.headers) == {
        "profiles": ["agent", "keeper"],
        "menus": [
            "dashboard", "workbench", "sessions", "todos", "orders", "warehouse", "tasks",
            "customers", "knowledge", "assistant",
        ],
    }  # fmt: skip


async def test_keeper_role_confirms_documents_and_replaces_the_worker_fallback(desk: Desk) -> None:
    wang = await desk.agent("wang", roles=["worker"], online=False)
    cang = await desk.agent("cang", roles=["keeper"], online=False)
    cui = await desk.agent("cui", roles=["keeper"], online=False)
    boss = await desk.agent("boss", roles=["supervisor"], online=False)

    # 有"仓管"角色的员工时，他们都是仓管，最早创建的工人不再担任。
    settings = await call(desk, desk.admin, "GET", "/api/v1/warehouse/settings")
    assert (settings["by_role"], settings["fallback"], settings["effective_keeper_id"]) == (
        True,
        False,
        None,
    )
    assert settings["effective_keeper_name"] == "Cang、Cui"
    assert await console(desk, wang.headers) == {
        "profiles": ["worker"],
        "menus": ["production", "tasks", "assistant"],
    }
    assert await console(desk, cang.headers) == {
        "profiles": ["keeper"],
        # 没有加工和客户待办的权限，不显示。
        "menus": ["dashboard", "warehouse", "tasks", "assistant"],
    }
    me = await call(desk, cang.headers, "GET", "/api/v1/me")
    assert {"inventory:manage", "warehouse:confirm"} <= set(me["permissions"])

    # 主管开领料单：两位仓管都收到提醒（管理员不收），其中一位确认。
    frame = await material(desk, "AL-6063", "铝合金型材")
    await call(desk, desk.admin, "POST", f"/api/v1/products/{frame['id']}/stock",
               mode="set", quantity=10)  # fmt: skip
    doc = await call(
        desk, boss.headers, "POST", "/api/v1/warehouse/documents", 201,
        kind="requisition", lines=[{"product_id": frame["id"], "quantity": 2}],
    )  # fmt: skip
    for keeper in (cang, cui):
        notes = await call(desk, keeper.headers, "GET", "/api/v1/notifications")
        assert any(n["kind"] == "warehouse_pending" for n in notes["items"])
    admin_notes = await call(desk, desk.admin, "GET", "/api/v1/notifications")
    assert not any(n["kind"] == "warehouse_pending" for n in admin_notes["items"])
    confirmed = await call(
        desk, cui.headers, "POST", f"/api/v1/warehouse/documents/{doc['id']}/confirm"
    )
    assert confirmed["status"] == "confirmed"

    # 指定了仓管时以指定的为准。
    await call(desk, desk.admin, "PUT", "/api/v1/warehouse/settings", keeper_id=str(wang.staff_id))
    settings = await call(desk, desk.admin, "GET", "/api/v1/warehouse/settings")
    assert (settings["by_role"], settings["effective_keeper_name"]) == (False, "Wang")
    assert (await console(desk, wang.headers))["profiles"] == ["keeper", "worker"]


async def test_custom_roles_choose_a_console(desk: Desk) -> None:
    picker = await call(
        desk, desk.admin, "POST", "/api/v1/roles", 201,
        code="picker", name="拣货员", permissions=["inventory:manage", "production:work"],
    )  # fmt: skip
    assert (picker["console"], picker["console_auto"]) == ("keeper", True)
    helper = await call(
        desk, desk.admin, "POST", "/api/v1/roles", 201,
        code="helper", name="客服助理", permissions=["workbench:use", "todo:read"],
        console="agent",
    )  # fmt: skip
    assert (helper["console"], helper["console_auto"]) == ("agent", False)
    changed = await call(
        desk, desk.admin, "PATCH", f"/api/v1/roles/{picker['id']}", console="worker"
    )
    assert (changed["console"], changed["console_auto"]) == ("worker", False)
    reset = await call(desk, desk.admin, "PATCH", f"/api/v1/roles/{picker['id']}", console=None)
    assert (reset["console"], reset["console_auto"]) == ("keeper", True)
    roles = await call(desk, desk.admin, "GET", "/api/v1/roles")
    consoles = {r["code"]: r["console"] for r in roles["items"]}
    assert consoles["tenant_admin"] == "admin" and consoles["keeper"] == "keeper"

    staff = await desk.agent("zhou", roles=["helper"], online=False)
    assert (await console(desk, staff.headers))["profiles"] == ["agent"]


async def test_tenant_adjusts_menus_per_console(desk: Desk) -> None:
    defaults = await call(desk, desk.admin, "GET", "/api/v1/tenant/console")
    by_profile = {i["profile"]: i for i in defaults["items"]}
    assert by_profile["admin"]["editable"] is False
    assert by_profile["agent"]["customized"] is False
    assert "products" not in by_profile["agent"]["menus"]

    agent = await desk.agent("mei", roles=["agent"], online=False)
    saved = await call(
        desk, desk.admin, "PUT", "/api/v1/tenant/console",
        menus={"agent": ["products", "dashboard", "workbench", "orders", "orders"]},
    )  # fmt: skip
    agent_menus = next(i for i in saved["items"] if i["profile"] == "agent")
    assert agent_menus["customized"] is True
    assert agent_menus["menus"] == ["dashboard", "workbench", "orders", "products"]
    assert (await console(desk, agent.headers))["menus"] == [
        "dashboard",
        "workbench",
        "orders",
        "products",
    ]

    for bad in ({"admin": ["dashboard"]}, {"agent": []}):
        rejected = await desk.client.put(
            "/api/v1/tenant/console", headers=desk.admin, json={"menus": bad}
        )
        assert rejected.status_code == 422, rejected.text
    denied = await desk.client.get("/api/v1/tenant/console", headers=agent.headers)
    assert denied.status_code == 403

    # 没有列出的岗位恢复默认。
    await call(desk, desk.admin, "PUT", "/api/v1/tenant/console", menus={})
    assert "products" not in (await console(desk, agent.headers))["menus"]
