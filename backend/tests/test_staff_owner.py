"""企业所有者（设计文档 §39.5）：只能由平台创建，企业里不能分配；账号只能由本人修改。"""

from typing import Any

from tests import test_staff_admin
from tests.desk import Desk
from tests.factories import STAFF_PASSWORD

desk = test_staff_admin.desk


async def _staff(desk: Desk) -> dict[str, dict[str, Any]]:
    items = (await desk.client.get("/api/v1/staff", headers=desk.admin)).json()["items"]
    return {s["username"]: s for s in items}


async def test_owner_role_cannot_be_assigned(desk: Desk) -> None:
    staff = await _staff(desk)
    assert [name for name, s in staff.items() if s["is_owner"]] == ["admin"]

    body = {"username": "boss", "display_name": "老板", "password": STAFF_PASSWORD}
    created = await desk.client.post(
        "/api/v1/staff", headers=desk.admin, json={**body, "role_codes": ["tenant_admin"]}
    )
    assert created.status_code == 422
    assert "平台" in created.json()["error"]["message"]
    alice = await desk.agent("alice", online=False)
    promote = await desk.client.patch(
        f"/api/v1/staff/{alice.staff_id}",
        headers=desk.admin,
        json={"role_codes": ["agent", "tenant_admin"]},
    )
    assert promote.status_code == 422

    # 企业所有者自己的角色也不能改；姓名可以改。
    owner = staff["admin"]["id"]
    roles = await desk.client.patch(
        f"/api/v1/staff/{owner}", headers=desk.admin, json={"role_codes": ["tenant_admin", "agent"]}
    )
    assert roles.status_code == 422
    renamed = await desk.client.patch(
        f"/api/v1/staff/{owner}", headers=desk.admin, json={"display_name": "张总"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["is_owner"] is True


async def test_only_the_owner_manages_the_owner_account(desk: Desk) -> None:
    # 有全部权限的自定义角色（"副总"）也不能修改、停用、删除企业所有者，不能重置他的密码。
    catalog = (await desk.client.get("/api/v1/permissions", headers=desk.admin)).json()["items"]
    role = await desk.client.post(
        "/api/v1/roles",
        headers=desk.admin,
        json={"code": "deputy", "name": "副总", "permissions": [p["code"] for p in catalog]},
    )
    assert role.status_code == 201, role.text
    deputy = await desk.agent("deputy", roles=["deputy"], online=False)
    owner = (await _staff(desk))["admin"]["id"]
    for body in ({"display_name": "改名"}, {"status": "disabled"}):
        changed = await desk.client.patch(
            f"/api/v1/staff/{owner}", headers=deputy.headers, json=body
        )
        assert changed.status_code == 403, body
    reset = await desk.client.post(
        f"/api/v1/staff/{owner}/password", headers=deputy.headers, json={}
    )
    assert reset.status_code == 403
    assert "平台" in reset.json()["error"]["message"]
    deleted = await desk.client.delete(
        f"/api/v1/staff/diagram/nodes/{owner}", headers=deputy.headers
    )
    assert deleted.status_code == 403
    assert (await _staff(desk))["admin"]["status"] == "active"

    # 其他员工照常可以管理。
    alice = await desk.agent("alice", online=False)
    other = await desk.client.post(
        f"/api/v1/staff/{alice.staff_id}/password", headers=deputy.headers, json={}
    )
    assert other.status_code == 200, other.text


async def test_staff_given_the_role_before_are_not_owners(desk: Desk) -> None:
    extra = await desk.extra_admin("root2")
    staff = await _staff(desk)
    assert (staff["admin"]["is_owner"], staff["root2"]["is_owner"]) == (True, False)
    # 原来就有的角色保留：编辑时带着它照常保存；企业所有者可以重置他的密码、去掉这个角色。
    kept = await desk.client.patch(
        f"/api/v1/staff/{extra.staff_id}",
        headers=desk.admin,
        json={"display_name": "副管理员", "role_codes": ["tenant_admin"]},
    )
    assert kept.status_code == 200, kept.text
    reset = await desk.client.post(
        f"/api/v1/staff/{extra.staff_id}/password", headers=desk.admin, json={}
    )
    assert reset.status_code == 200, reset.text
    demoted = await desk.client.patch(
        f"/api/v1/staff/{extra.staff_id}", headers=desk.admin, json={"role_codes": ["agent"]}
    )
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["roles"] == ["agent"]
