"""越权矩阵（P1 验收"越权测试全部通过"）：跨租户、跨坐席访问一律拒绝，且不改变对方数据。

- 跨租户：租户 A 的管理员拿租户 B 的对象 ID 调用每一个带路径参数的接口（覆盖检查保证新增接口
  必须加入矩阵），以及把 B 的 ID 放进请求体、查询参数；列表接口的响应里不能出现 B 的 ID。
- 跨坐席：坐席只能访问自己接待的会话和自己的客户，管理类接口一律 403。
"""

import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from tests.desk import Agent, Desk, Visitor
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls

DENIED = {403, 404}
REJECTED = {403, 404, 422}
# 路径参数里不是对象 ID 的值（检查列表是否泄露 ID 时跳过）。
NOT_IDS = {"version", "userid"}

# 每个带路径参数的租户接口：(方法, 路径模板, 请求体)。请求体必须合法，才能验证到权限而不是参数校验。
MATRIX: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/v1/customers/{customer_id}", None),
    ("PATCH", "/api/v1/customers/{customer_id}", {"notes": "越权修改"}),
    ("GET", "/api/v1/customers/{customer_id}/owner-history", None),
    ("GET", "/api/v1/customers/{customer_id}/wecom", None),
    ("GET", "/api/v1/customers/{customer_id}/sensitive", None),
    ("POST", "/api/v1/customers/{customer_id}/merge", {"source_ids": ["{own_customer_id}"]}),
    ("POST", "/api/v1/customers/{customer_id}/personal-data", {"reason": "越权查询"}),
    ("POST", "/api/v1/customers/{customer_id}/erase", {"confirm_name": "x", "reason": "越权"}),
    ("POST", "/api/v1/customers/{customer_id}/transfer-requests", {"reason": "越权申请"}),
    ("POST", "/api/v1/customers/transfer-requests/{request_id}/approve", {}),
    ("POST", "/api/v1/customers/transfer-requests/{request_id}/reject", {}),
    ("POST", "/api/v1/customers/transfer-requests/{request_id}/cancel", None),
    ("GET", "/api/v1/customers/{customer_id}/lead-drafts", None),
    ("GET", "/api/v1/customers/{customer_id}/summaries", None),
    ("POST", "/api/v1/customers/lead-drafts/{draft_id}/confirm", None),
    ("POST", "/api/v1/customers/lead-drafts/{draft_id}/discard", None),
    ("POST", "/api/v1/sessions/{session_id}/return-to-ai", None),
    ("POST", "/api/v1/sessions/{session_id}/handoff", None),
    ("POST", "/api/v1/sessions/{session_id}/monitor", None),
    ("POST", "/api/v1/sessions/{session_id}/assists", {"staff_id": "{own_staff_id}"}),
    ("DELETE", "/api/v1/sessions/{session_id}/watchers/{staff_id}", None),
    ("PATCH", "/api/v1/staff/{staff_id}", {"display_name": "越权修改"}),
    ("POST", "/api/v1/staff/{staff_id}/password", {"password": "cross-tenant-reset"}),
    ("PATCH", "/api/v1/roles/{role_id}", {"name": "越权修改"}),
    ("DELETE", "/api/v1/roles/{role_id}", None),
    ("POST", "/api/v1/customers/handover/{staff_id}", {"to_owner_id": "{own_staff_id}"}),
    ("PATCH", "/api/v1/channels/{channel_id}", {"name": "越权修改"}),
    ("POST", "/api/v1/channels/{channel_id}/identity-secret", None),
    ("GET", "/api/v1/rooms/{room_id}/messages", None),
    ("GET", "/api/v1/sessions/{session_id}", None),
    ("GET", "/api/v1/sessions/{session_id}/messages", None),
    ("GET", "/api/v1/sessions/{session_id}/reply-window", None),
    ("GET", "/api/v1/sessions/{session_id}/ai-decisions", None),
    ("POST", "/api/v1/sessions/{session_id}/suggestions", None),
    ("GET", "/api/v1/sessions/{session_id}/alerts", None),
    ("GET", "/api/v1/sessions/{session_id}/summary", None),
    ("POST", "/api/v1/sessions/{session_id}/summary", None),
    ("POST", "/api/v1/sessions/{session_id}/summary/confirm", {"summary": "越权确认"}),
    ("POST", "/api/v1/sessions/{session_id}/summary/discard", None),
    ("POST", "/api/v1/sessions/{session_id}/close", None),
    ("POST", "/api/v1/sessions/{session_id}/messages", {"client_msg_id": "x" * 16, "text": "越权"}),
    ("POST", "/api/v1/sessions/{session_id}/transfer", {"to_staff_id": "{own_staff_id}"}),
    ("POST", "/api/v1/tickets/{ticket_id}/done", None),
    ("POST", "/api/v1/transfers/{transfer_id}/accept", None),
    ("POST", "/api/v1/transfers/{transfer_id}/reject", None),
    ("POST", "/api/v1/transfers/{transfer_id}/cancel", None),
    ("PATCH", "/api/v1/skill-groups/{group_id}", {"name": "越权修改"}),
    ("DELETE", "/api/v1/skill-groups/{group_id}", None),
    ("PATCH", "/api/v1/routing-policies/{policy_id}", {"name": "越权修改"}),
    ("DELETE", "/api/v1/routing-policies/{policy_id}", None),
    ("PATCH", "/api/v1/agents/{staff_id}", {"max_concurrency": 1}),
    ("PATCH", "/api/v1/quick-replies/{reply_id}", {"title": "越权修改"}),
    ("DELETE", "/api/v1/quick-replies/{reply_id}", None),
    ("GET", "/api/v1/kb/items/{item_id}", None),
    ("PATCH", "/api/v1/kb/items/{item_id}", {"title": "越权修改"}),
    ("DELETE", "/api/v1/kb/items/{item_id}", None),
    ("POST", "/api/v1/kb/items/{item_id}/publish", None),
    ("POST", "/api/v1/kb/items/{item_id}/archive", None),
    ("GET", "/api/v1/kb/items/{item_id}/versions", None),
    ("POST", "/api/v1/kb/items/{item_id}/versions/{version}/restore", None),
    ("POST", "/api/v1/kb/items/{item_id}/read", None),
    ("GET", "/api/v1/kb/items/{item_id}/reads", None),
    ("POST", "/api/v1/kb/items/{item_id}/feedback", {"value": 1}),
    ("GET", "/api/v1/kb/items/{item_id}/stats", None),
    ("GET", "/api/v1/kb/candidates/{candidate_id}", None),
    ("POST", "/api/v1/kb/candidates/{candidate_id}/approve", {"answer": "越权"}),
    ("POST", "/api/v1/kb/candidates/{candidate_id}/merge", {"item_id": "{own_item_id}"}),
    ("POST", "/api/v1/kb/candidates/{candidate_id}/reject", {"reason": "越权"}),
    ("GET", "/api/v1/kb/imports/{job_id}", None),
    ("PATCH", "/api/v1/kb/spaces/{space_id}", {"name": "越权修改"}),
    ("DELETE", "/api/v1/kb/spaces/{space_id}", None),
    ("PATCH", "/api/v1/kb/categories/{category_id}", {"name": "越权修改"}),
    ("DELETE", "/api/v1/kb/categories/{category_id}", None),
    ("POST", "/api/v1/notifications/{notification_id}/read", None),
    ("PUT", "/api/v1/admin/integrations/wecom/members/{userid}", {"staff_id": "{own_staff_id}"}),
    ("DELETE", "/api/v1/admin/integrations/wecom/join-ways/{way_id}", None),
    ("PUT", "/api/v1/sidebar/customers/{customer_id}/tags", {"tags": ["越权"]}),
    ("GET", "/api/v1/wecom/broadcasts/{broadcast_id}", None),
    ("POST", "/api/v1/wecom/broadcasts/{broadcast_id}/refresh", None),
    ("POST", "/api/v1/wecom/broadcasts/{broadcast_id}/cancel", None),
    ("POST", "/api/v1/wecom/broadcasts/{broadcast_id}/remind", None),
    ("GET", "/api/v1/tenant/exports/{export_id}/download", None),
    ("DELETE", "/api/v1/tenant/support-grants/{grant_id}", None),
]


@dataclass
class Tenant:
    desk: Desk
    agent: Agent
    other_agent: Agent
    visitor: Visitor
    ids: dict[str, str]


async def build(desk: Desk) -> Tenant:
    """一个租户的全套对象：接待中的会话、待确认的转接、留言、话术、技能组、路由策略。"""
    agent = await desk.agent("carol")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    other = await desk.agent("dave")
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == agent.staff_id
    client = desk.client
    sent = await client.post(
        f"/api/v1/sessions/{chat['id']}/messages",
        headers=agent.headers,
        json={"client_msg_id": uuid.uuid4().hex, "text": "您好"},
    )
    assert sent.status_code == 200, sent.text
    transfer = await client.post(
        f"/api/v1/sessions/{chat['id']}/transfer",
        headers=agent.headers,
        json={"to_staff_id": str(other.staff_id)},
    )
    assert transfer.status_code == 200, transfer.text
    leaving = await desk.visitor()
    await client.post(
        "/api/v1/visitor/tickets",
        headers={"X-Visitor-Token": leaving.visitor_token},
        json={"content": "请回电"},
    )
    reply = await client.post(
        "/api/v1/quick-replies", headers=agent.headers, json={"title": "问候", "content": "您好"}
    )
    group = await client.post(
        "/api/v1/skill-groups",
        headers=desk.admin,
        json={"name": "售后", "members": [{"staff_id": str(agent.staff_id)}]},
    )
    policy = await client.post(
        "/api/v1/routing-policies", headers=desk.admin, json={"name": "夜间"}
    )
    # 仅管理员可见的知识：坐席既看不到，也不能修改。
    knowledge = await client.post(
        "/api/v1/kb/items",
        headers=desk.admin,
        json={"title": "内部报价规则", "content": "仅限内部", "visibility": "admin"},
    )
    [channel] = (await client.get("/api/v1/channels", headers=desk.admin)).json()["items"]
    [ticket] = await desk.sql("SELECT id FROM tickets WHERE tenant_id = $1", desk.tenant_id)
    # 从会话提炼的待审候选（直接写库，提炼流程见 test_kb_extraction.py）。
    [candidate] = await desk.sql(
        "INSERT INTO kb_candidates (id, tenant_id, kind, question, answer)"
        " VALUES ($1, $2, 'new', '周末发货吗', '周末正常发货') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
    )
    # 群发任务和客户群活码（直接写库，流程见 test_wecom_extras.py）。
    [broadcast] = await desk.sql(
        "INSERT INTO wecom_broadcasts (id, tenant_id, kind, title, content, status)"
        " VALUES ($1, $2, 'single', '国庆活动', '全场九折', 'created') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
    )
    [way] = await desk.sql(
        "INSERT INTO wecom_join_ways (id, tenant_id, config_id, name, state)"
        " VALUES ($1, $2, $3, '活动二维码', $3) RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        f"edp{desk.code}",
    )
    request = await client.post(
        f"/api/v1/customers/{chat['customer_id']}/transfer-requests",
        headers=desk.admin,
        json={"to_owner_id": str(agent.staff_id), "reason": "由 Carol 跟进"},
    )
    assert request.status_code == 201, request.text
    role = await client.post(
        "/api/v1/roles",
        headers=desk.admin,
        json={"code": "quality", "name": "质检", "permissions": ["report:view"]},
    )
    # 已完成的数据导出和平台访问授权（流程见 test_lifecycle.py）。
    [export] = await desk.sql(
        "INSERT INTO tenant_exports (id, tenant_id, status, object_key, expires_at)"
        " VALUES ($1, $2, 'done', $3, now() + interval '1 day') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        f"{desk.code}/_exports/x.zip",
    )
    [grant] = await desk.sql(
        "INSERT INTO support_grants (id, tenant_id, reason, expires_at)"
        " VALUES ($1, $2, '排查问题', now() + interval '1 day') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
    )
    space = await client.post("/api/v1/kb/spaces", headers=desk.admin, json={"name": "售后"})
    category = await client.post(
        "/api/v1/kb/categories",
        headers=desk.admin,
        json={"space_id": space.json()["id"], "name": "物流"},
    )
    # 知识导入任务（直接写库，流程见 test_kb_import_g5.py）。
    [job] = await desk.sql(
        "INSERT INTO kb_import_jobs (id, tenant_id, kind, params)"
        " VALUES ($1, $2, 'crawl', '{\"url\": \"https://help.example.com/\"}') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
    )
    # AI 登记的线索和会话小结草稿（直接写库，流程见 test_copilot_g5.py）。
    draft = await lead_draft(desk, chat)
    await desk.sql(
        "INSERT INTO session_summaries (tenant_id, session_id, customer_id, summary, tags)"
        " VALUES ($1, $2, $3, '客户问候', '{咨询}')",
        desk.tenant_id,
        chat["id"],
        chat["customer_id"],
    )
    await desk.flush()
    ids = {
        "customer_id": str(chat["customer_id"]),
        "staff_id": str(agent.staff_id),
        "channel_id": channel["id"],
        "room_id": str(chat["room_id"]),
        "session_id": str(chat["id"]),
        "ticket_id": str(ticket["id"]),
        "transfer_id": transfer.json()["id"],
        "group_id": group.json()["id"],
        "policy_id": policy.json()["id"],
        "reply_id": reply.json()["id"],
        "item_id": knowledge.json()["id"],
        "candidate_id": str(candidate["id"]),
        "broadcast_id": str(broadcast["id"]),
        "way_id": str(way["id"]),
        "export_id": str(export["id"]),
        "grant_id": str(grant["id"]),
        "role_id": role.json()["id"],
        "request_id": request.json()["id"],
        "draft_id": draft,
        "space_id": space.json()["id"],
        "category_id": category.json()["id"],
        "job_id": str(job["id"]),
        "notification_id": await notification(desk, agent.staff_id),
        "version": "1",
        "userid": "zhangsan",
        "tenant_id": str(desk.tenant_id),
    }
    return Tenant(desk, agent, other, visitor, ids)


async def lead_draft(desk: Desk, chat: Any) -> str:
    [draft] = await desk.sql(
        "INSERT INTO customer_lead_drafts (id, tenant_id, customer_id, session_id, fields)"
        ' VALUES ($1, $2, $3, $4, \'{"company": "星河科技"}\') RETURNING id',
        uuid.uuid4(),
        desk.tenant_id,
        chat["customer_id"],
        chat["id"],
    )
    return str(draft["id"])


async def notification(desk: Desk, staff_id: uuid.UUID) -> str:
    [row] = await desk.sql(
        "INSERT INTO staff_notifications (id, tenant_id, staff_id, kind, title)"
        " VALUES ($1, $2, $3, 'kb_expiring', '知识即将到期') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        staff_id,
    )
    return str(row["id"])


@pytest.fixture
async def tenants(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> tuple[Tenant, Tenant]:
    acme = await build(await Desk(app, client, fake_im, settings, database_urls).open("acme"))
    globex = await build(await Desk(app, client, fake_im, settings, database_urls).open("globex"))
    return acme, globex


def fill(value: Any, ids: dict[str, str], own: dict[str, str]) -> Any:
    """路径与请求体里的 {name} 换成对方的 ID，{own_name} 换成自己租户的 ID。"""
    if isinstance(value, str):
        for key, id_ in own.items():
            value = value.replace(f"{{own_{key}}}", id_)
        for key, id_ in ids.items():
            value = value.replace(f"{{{key}}}", id_)
        return value
    if isinstance(value, dict):
        return {k: fill(v, ids, own) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, ids, own) for v in value]
    return value


async def call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    **params: Any,
) -> httpx.Response:
    return await client.request(method, path, headers=headers, json=body, params=params or None)


async def snapshot(desk: Desk) -> list[Any]:
    """对方租户里可能被越权修改的数据。"""
    tables = {
        "sessions": "id, status, assignee_id, closed_at",
        "customers": "id, owner_id, notes, display_name",
        "channel_accounts": "id, name, config",
        "session_transfers": "id, status",
        "tickets": "id, status",
        "skill_groups": "id, name",
        "skill_group_members": "skill_group_id, staff_id",
        "routing_policies": "id, name, default_skill_group_id",
        "agent_states": "staff_id, max_concurrency",
        "quick_replies": "id, title",
        "kb_items": "id, title, status, version",
        "kb_candidates": "id, status",
        "wecom_broadcasts": "id, status",
        "wecom_join_ways": "id, name",
        "tenant_exports": "id, status, object_key",
        "support_grants": "id, revoked_at",
        "messages": "id",
        "staff": "id, display_name, status, password_hash",
        "roles": "id, name, permissions",
        "refresh_tokens": "id, revoked_at",
        "privacy_requests": "id",
        "rooms": "id, customer_id",
        "customer_transfer_requests": "id, status",
        "session_watchers": "session_id, staff_id, left_at",
        "customer_lead_drafts": "id, status",
        "session_summaries": "session_id, status, summary",
        "kb_spaces": "id, name",
        "kb_categories": "id, name, parent_id",
        "staff_notifications": "id, read_at",
        "kb_import_jobs": "id, status",
    }
    rows = []
    for table, columns in tables.items():
        order = ", ".join(str(i + 1) for i in range(len(columns.split(","))))
        found = await desk.sql(
            f"SELECT {columns} FROM {table} WHERE tenant_id = $1 ORDER BY {order}",
            desk.tenant_id,
        )
        rows.append((table, [tuple(r) for r in found]))
    return rows


def test_matrix_covers_every_route_with_an_id(app: FastAPI) -> None:
    routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1/") and "{" in path
        for method in operations
    }
    assert routes == {(method, path) for method, path, _ in MATRIX}


async def test_other_tenants_objects_are_invisible_and_untouchable(
    tenants: tuple[Tenant, Tenant],
) -> None:
    acme, globex = tenants
    client = acme.desk.client
    before = await snapshot(globex.desk)

    results = {}
    for method, template, body in MATRIX:
        path = fill(template, globex.ids, acme.ids)
        response = await call(
            client, method, path, acme.desk.admin, fill(body, globex.ids, acme.ids)
        )
        results[f"{method} {template}"] = response.status_code
    platform = await call(
        client, "GET", f"/platform/v1/tenants/{globex.ids['tenant_id']}", acme.desk.admin
    )
    await globex.desk.flush()

    assert {route: code for route, code in results.items() if code not in DENIED} == {}
    assert platform.status_code == 401
    assert await snapshot(globex.desk) == before

    # 对照：同样的读取和修改由对方租户自己发出时成功，说明上面的拒绝来自租户隔离，而不是请求有误。
    controls = [
        (method, template, body)
        for method, template, body in MATRIX
        if method == "GET" or (method == "PATCH" and "quick-replies" not in template)
    ]
    for method, template, body in controls:
        path = fill(template, globex.ids, globex.ids)
        response = await call(
            client, method, path, globex.desk.admin, fill(body, globex.ids, globex.ids)
        )
        assert response.status_code in (200, 204), (template, response.text)


async def test_other_tenants_ids_in_bodies_and_queries_are_rejected(
    tenants: tuple[Tenant, Tenant],
) -> None:
    acme, globex = tenants
    own, other = acme.ids, globex.ids
    vectors: list[tuple[str, str, dict[str, Any]]] = [
        (
            "POST",
            "/api/v1/skill-groups",
            {"name": "x", "members": [{"staff_id": other["staff_id"]}]},
        ),
        (
            "PATCH",
            f"/api/v1/skill-groups/{own['group_id']}",
            {"members": [{"staff_id": other["staff_id"]}]},
        ),
        (
            "POST",
            "/api/v1/routing-policies",
            {"name": "x", "default_skill_group_id": other["group_id"]},
        ),
        (
            "PATCH",
            f"/api/v1/routing-policies/{own['policy_id']}",
            {"default_skill_group_id": other["group_id"]},
        ),
        (
            "PATCH",
            f"/api/v1/channels/{own['channel_id']}",
            {"routing_policy_id": other["policy_id"]},
        ),
        ("POST", "/api/v1/customers", {"display_name": "x", "owner_id": other["staff_id"]}),
        (
            "POST",
            "/api/v1/customers/transfer",
            {"customer_ids": [other["customer_id"]], "to_owner_id": own["staff_id"]},
        ),
        (
            "POST",
            "/api/v1/customers/transfer",
            {"customer_ids": [own["customer_id"]], "to_owner_id": other["staff_id"]},
        ),
        (
            "POST",
            f"/api/v1/customers/handover/{acme.other_agent.staff_id}",
            {"to_owner_id": other["staff_id"]},
        ),
        (
            "POST",
            f"/api/v1/customers/handover/{acme.other_agent.staff_id}",
            {"to_group_id": other["group_id"]},
        ),
        (
            "POST",
            f"/api/v1/sessions/{own['session_id']}/transfer",
            {"to_staff_id": other["staff_id"], "force": True},
        ),
        (
            "POST",
            f"/api/v1/sessions/{own['session_id']}/transfer",
            {"to_group_id": other["group_id"], "force": True},
        ),
        (
            "POST",
            f"/api/v1/customers/{own['customer_id']}/merge",
            {"source_ids": [other["customer_id"]]},
        ),
    ]
    before = await snapshot(globex.desk)

    results = {}
    for method, path, body in vectors:
        response = await call(acme.desk.client, method, path, acme.desk.admin, body)
        results[f"{method} {path} {sorted(body)}"] = response.status_code

    assert {k: code for k, code in results.items() if code not in REJECTED} == {}
    assert await snapshot(globex.desk) == before


async def test_lists_never_show_other_tenants_ids(
    app: FastAPI, tenants: tuple[Tenant, Tenant]
) -> None:
    acme, globex = tenants
    client = acme.desk.client
    lists = [
        path
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1/")
        and "{" not in path
        and "get" in operations
        and not path.startswith("/api/v1/visitor/")
    ]
    leaks = {}
    for path in lists:
        for headers in (acme.desk.admin, acme.agent.headers):
            response = await call(client, "GET", path, headers)
            found = [
                key
                for key, id_ in globex.ids.items()
                if key not in NOT_IDS and id_ in response.text
            ]
            if found:
                leaks[path] = found
    filtered = await call(
        client, "GET", "/api/v1/sessions", acme.desk.admin, customer_id=globex.ids["customer_id"]
    )

    assert leaks == {}
    assert len(lists) >= 15
    assert filtered.json()["total"] == 0


async def test_agents_only_reach_their_own_sessions_and_customers(
    tenants: tuple[Tenant, Tenant],
) -> None:
    acme, _ = tenants
    desk = acme.desk
    # Dave 另外接待一位访客：Carol 看不到、改不了 Dave 的会话和客户。
    await desk.set_status(acme.agent, "away")
    visitor = await desk.visitor()
    await desk.say(visitor, "找 Dave")
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == acme.other_agent.staff_id
    dave_reply = await desk.client.post(
        "/api/v1/quick-replies",
        headers=acme.other_agent.headers,
        json={"title": "私人", "content": "只给自己用"},
    )
    dave_ids = {
        "customer_id": str(chat["customer_id"]),
        "room_id": str(chat["room_id"]),
        "session_id": str(chat["id"]),
        "reply_id": dave_reply.json()["id"],
        "staff_id": str(acme.other_agent.staff_id),
        "channel_id": acme.ids["channel_id"],
        "ticket_id": acme.ids["ticket_id"],
        "transfer_id": acme.ids["transfer_id"],
        "group_id": acme.ids["group_id"],
        "policy_id": acme.ids["policy_id"],
        "item_id": acme.ids["item_id"],
        "broadcast_id": acme.ids["broadcast_id"],
        "way_id": acme.ids["way_id"],
        "export_id": acme.ids["export_id"],
        "grant_id": acme.ids["grant_id"],
        "draft_id": await lead_draft(desk, chat),
        "space_id": acme.ids["space_id"],
        "category_id": acme.ids["category_id"],
        "job_id": acme.ids["job_id"],
        "notification_id": await notification(desk, acme.other_agent.staff_id),
    }
    before = await snapshot(desk)

    results = {}
    for method, template, body in MATRIX:
        # 待确认的转接是 Carol 自己发起的，她可以撤回；这里只验证她不能替 Dave 接受或拒绝。
        if template.endswith("/cancel"):
            continue
        path = fill(template, dave_ids, acme.ids)
        response = await call(
            desk.client, method, path, acme.agent.headers, fill(body, dave_ids, acme.ids)
        )
        results[f"{method} {template}"] = response.status_code
    await desk.flush()

    assert {k: code for k, code in results.items() if code not in DENIED} == {}
    assert await snapshot(desk) == before
