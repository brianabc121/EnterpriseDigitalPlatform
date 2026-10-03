"""需要隔离 PostgreSQL/Redis 测试环境的布局接口集成测试。"""

from uuid import uuid4

import pytest

from tests import test_staff_admin
from tests.desk import Desk
from tests.factories import STAFF_PASSWORD

desk = test_staff_admin.desk


@pytest.mark.parametrize("direction", ["left", "right", "down"])
async def test_create_branch_persists_without_inheriting_permissions(desk: Desk, direction: str):
    parent = await desk.extra_admin("diagram-admin")
    response = await desk.client.post(
        "/api/v1/staff",
        headers=desk.admin,
        json={
            "username": "diagram-child",
            "display_name": "新客服",
            "password": STAFF_PASSWORD,
            "role_codes": ["agent"],
            "diagram_parent_id": str(parent.staff_id),
            "diagram_direction": direction,
        },
    )
    assert response.status_code == 201, response.text
    child = response.json()
    assert child["roles"] == ["agent"]
    assert "staff:manage" not in child["permissions"]
    listed = (await desk.client.get("/api/v1/staff", headers=desk.admin)).json()["items"]
    saved = next(item for item in listed if item["id"] == child["id"])
    assert saved["diagram_parent_id"] == str(parent.staff_id)
    assert saved["diagram_direction"] == direction


async def test_missing_source_rejected_without_partial_employee(desk: Desk):
    response = await desk.client.post(
        "/api/v1/staff",
        headers=desk.admin,
        json={
            "username": "diagram-rejected",
            "display_name": "错误来源",
            "password": STAFF_PASSWORD,
            "role_codes": ["agent"],
            "diagram_parent_id": str(uuid4()),
            "diagram_direction": "down",
        },
    )
    assert response.status_code == 422
    listed = (await desk.client.get("/api/v1/staff", headers=desk.admin)).json()["items"]
    assert all(item["username"] != "diagram-rejected" for item in listed)


async def test_draft_card_then_employee_keeps_child_and_cannot_convert_twice(desk: Desk):
    path = "/api/v1/staff/diagram/nodes"
    before = (await desk.client.get("/api/v1/staff", headers=desk.admin)).json()["items"]
    created = await desk.client.post(path, headers=desk.admin, json={"direction": "right"})
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["staff_id"] is None
    after = (await desk.client.get("/api/v1/staff", headers=desk.admin)).json()["items"]
    assert len(after) == len(before)
    child = await desk.client.post(
        path, headers=desk.admin, json={"parent_id": draft["id"], "direction": "down"}
    )
    assert child.status_code == 201, child.text
    body = {
        "username": "draft-account",
        "display_name": "张三",
        "password": STAFF_PASSWORD,
        "role_codes": ["agent"],
        "diagram_node_id": draft["id"],
    }
    converted = await desk.client.post("/api/v1/staff", headers=desk.admin, json=body)
    assert converted.status_code == 201, converted.text
    nodes = (await desk.client.get(path, headers=desk.admin)).json()["items"]
    assert next(n for n in nodes if n["id"] == draft["id"])["staff_id"] == converted.json()["id"]
    assert next(n for n in nodes if n["id"] == child.json()["id"])["parent_id"] == draft["id"]
    assert "staff:manage" not in converted.json()["permissions"]
    body["username"] = "second-account"
    repeated = await desk.client.post("/api/v1/staff", headers=desk.admin, json=body)
    assert repeated.status_code == 409


async def test_failed_conversion_leaves_draft_and_readonly_cannot_add(desk: Desk):
    path = "/api/v1/staff/diagram/nodes"
    agent = await desk.agent("diagram-reader", online=False)
    denied = await desk.client.post(path, headers=agent.headers, json={"direction": "left"})
    assert denied.status_code == 403
    draft = (await desk.client.post(path, headers=desk.admin, json={"direction": "left"})).json()
    failed = await desk.client.post(
        "/api/v1/staff",
        headers=desk.admin,
        json={
            "username": "bad-role",
            "display_name": "失败",
            "password": STAFF_PASSWORD,
            "role_codes": ["does-not-exist"],
            "diagram_node_id": draft["id"],
        },
    )
    assert failed.status_code == 422
    nodes = (await desk.client.get(path, headers=desk.admin)).json()["items"]
    assert next(n for n in nodes if n["id"] == draft["id"])["staff_id"] is None


async def test_delete_employee_card_keeps_children_and_revokes_account(desk: Desk):
    path = "/api/v1/staff/diagram/nodes"
    draft = (await desk.client.post(path, headers=desk.admin, json={"direction": "right"})).json()
    child = (
        await desk.client.post(
            path, headers=desk.admin, json={"parent_id": draft["id"], "direction": "down"}
        )
    ).json()
    created = await desk.client.post(
        "/api/v1/staff",
        headers=desk.admin,
        json={
            "username": "delete-card-account",
            "display_name": "待删除员工",
            "password": STAFF_PASSWORD,
            "role_codes": ["agent"],
            "diagram_node_id": draft["id"],
        },
    )
    assert created.status_code == 201, created.text
    deleted = await desk.client.delete(f"{path}/{draft['id']}", headers=desk.admin)
    assert deleted.status_code == 204, deleted.text
    nodes = (await desk.client.get(path, headers=desk.admin)).json()["items"]
    assert all(n["id"] != draft["id"] for n in nodes)
    assert next(n for n in nodes if n["id"] == child["id"])["parent_id"] is None
    staff = (await desk.client.get("/api/v1/staff", headers=desk.admin)).json()["items"]
    assert all(s["id"] != created.json()["id"] for s in staff)
    company = await desk.client.delete(f"{path}/company", headers=desk.admin)
    assert company.status_code == 422
    reader = await desk.agent("delete-reader", online=False)
    denied = await desk.client.delete(f"{path}/{child['id']}", headers=reader.headers)
    assert denied.status_code == 403
