"""客户敏感字段（加密、掩码、授权查看）、导出、合并、个人信息查询与删除，以及租户操作日志。"""

import csv
import io
import json
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.files.service import key_of_url
from tests.desk import Agent, Desk
from tests.factories import ADMIN_PASSWORD, STAFF_PASSWORD
from tests.fake_openim import FakeOpenIM
from tests.fake_storage import FakeStorage
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


async def _customer(desk: Desk, headers: dict[str, str], **fields: Any) -> dict[str, Any]:
    response = await desk.client.post("/api/v1/customers", headers=headers, json=fields)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def _role(desk: Desk, code: str, permissions: list[str]) -> None:
    response = await desk.client.post(
        "/api/v1/roles",
        headers=desk.admin,
        json={"code": code, "name": code, "permissions": permissions},
    )
    assert response.status_code == 201, response.text


async def test_phone_and_email_are_encrypted_masked_and_searchable(desk: Desk) -> None:
    created = await _customer(
        desk,
        desk.admin,
        display_name="张三",
        phone="+86 138-0000-1234",
        email="Zhang.San@Example.com",
        company="示例科技",
    )
    assert (created["phone"], created["email"], created["company"]) == (
        "138****1234",
        "z***@example.com",
        "示例科技",
    )
    [row] = await desk.sql("SELECT phone_enc, phone_hash, email_enc FROM customers")
    assert row["phone_enc"].startswith("v2:1:") and "1234" not in row["phone_enc"]
    assert len(row["phone_hash"]) == 64
    await _customer(desk, desk.admin, display_name="李四", phone="13900005678")

    async def search(q: str) -> list[str]:
        response = await desk.client.get("/api/v1/customers", headers=desk.admin, params={"q": q})
        assert response.status_code == 200, response.text
        return [c["display_name"] for c in response.json()["items"]]

    # 手机号、邮箱只能完整匹配（盲索引）；名称和公司可以部分匹配。
    assert await search("13800001234") == ["张三"]
    assert await search("138 0000 1234") == ["张三"]
    assert await search("zhang.san@example.com") == ["张三"]
    assert await search("1380000") == []
    assert await search("示例") == ["张三"]
    assert await search("李") == ["李四"]
    assert await search("100%_") == []

    bad = await desk.client.post(
        "/api/v1/customers", headers=desk.admin, json={"display_name": "x", "phone": "abc"}
    )
    assert bad.status_code == 422
    # 修改与清除。
    updated = await desk.client.patch(
        f"/api/v1/customers/{created['id']}",
        headers=desk.admin,
        json={"phone": "", "email": "new@example.com"},
    )
    assert updated.status_code == 200, updated.text
    assert (updated.json()["phone"], updated.json()["email"]) == (None, "n***@example.com")
    assert await search("13800001234") == []


async def test_viewing_plaintext_requires_permission_and_is_audited(desk: Desk) -> None:
    agent = await desk.agent("alice", online=False)
    mine = await _customer(desk, agent.headers, display_name="王五", phone="13700001111")
    detail = await desk.client.get(f"/api/v1/customers/{mine['id']}", headers=agent.headers)
    assert detail.json()["phone"] == "137****1111"
    denied = await desk.client.get(
        f"/api/v1/customers/{mine['id']}/sensitive", headers=agent.headers
    )
    assert denied.status_code == 403

    shown = await desk.client.get(f"/api/v1/customers/{mine['id']}/sensitive", headers=desk.admin)
    assert shown.json() == {"phone": "13700001111", "email": None}
    [audit] = await desk.sql(
        "SELECT resource_id, detail FROM audit_logs WHERE action = 'customer.view_sensitive'"
    )
    assert audit["resource_id"] == mine["id"]

    # 自定义角色授予查看权限后，坐席只能查看自己数据范围内的客户。
    await _role(desk, "sensitive_agent", ["customer:read", "customer:view_sensitive"])
    viewer = await desk.agent("bob", roles=["sensitive_agent"], online=False)
    other = await desk.client.get(
        f"/api/v1/customers/{mine['id']}/sensitive", headers=viewer.headers
    )
    assert other.status_code == 404


async def test_export_requires_password_and_masks_without_permission(desk: Desk) -> None:
    await _customer(
        desk, desk.admin, display_name="张三", phone="13800001234", company="=HYPERLINK(1)"
    )
    await _customer(desk, desk.admin, display_name="李四", email="li@example.com")

    wrong = await desk.client.post(
        "/api/v1/customers/export", headers=desk.admin, json={"password": "wrong"}
    )
    assert wrong.status_code == 422
    exported = await desk.client.post(
        "/api/v1/customers/export", headers=desk.admin, json={"password": ADMIN_PASSWORD}
    )
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert rows[0][:4] == ["客户ID", "名称", "手机号", "邮箱"]
    by_name = {r[1]: r for r in rows[1:]}
    assert by_name["张三"][2] == "13800001234"
    # 以等号开头的单元格加了单引号，表格软件不会当作公式执行。
    assert by_name["张三"][4] == "'=HYPERLINK(1)"
    assert by_name["李四"][3] == "li@example.com"
    [audit] = await desk.sql("SELECT detail FROM audit_logs WHERE action = 'customer.export'")
    assert '"rows": 2' in audit["detail"] and '"plaintext": true' in audit["detail"]

    # 只有导出权限、没有查看敏感信息权限：导出掩码，且只导出自己数据范围内的客户。
    await _role(desk, "exporter", ["customer:read", "customer:create", "customer:export"])
    exporter = await desk.agent("carol", roles=["exporter"], online=False)
    await _customer(desk, exporter.headers, display_name="赵六", phone="13600002222")
    masked = await desk.client.post(
        "/api/v1/customers/export", headers=exporter.headers, json={"password": STAFF_PASSWORD}
    )
    rows = list(csv.reader(io.StringIO(masked.content.decode("utf-8-sig"))))
    assert [(r[1], r[2]) for r in rows[1:]] == [("赵六", "136****2222")]
    agent = await desk.agent("dave", online=False)
    denied = await desk.client.post(
        "/api/v1/customers/export", headers=agent.headers, json={"password": STAFF_PASSWORD}
    )
    assert denied.status_code == 403


async def _chat_with_file(desk: Desk, agent: Agent, fake_storage: FakeStorage) -> tuple[Any, str]:
    """访客发起会话，坐席回复一张图片（对象存储里放一个文件）。返回（会话，对象 key）。"""
    visitor = await desk.visitor()
    await desk.say(visitor, "我的手机号是 13800001234")
    chat = await desk.session_of(visitor)
    upload = await desk.client.post(
        "/api/v1/uploads",
        headers=agent.headers,
        json={"filename": "报价.png", "content_type": "image/png", "size": 3},
    )
    file_url = upload.json()["file_url"]
    key = key_of_url(desk.settings, file_url)
    assert key is not None
    fake_storage.objects[f"/edp-files/{key}"] = (b"PNG", "image/png")
    sent = await desk.client.post(
        f"/api/v1/sessions/{chat['id']}/messages",
        headers=agent.headers,
        json={
            "client_msg_id": uuid.uuid4().hex,
            "type": "image",
            "attachment": {
                "url": file_url,
                "name": "报价.png",
                "size": 3,
                "content_type": "image/png",
            },
        },
    )
    assert sent.status_code == 200, sent.text
    await desk.flush()
    return visitor, key


async def test_merge_moves_identities_and_sessions(desk: Desk, fake_storage: FakeStorage) -> None:
    alice = await desk.agent("alice")
    visitor, _ = await _chat_with_file(desk, alice, fake_storage)
    [source] = await desk.sql("SELECT id FROM customers")
    target = await _customer(desk, desk.admin, display_name="张三（老客户）", phone="13800001234")
    await desk.client.patch(
        f"/api/v1/customers/{source['id']}",
        headers=desk.admin,
        json={"tags": ["访客"], "notes": "网页咨询", "email": "z@example.com"},
    )
    await desk.client.patch(
        f"/api/v1/customers/{target['id']}", headers=desk.admin, json={"tags": ["VIP"]}
    )

    agent_try = await desk.client.post(
        f"/api/v1/customers/{target['id']}/merge",
        headers=alice.headers,
        json={"source_ids": [str(source["id"])]},
    )
    assert agent_try.status_code == 403
    merged = await desk.client.post(
        f"/api/v1/customers/{target['id']}/merge",
        headers=desk.admin,
        json={"source_ids": [str(source["id"])]},
    )
    assert merged.status_code == 200, merged.text
    body = merged.json()
    assert body["tags"] == ["VIP", "访客"]
    assert (body["phone"], body["email"]) == ("138****1234", "z***@example.com")
    assert "网页咨询" in body["notes"]
    assert [i["channel_type"] for i in body["identities"]] == ["web"]
    assert await desk.sql("SELECT 1 FROM customers WHERE id = $1", source["id"]) == []
    chat = await desk.session_of(visitor)
    assert str(chat["customer_id"]) == target["id"]
    [room] = await desk.sql("SELECT customer_id FROM rooms WHERE id = $1", visitor.room_id)
    assert str(room["customer_id"]) == target["id"]
    # 访客继续发消息，归到合并后的客户。
    await desk.say(visitor, "还在吗")
    assert str((await desk.session_of(visitor))["customer_id"]) == target["id"]
    self_merge = await desk.client.post(
        f"/api/v1/customers/{target['id']}/merge",
        headers=desk.admin,
        json={"source_ids": [target["id"]]},
    )
    assert self_merge.status_code == 422


async def test_personal_data_copy_and_erasure(desk: Desk, fake_storage: FakeStorage) -> None:
    alice = await desk.agent("alice")
    visitor, key = await _chat_with_file(desk, alice, fake_storage)
    chat = await desk.session_of(visitor)
    [customer] = await desk.sql("SELECT id, display_name FROM customers")
    customer_id = str(customer["id"])
    await desk.client.patch(
        f"/api/v1/customers/{customer_id}", headers=desk.admin, json={"phone": "13800001234"}
    )
    # 知识候选引用了这次会话的对话片段。
    await desk.sql(
        "INSERT INTO kb_candidates (id, tenant_id, kind, question, answer, evidence)"
        " VALUES ($1, $2, 'new', '怎么联系', '留手机号', $3::jsonb)",
        uuid.uuid4(),
        desk.tenant_id,
        json.dumps(
            [
                {
                    "session_id": str(chat["id"]),
                    "lines": [{"role": "customer", "text": "13800001234"}],
                },
                {"session_id": str(uuid.uuid4()), "lines": []},
            ]
        ),
    )

    copy = await desk.client.post(
        f"/api/v1/customers/{customer_id}/personal-data",
        headers=desk.admin,
        json={"reason": "客户来电要求查询"},
    )
    assert copy.status_code == 200, copy.text
    document = copy.json()
    assert document["customer"]["phone"] == "13800001234"
    assert "我的手机号是 13800001234" in [m["text"] for m in document["messages"]]
    assert [s["id"] for s in document["sessions"]] == [str(chat["id"])]
    assert document["identities"][0]["channel_type"] == "web"

    mismatch = await desk.client.post(
        f"/api/v1/customers/{customer_id}/erase",
        headers=desk.admin,
        json={"confirm_name": "别人", "reason": "客户要求删除"},
    )
    assert mismatch.status_code == 422
    erased = await desk.client.post(
        f"/api/v1/customers/{customer_id}/erase",
        headers=desk.admin,
        json={"confirm_name": customer["display_name"], "reason": "客户要求删除"},
    )
    assert erased.status_code == 200, erased.text
    result = erased.json()
    assert (result["sessions"], result["files"], result["im_groups"]) == (1, 1, 1)
    assert result["messages"] >= 2
    assert await desk.sql("SELECT 1 FROM customers") == []
    assert await desk.sql("SELECT 1 FROM messages") == []
    assert await desk.sql("SELECT 1 FROM sessions") == []
    assert f"/edp-files/{key}" not in fake_storage.objects
    assert visitor.group_id not in desk.im.groups
    [candidate] = await desk.sql("SELECT evidence::text AS evidence FROM kb_candidates")
    assert (
        "13800001234" not in candidate["evidence"] and str(chat["id"]) not in candidate["evidence"]
    )

    requests = await desk.client.get("/api/v1/customers/privacy-requests", headers=desk.admin)
    items = requests.json()["items"]
    assert [(r["kind"], r["customer_id"]) for r in items] == [
        ("erase", customer_id),
        ("access", customer_id),
    ]
    # 记录里只保留掩码后的名称，不保留联系方式。
    assert items[0]["customer_name"].endswith("**")
    assert items[0]["requested_by_name"] == "管理员"
    agent_list = await desk.client.get("/api/v1/customers/privacy-requests", headers=alice.headers)
    assert agent_list.status_code == 403


async def test_tenant_audit_log_viewer(desk: Desk, app: FastAPI) -> None:
    from tests.factories import provision

    await _customer(desk, desk.admin, display_name="张三", phone="13800001234")
    other = await provision(app, "other")
    await desk.sql(
        "INSERT INTO audit_logs (id, tenant_id, actor_type, action)"
        " VALUES ($1, $2, 'staff', 'x.y')",
        uuid.uuid4(),
        other,
    )
    await desk.sql(
        "INSERT INTO audit_logs (id, tenant_id, actor_type, actor_id, action)"
        " VALUES ($1, $2, 'platform', $3, 'support.view')",
        uuid.uuid4(),
        desk.tenant_id,
        uuid.uuid4(),
    )
    logs = await desk.client.get("/api/v1/audit-logs", headers=desk.admin)
    assert logs.status_code == 200, logs.text
    items = logs.json()["items"]
    actions = [i["action"] for i in items]
    assert "customer.create" in actions and "auth.login" in actions and "x.y" not in actions
    platform = next(i for i in items if i["action"] == "support.view")
    assert platform["actor_name"] == "平台运维"
    created = next(i for i in items if i["action"] == "customer.create")
    assert created["actor_name"] == "管理员"

    filtered = await desk.client.get(
        "/api/v1/audit-logs", headers=desk.admin, params={"action": "customer", "limit": 1}
    )
    assert [i["action"] for i in filtered.json()["items"]] == ["customer.create"]
    agent = await desk.agent("alice", online=False)
    denied = await desk.client.get("/api/v1/audit-logs", headers=agent.headers)
    assert denied.status_code == 403
