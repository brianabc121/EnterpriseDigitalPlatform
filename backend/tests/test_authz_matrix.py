"""越权矩阵（P1 验收"越权测试全部通过"）：跨租户、跨坐席访问一律拒绝，且不改变对方数据。

- 跨租户：租户 A 的管理员拿租户 B 的对象 ID 调用每一个带路径参数的接口（覆盖检查保证新增接口
  必须加入矩阵），以及把 B 的 ID 放进请求体、查询参数；列表接口的响应里不能出现 B 的 ID。
- 跨坐席：坐席只能访问自己接待的会话和自己的客户，管理类接口一律 403。
"""

import base64
import json
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
NOT_IDS = {"version", "userid", "order_version", "record_type"}

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
    ("GET", "/api/v1/sessions/{session_id}/intent", None),
    ("GET", "/api/v1/sessions/{session_id}/summary", None),
    ("POST", "/api/v1/sessions/{session_id}/summary", None),
    ("POST", "/api/v1/sessions/{session_id}/summary/confirm", {"summary": "越权确认"}),
    ("POST", "/api/v1/sessions/{session_id}/summary/discard", None),
    ("POST", "/api/v1/sessions/{session_id}/close", None),
    ("POST", "/api/v1/sessions/{session_id}/messages", {"client_msg_id": "x" * 16, "text": "越权"}),
    ("POST", "/api/v1/sessions/{session_id}/transfer", {"to_staff_id": "{own_staff_id}"}),
    ("GET", "/api/v1/todos/{todo_id}", None),
    ("PATCH", "/api/v1/todos/{todo_id}", {"title": "越权修改"}),
    ("POST", "/api/v1/todos/{todo_id}/reveal", None),
    ("POST", "/api/v1/todos/{todo_id}/confirm", {}),
    ("POST", "/api/v1/todos/{todo_id}/discard", {"reason": "other"}),
    ("POST", "/api/v1/todos/{todo_id}/merge", {"target_id": "{own_todo_id}"}),
    ("POST", "/api/v1/todos/{todo_id}/claim", None),
    ("POST", "/api/v1/todos/{todo_id}/start", None),
    ("POST", "/api/v1/todos/{todo_id}/wait", {}),
    ("POST", "/api/v1/todos/{todo_id}/resume", None),
    ("POST", "/api/v1/todos/{todo_id}/done", {"result": "越权完成"}),
    ("POST", "/api/v1/todos/{todo_id}/cancel", {"reason": "越权取消"}),
    ("POST", "/api/v1/todos/{todo_id}/reopen", {"reason": "越权"}),
    ("POST", "/api/v1/todos/{todo_id}/assign", {"assignee_id": "{own_staff_id}"}),
    (
        "POST",
        "/api/v1/todos/{todo_id}/reschedule",
        {"due_at": "2099-01-01T10:00:00+08:00", "reason": "越权改期"},
    ),
    ("POST", "/api/v1/todos/{todo_id}/comments", {"text": "越权评论"}),
    ("POST", "/api/v1/todos/{todo_id}/notify", {"text": "越权通知"}),
    ("PUT", "/api/v1/admin/todo-types/{type_id}", {"code": "other", "name": "越权修改"}),
    ("DELETE", "/api/v1/admin/todo-types/{type_id}", None),
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
    ("GET", "/api/v1/orders/{order_id}", None),
    ("PATCH", "/api/v1/orders/{order_id}", {"version": "{order_version}", "customer_note": "越权"}),
    ("POST", "/api/v1/orders/{order_id}/submit", None),
    ("POST", "/api/v1/orders/{order_id}/confirm", {"payment_method": "cod"}),
    ("POST", "/api/v1/orders/{order_id}/start", None),
    ("POST", "/api/v1/orders/{order_id}/ship", {"shipping_company": "越权", "tracking_no": "1"}),
    ("POST", "/api/v1/orders/{order_id}/complete", {}),
    ("POST", "/api/v1/orders/{order_id}/cancel", {"reason": "越权取消"}),
    ("POST", "/api/v1/orders/{order_id}/payments", {"amount": "1", "channel": "cash"}),
    ("POST", "/api/v1/orders/{order_id}/payments/{payment_id}/void", {"reason": "越权作废"}),
    ("POST", "/api/v1/orders/{order_id}/reveal", None),
    ("POST", "/api/v1/orders/{order_id}/tracking-link", None),
    ("POST", "/api/v1/orders/{order_id}/assign", {"assignee_id": "{own_staff_id}"}),
    ("POST", "/api/v1/orders/{order_id}/notify", {"text": "越权通知"}),
    ("GET", "/api/v1/orders/{order_id}/revisions", None),
    ("GET", "/api/v1/orders/{order_id}/revisions/{version}", None),
    ("POST", "/api/v1/orders/{order_id}/items/{order_item_id}/restock", None),
    ("GET", "/api/v1/production/orders/{order_id}", None),
    ("POST", "/api/v1/finance/receivables/{order_id}/followup", {"note": "越权跟进"}),
    ("POST", "/api/v1/finance/receivables/{order_id}/collect", {}),
    ("GET", "/api/v1/finance/customers/{customer_id}/statement", None),
    # 云打印机（§29）。
    (
        "PUT",
        "/api/v1/print/printers/{printer_id}",
        {"name": "x", "brand": "xpyun", "account": "dev@example.com", "sn": "XPY0001"},
    ),
    ("DELETE", "/api/v1/print/printers/{printer_id}", None),
    ("POST", "/api/v1/print/printers/{printer_id}/check", None),
    ("POST", "/api/v1/print/printers/{printer_id}/test", None),
    ("POST", "/api/v1/print/jobs/{print_job_id}/resend", None),
    ("POST", "/api/v1/print/orders/{order_id}", None),
    ("POST", "/api/v1/print/documents/{document_id}", None),
    # 盈利报表的收支登记（§30）。
    (
        "PUT",
        "/api/v1/profit/entries/{profit_entry_id}",
        {"kind": "expense", "category": "越权修改", "amount": "1", "occurred_on": "2026-01-01"},
    ),
    ("DELETE", "/api/v1/profit/entries/{profit_entry_id}", None),
    # AI 唤醒发现的问题（§33.4）：负责人或管理员才能忽略、标记已处理。
    ("POST", "/api/v1/wake/findings/{finding_id}/ignore", {"days": 7}),
    ("POST", "/api/v1/wake/findings/{finding_id}/resolve", {}),
    # 合同（§34.6）：分类由有管理权限的员工维护；合同只有负责人、创建人和管理员能看到。
    ("PATCH", "/api/v1/contracts/categories/{contract_category_id}", {"name": "越权修改"}),
    ("DELETE", "/api/v1/contracts/categories/{contract_category_id}", None),
    ("GET", "/api/v1/contracts/templates/{contract_template_id}", None),
    ("PATCH", "/api/v1/contracts/templates/{contract_template_id}", {"name": "越权修改"}),
    ("DELETE", "/api/v1/contracts/templates/{contract_template_id}", None),
    ("GET", "/api/v1/contracts/templates/{contract_template_id}/file", None),
    ("GET", "/api/v1/contracts/{contract_id}", None),
    ("PATCH", "/api/v1/contracts/{contract_id}", {"title": "越权修改"}),
    ("DELETE", "/api/v1/contracts/{contract_id}", None),
    ("POST", "/api/v1/contracts/{contract_id}/finalize", None),
    ("POST", "/api/v1/contracts/{contract_id}/reopen", None),
    ("POST", "/api/v1/contracts/{contract_id}/sign", {"sign_date": "2026-10-01"}),
    ("POST", "/api/v1/contracts/{contract_id}/void", {"reason": "越权作废"}),
    ("POST", "/api/v1/contracts/{contract_id}/save-as-template", {"name": "越权另存"}),
    ("GET", "/api/v1/contracts/{contract_id}/docx", None),
    ("GET", "/api/v1/contracts/{contract_id}/scan", None),
    ("GET", "/api/v1/prospects/customer/{customer_id}", None),
    ("GET", "/api/v1/prospects/{prospect_id}", None),
    ("PATCH", "/api/v1/prospects/{prospect_id}", {"level": "low"}),
    ("POST", "/api/v1/prospects/{prospect_id}/followups", {"content": "越权跟进"}),
    ("POST", "/api/v1/prospects/{prospect_id}/won", None),
    ("POST", "/api/v1/prospects/{prospect_id}/lost", {"reason": "越权放弃"}),
    ("POST", "/api/v1/prospects/{prospect_id}/reopen", None),
    ("POST", "/api/v1/prospects/{prospect_id}/accept", None),
    ("POST", "/api/v1/prospects/{prospect_id}/dismiss", None),
    ("POST", "/api/v1/prospects/{prospect_id}/message", None),
    ("PATCH", "/api/v1/materials/folders/{material_folder_id}", {"name": "越权修改"}),
    ("DELETE", "/api/v1/materials/folders/{material_folder_id}", None),
    ("DELETE", "/api/v1/materials/shares/{material_share_id}", None),
    ("GET", "/api/v1/materials/{material_id}", None),
    ("PATCH", "/api/v1/materials/{material_id}", {"name": "越权修改"}),
    ("DELETE", "/api/v1/materials/{material_id}", None),
    ("POST", "/api/v1/materials/{material_id}/parts", {"part_numbers": [1]}),
    ("POST", "/api/v1/materials/{material_id}/complete", {}),
    ("DELETE", "/api/v1/materials/{material_id}/upload", None),
    ("GET", "/api/v1/materials/{material_id}/text", None),
    ("PUT", "/api/v1/materials/{material_id}/text", {"body": "越权修改"}),
    ("GET", "/api/v1/materials/{material_id}/link", None),
    ("POST", "/api/v1/materials/{material_id}/knowledge", {}),
    ("GET", "/api/v1/materials/{material_id}/shares", None),
    ("POST", "/api/v1/materials/{material_id}/shares", {"days": 1}),
    ("POST", "/api/v1/production/orders/{order_id}/claim", None),
    ("POST", "/api/v1/production/orders/{order_id}/release", None),
    ("POST", "/api/v1/production/orders/{order_id}/items/{order_item_id}/done", None),
    ("POST", "/api/v1/production/orders/{order_id}/items/{order_item_id}/undo", None),
    ("PUT", "/api/v1/production/orders/{order_id}/items/{order_item_id}/shortage", {}),
    ("POST", "/api/v1/production/orders/{order_id}/items/{order_item_id}/restock", None),
    ("POST", "/api/v1/production/orders/{order_id}/complete", {"mark_all": True}),
    ("POST", "/api/v1/production/orders/{order_id}/assign", {"worker_id": "{own_staff_id}"}),
    ("GET", "/api/v1/products/{product_id}", None),
    ("PUT", "/api/v1/products/{product_id}", {"name": "越权修改"}),
    ("DELETE", "/api/v1/products/{product_id}", None),
    ("POST", "/api/v1/products/{product_id}/stock", {"mode": "set", "quantity": 1}),
    ("GET", "/api/v1/products/{product_id}/stock-movements", None),
    ("GET", "/api/v1/products/{product_id}/materials", None),
    ("GET", "/api/v1/products/{product_id}/materials/history", None),
    ("PUT", "/api/v1/products/{product_id}/materials", {"items": []}),
    ("GET", "/api/v1/form-kb/entries/{entry_id}", None),
    ("PUT", "/api/v1/form-kb/entries/{entry_id}", {"text": "越权改的叫法"}),
    ("DELETE", "/api/v1/form-kb/entries/{entry_id}", None),
    ("POST", "/api/v1/form-kb/entries/{entry_id}/enable", None),
    ("POST", "/api/v1/form-kb/entries/{entry_id}/disable", None),
    ("POST", "/api/v1/form-kb/entries/{entry_id}/lock", None),
    ("POST", "/api/v1/form-kb/entries/{entry_id}/unlock", None),
    ("POST", "/api/v1/form-kb/entries/{entry_id}/confirm", {"decision": "keep"}),
    ("POST", "/api/v1/form-kb/entries/{entry_id}/apply-recipe", None),
    ("GET", "/api/v1/warehouse/documents/{document_id}", None),
    (
        "PUT",
        "/api/v1/warehouse/documents/{document_id}",
        {"lines": [{"product_id": "{own_raw_material_id}", "quantity": 1}]},
    ),
    ("POST", "/api/v1/warehouse/documents/{document_id}/confirm", None),
    ("POST", "/api/v1/warehouse/documents/{document_id}/reject", {"reason": "越权退回"}),
    ("POST", "/api/v1/warehouse/documents/{document_id}/void", None),
    ("GET", "/api/v1/history/{record_type}/{record_id}", None),
    ("GET", "/api/v1/products/imports/{import_id}", None),
    ("POST", "/api/v1/products/imports/{import_id}/confirm", None),
    ("POST", "/api/v1/products/imports/{import_id}/cancel", None),
    ("GET", "/api/v1/products/imports/{import_id}/result", None),
    ("POST", "/api/v1/products/gaps/{gap_id}/resolve", None),
    ("POST", "/api/v1/admin/api-keys/{key_id}/revoke", None),
    (
        "PUT",
        "/api/v1/admin/webhooks/{endpoint_id}",
        {"name": "越权修改", "url": "http://erp.example/x", "events": ["order.created"]},
    ),
    ("DELETE", "/api/v1/admin/webhooks/{endpoint_id}", None),
    ("POST", "/api/v1/admin/webhooks/{endpoint_id}/rotate-secret", None),
    ("POST", "/api/v1/admin/webhooks/{endpoint_id}/test", None),
    ("GET", "/api/v1/admin/webhook-deliveries/{delivery_id}", None),
    ("POST", "/api/v1/admin/webhook-deliveries/{delivery_id}/resend", None),
    ("POST", "/api/v1/sessions/{session_id}/read", None),
    ("POST", "/api/v1/sessions/{session_id}/typing", None),
    ("GET", "/api/v1/mail/accounts/{account_id}", None),
    (
        "PUT",
        "/api/v1/mail/accounts/{account_id}",
        {"name": "越权修改", "address": "evil@example.com", "provider": "netease163"},
    ),
    ("POST", "/api/v1/mail/accounts/{account_id}/fetch", None),
    ("POST", "/api/v1/mail/accounts/{account_id}/enable", None),
    ("POST", "/api/v1/mail/accounts/{account_id}/disable", None),
    ("GET", "/api/v1/mail/messages/{message_id}/original", None),
    ("GET", "/api/v1/tasks/{task_id}", None),
    ("PATCH", "/api/v1/tasks/{task_id}", {"title": "越权修改"}),
    ("POST", "/api/v1/tasks/{task_id}/done", None),
    ("POST", "/api/v1/tasks/{task_id}/reopen", None),
    ("POST", "/api/v1/tasks/{task_id}/cancel", None),
    ("GET", "/api/v1/assistant/bots/{bot_id}", None),
    ("PUT", "/api/v1/assistant/bots/{bot_id}", {"provider": "wecom", "name": "越权修改"}),
    ("DELETE", "/api/v1/assistant/bots/{bot_id}", None),
    ("POST", "/api/v1/assistant/bots/{bot_id}/enable", None),
    ("POST", "/api/v1/assistant/bots/{bot_id}/disable", None),
    ("POST", "/api/v1/assistant/bots/{bot_id}/rotate-token", None),
    ("POST", "/api/v1/assistant/bots/{bot_id}/test", None),
    ("PUT", "/api/v1/assistant/identities/{identity_id}", {"staff_id": "{own_staff_id}"}),
    ("DELETE", "/api/v1/assistant/identities/{identity_id}", None),
    ("DELETE", "/api/v1/assistant/bindings/{identity_id}", None),
    ("PATCH", "/api/v1/assistant/groups/{assistant_group_id}", {"name": "越权修改"}),
    ("GET", "/api/v1/assistant/groups/{assistant_group_id}/messages", None),
    ("POST", "/api/v1/assistant/groups/{assistant_group_id}/extract", None),
    ("POST", "/api/v1/assistant/groups/{assistant_group_id}/clear", None),
]
# 凭随机令牌访问的公开接口（订单跟踪页）：令牌本身就是访问凭证，不属于租户内的越权检查，
# 令牌的有效期和失效见 test_orders.py。
PUBLIC: set[tuple[str, str]] = {
    ("GET", "/api/v1/public/orders/{token}"),
    # 资料的分享页（令牌的有效期和停用见 test_materials.py）。
    ("GET", "/api/v1/public/materials/{token}"),
}


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
    [todo] = await desk.sql("SELECT id FROM todos WHERE tenant_id = $1", desk.tenant_id)
    [todo_type] = await desk.sql(
        "SELECT id FROM todo_types WHERE tenant_id = $1 AND code = 'other'", desk.tenant_id
    )
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
    order_ids = await orders(desk, chat)
    mail_ids = await mail(desk, chat)
    assistant_ids = await assistant(desk, agent.staff_id)
    # 云打印机和一条放弃的打印任务（直接写库，流程见 test_print.py）。
    [printer] = await desk.sql(
        "INSERT INTO printers (id, tenant_id, name, brand, account, key_enc, sn)"
        " VALUES ($1, $2, '车间打印机', 'xpyun', 'dev@example.com', 'sealed', $3) RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        f"XPY{desk.code.upper()}",
    )
    [print_job] = await desk.sql(
        "INSERT INTO print_jobs (id, tenant_id, printer_id, printer_name, kind, source, status)"
        " VALUES ($1, $2, $3, '车间打印机', 'test', 'test', 'dead') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        printer["id"],
    )
    # 一笔费用（盈利报表的收支登记，§30）。
    [profit_entry] = await desk.sql(
        "INSERT INTO profit_entries (id, tenant_id, kind, category, amount, occurred_on)"
        " VALUES ($1, $2, 'expense', '房租物业', 3000, current_date) RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
    )
    # AI 唤醒发现的一个问题，负责人是管理员（坐席不是负责人）。
    [finding] = await desk.sql(
        "INSERT INTO wake_findings (id, tenant_id, check_code, fingerprint, category, severity,"
        " title, assignee_ids) SELECT $1, $2, 'stock_low', $3, 'warehouse', 'warning',"
        " '库存不足', array_agg(id) FROM staff WHERE tenant_id = $2 AND username = 'admin'"
        " RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        f"stock_low:product:{uuid.uuid4()}",
    )
    # 合同的分类、模板和一份管理员负责的合同草稿，带扫描件（直接写库，流程见 test_contracts.py）。
    [contract_category] = await desk.sql(
        "INSERT INTO contract_categories (id, tenant_id, name) VALUES ($1, $2, '销售合同')"
        " RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
    )
    [contract_template] = await desk.sql(
        "INSERT INTO contract_templates (id, tenant_id, name, body, file_key, file_name)"
        " VALUES ($1, $2, '销售模板', '# 销售合同', $3, '销售模板.docx') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        f"{desk.code}/_contract_templates/x/销售模板.docx",
    )
    [contract] = await desk.sql(
        "INSERT INTO contracts (id, tenant_id, no, title, body, owner_id, created_by,"
        " scan_key, scan_name) SELECT $1, $2, 'HT20261001-0001', '销售合同', '# 销售合同',"
        " id, id, $3, '签字版.pdf' FROM staff WHERE tenant_id = $2 AND username = 'admin'"
        " RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        f"{desk.code}/_contracts/x/签字版.pdf",
    )
    # 坐席客户的意向记录（直接写库，流程见 test_prospects.py）。
    prospect = await prospect_of(desk, chat["customer_id"])
    material_ids = await materials(desk)
    await desk.flush()
    ids = {
        "prospect_id": prospect,
        **material_ids,
        "contract_category_id": str(contract_category["id"]),
        "contract_template_id": str(contract_template["id"]),
        "contract_id": str(contract["id"]),
        "finding_id": str(finding["id"]),
        "printer_id": str(printer["id"]),
        "print_job_id": str(print_job["id"]),
        "profit_entry_id": str(profit_entry["id"]),
        **order_ids,
        **mail_ids,
        **assistant_ids,
        "customer_id": str(chat["customer_id"]),
        "staff_id": str(agent.staff_id),
        "channel_id": channel["id"],
        "room_id": str(chat["room_id"]),
        "session_id": str(chat["id"]),
        "todo_id": str(todo["id"]),
        "type_id": str(todo_type["id"]),
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


async def materials(desk: Desk) -> dict[str, str]:
    """管理员建的资料文件夹、里面的一份文字资料（文件在模拟 OSS 上）和它的分享链接。"""
    client = desk.client
    folder = await client.post(
        "/api/v1/materials/folders", headers=desk.admin, json={"name": "产品资料"}
    )
    assert folder.status_code == 201, folder.text
    text = await client.post(
        "/api/v1/materials/texts",
        headers=desk.admin,
        json={"name": "安装说明", "body": "# 安装说明", "folder_id": folder.json()["id"]},
    )
    assert text.status_code == 201, text.text
    share = await client.post(
        f"/api/v1/materials/{text.json()['id']}/shares", headers=desk.admin, json={"days": 7}
    )
    assert share.status_code == 201, share.text
    return {
        "material_folder_id": folder.json()["id"],
        "material_id": text.json()["id"],
        "material_share_id": share.json()["id"],
    }


async def prospect_of(desk: Desk, customer_id: uuid.UUID) -> str:
    """客户跟进中的意向记录，带一条跟进记录。"""
    [prospect] = await desk.sql(
        "INSERT INTO customer_prospects (id, tenant_id, customer_id, interest, next_follow_at)"
        " VALUES ($1, $2, $3, '智能门锁', current_date + 3) RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        customer_id,
    )
    await desk.sql(
        "INSERT INTO prospect_followups (id, tenant_id, prospect_id, method, content)"
        " VALUES ($1, $2, $3, 'phone', '电话沟通')",
        uuid.uuid4(),
        desk.tenant_id,
        prospect["id"],
    )
    return str(prospect["id"])


async def orders(desk: Desk, chat: Any) -> dict[str, str]:
    """商品、已确认的订单（带一笔收款、货到付款，在待领取加工的列表里）、商品导入预览和商品缺口。"""
    client = desk.client
    product = await client.post(
        "/api/v1/products",
        headers=desk.admin,
        json={"code": "LOCK-X1", "name": "智能门锁", "retail_price": "1299", "cost_price": "800"},
    )
    assert product.status_code == 201, product.text
    order = await client.post(
        "/api/v1/orders",
        headers=desk.admin,
        json={
            "customer_id": str(chat["customer_id"]),
            "session_id": str(chat["id"]),
            "items": [{"product_id": product.json()["id"], "quantity": 1}],
            "receiver": {"name": "王先生", "phone": "13800001111", "address": "上海市浦东新区"},
        },
    )
    assert order.status_code == 201, order.text
    paid = await client.post(
        f"/api/v1/orders/{order.json()['id']}/payments",
        headers=desk.admin,
        json={"amount": "100", "channel": "wechat"},
    )
    assert paid.status_code == 200, paid.text
    confirmed = await client.post(
        f"/api/v1/orders/{order.json()['id']}/confirm",
        headers=desk.admin,
        json={"payment_method": "cod", "notify_customer": False},
    )
    assert confirmed.status_code == 200, confirmed.text
    upload = await client.post(
        "/api/v1/products/imports",
        headers=desk.admin,
        json={
            "filename": "商品.csv",
            "content_base64": base64.b64encode("名称,建议零售价\n门铃,199\n".encode()).decode(),
        },
    )
    assert upload.status_code == 201, upload.text
    [gap] = await desk.sql(
        "INSERT INTO product_gaps (id, tenant_id, term) VALUES ($1, $2, '扫地机器人') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
    )
    # 企业系统对接：接口密钥、推送地址和一条推送记录（测试推送）。
    key = await client.post(
        "/api/v1/admin/api-keys",
        headers=desk.admin,
        json={"name": "ERP", "scopes": ["orders:read"]},
    )
    assert key.status_code == 201, key.text
    endpoint = await client.post(
        "/api/v1/admin/webhooks",
        headers=desk.admin,
        json={"name": "ERP", "url": "http://erp.example/hooks", "events": ["order.created"]},
    )
    assert endpoint.status_code == 201, endpoint.text
    ping = await client.post(
        f"/api/v1/admin/webhooks/{endpoint.json()['id']}/test", headers=desk.admin
    )
    assert ping.status_code == 200, ping.text
    [delivery] = await desk.sql(
        "SELECT id FROM webhook_deliveries WHERE tenant_id = $1", desk.tenant_id
    )
    # 仓库：一个材料和一张待确认的领料单（管理员开的单会直接确认，这里直接写入）。
    material = await client.post(
        "/api/v1/products",
        headers=desk.admin,
        json={"code": "AL-6063", "name": "铝合金型材", "kind": "material", "unit": "米"},
    )
    assert material.status_code == 201, material.text
    document_id = uuid.uuid4()
    await desk.sql(
        "INSERT INTO stock_documents (id, tenant_id, kind, no, status)"
        " VALUES ($1, $2, 'requisition', 'LL20260930-0001', 'pending')",
        document_id,
        desk.tenant_id,
    )
    await desk.sql(
        "INSERT INTO stock_document_lines (id, tenant_id, document_id, product_id, name, quantity)"
        " VALUES ($1, $2, $3, $4, '铝合金型材', 1)",
        uuid.uuid4(),
        desk.tenant_id,
        document_id,
        uuid.UUID(material.json()["id"]),
    )
    # 表单知识：一条手工添加的叫法（§25.18）。
    entry = await client.post(
        "/api/v1/form-kb/entries",
        headers=desk.admin,
        json={"kind": "alias", "text": "大窗", "product_id": product.json()["id"]},
    )
    assert entry.status_code == 201, entry.text
    return {
        "entry_id": entry.json()["id"],
        "key_id": key.json()["id"],
        "endpoint_id": endpoint.json()["id"],
        "delivery_id": str(delivery["id"]),
        "product_id": product.json()["id"],
        "order_id": order.json()["id"],
        "order_item_id": paid.json()["items"][0]["id"],
        "payment_id": paid.json()["payments"][0]["id"],
        "import_id": upload.json()["id"],
        "gap_id": str(gap["id"]),
        "order_version": str(confirmed.json()["order"]["version"]),
        "raw_material_id": material.json()["id"],
        "document_id": str(document_id),
        # 修改历史：用订单的历史（订单是通过接口建的，有版本）。
        "record_type": "order",
        "record_id": order.json()["id"],
    }


async def mail(desk: Desk, chat: Any) -> dict[str, str]:
    """一个邮箱和会话里的一封客户邮件（直接写库，收信和回复见 test_mail.py）。"""
    channel_id = uuid.uuid4()
    await desk.sql(
        "INSERT INTO channel_accounts (id, tenant_id, type, name, public_key)"
        " VALUES ($1, $2, 'email', '售后邮箱', $3)",
        channel_id,
        desk.tenant_id,
        f"pk_mail_{desk.code}",
    )
    [account] = await desk.sql(
        "INSERT INTO mail_accounts (id, tenant_id, channel_account_id, address, provider,"
        " imap_host, imap_port, imap_security, smtp_host, smtp_port, smtp_security, username,"
        " secret_enc) VALUES ($1, $2, $3, $4, 'netease163', 'imap.163.com', 993, 'ssl',"
        " 'smtp.163.com', 465, 'ssl', $4, 'sealed') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        channel_id,
        f"support@{desk.code}.example.com",
    )
    return {"account_id": str(account["id"]), "message_id": await email(desk, chat)}


async def assistant(desk: Desk, staff_id: uuid.UUID) -> dict[str, str]:
    """个人待办、AI 助理的机器人、一个已绑定的 IM 账号和一个记录的群（直接写库，流程见
    test_tasks.py、test_assistant.py）。"""
    [task] = await desk.sql(
        "INSERT INTO staff_tasks (id, tenant_id, no, owner_id, title)"
        " VALUES ($1, $2, 'T20261002-0001', $3, '盘点仓库') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        staff_id,
    )
    [bot] = await desk.sql(
        "INSERT INTO assistant_bots (id, tenant_id, provider, name, webhook_token)"
        " VALUES ($1, $2, 'wecom', '小助', $3) RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        f"token-{desk.code}",
    )
    [identity] = await desk.sql(
        "INSERT INTO assistant_identities (id, tenant_id, bot_id, external_user_id, staff_id,"
        " bound_at) VALUES ($1, $2, $3, 'zhangsan', $4, now()) RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        bot["id"],
        staff_id,
    )
    [group] = await desk.sql(
        "INSERT INTO assistant_groups (id, tenant_id, bot_id, external_chat_id, name)"
        " VALUES ($1, $2, $3, 'chat-1', '售后群') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        bot["id"],
    )
    return {
        "task_id": str(task["id"]),
        "bot_id": str(bot["id"]),
        "identity_id": str(identity["id"]),
        "assistant_group_id": str(group["id"]),
    }


async def dave_assistant(desk: Desk, staff_id: uuid.UUID, bot_id: str) -> dict[str, str]:
    [task] = await desk.sql(
        "INSERT INTO staff_tasks (id, tenant_id, no, owner_id, title)"
        " VALUES ($1, $2, 'T20261002-0002', $3, 'Dave 的事') RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        staff_id,
    )
    [identity] = await desk.sql(
        "INSERT INTO assistant_identities (id, tenant_id, bot_id, external_user_id, staff_id,"
        " bound_at) VALUES ($1, $2, $3, 'dave', $4, now()) RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        uuid.UUID(bot_id),
        staff_id,
    )
    return {"task_id": str(task["id"]), "identity_id": str(identity["id"])}


async def email(desk: Desk, chat: Any) -> str:
    [row] = await desk.sql(
        "INSERT INTO messages (id, tenant_id, room_id, channel_account_id, session_id, direction,"
        " sender_type, content_type, content, text_plain, source, sent_at)"
        " VALUES ($1, $2, $3, $4, $5, 'in', 'customer', 'email', $6, '询价', 'channel', now())"
        " RETURNING id",
        uuid.uuid4(),
        desk.tenant_id,
        chat["room_id"],
        chat["channel_account_id"],
        chat["id"],
        json.dumps({"subject": "询价", "text": "请报价"}),
    )
    return str(row["id"])


async def first_item(desk: Desk, order_id: str) -> str:
    [item] = await desk.sql(
        "SELECT id FROM order_items WHERE order_id = $1 ORDER BY sort LIMIT 1", uuid.UUID(order_id)
    )
    return str(item["id"])


async def dave_order(desk: Desk, chat: Any, product_id: str) -> str:
    response = await desk.client.post(
        "/api/v1/orders",
        headers=desk.admin,
        json={
            "customer_id": str(chat["customer_id"]),
            "session_id": str(chat["id"]),
            "items": [{"product_id": product_id, "quantity": 1}],
            "receiver": {"name": "李女士", "phone": "13900002222", "address": "北京市朝阳区"},
        },
    )
    assert response.status_code == 201, response.text
    order_id: str = response.json()["id"]
    return order_id


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
        "sessions": "id, status, assignee_id, closed_at, read_at",
        "customers": "id, owner_id, notes, display_name",
        "channel_accounts": "id, name, config",
        "session_transfers": "id, status",
        "todos": "id, status, title, assignee_id, due_at",
        "todo_types": "id, name, enabled",
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
        "products": "id, name, status, retail_price, cost_price",
        "product_imports": "id, status",
        "product_gaps": "id, resolved_at",
        "orders": "id, status, version, total, assignee_id, customer_note, tracking_token",
        "order_items": "id, product_id, quantity, unit_price",
        "order_payments": "id, voided_at",
        "api_keys": "id, name, revoked_at",
        "webhook_endpoints": "id, name, url, secret_enc, enabled, events",
        "webhook_deliveries": "id, status, attempts",
        "product_materials": "id, product_id, material_id, quantity",
        "stock_documents": "id, status, note",
        "stock_document_lines": "id, quantity",
        "record_versions": "id, seq, action",
        "form_kb_entries": "id, status, text, value, locked",
        "mail_accounts": "id, status, address, display_name, secret_enc, last_uid, failures",
        "staff_tasks": "id, status, title, owner_id, due_at",
        "assistant_bots": "id, name, status, webhook_token, secrets_enc, config",
        "assistant_identities": "id, staff_id, bound_at",
        "assistant_groups": "id, name, recording, extract, message_count",
        "assistant_group_messages": "id, extracted_at",
        "profit_entries": "id, kind, category, amount, occurred_on, note, recurring",
        "wake_findings": "id, status, ignored_until, resolved_at, resolve_note",
        "contract_categories": "id, name, parent_id, sort",
        "contract_templates": "id, name, status, body, used_count",
        "contracts": "id, status, title, owner_id, body, void_reason",
        "customer_prospects": "id, status, level, follower_id, next_follow_at, follow_count",
        "prospect_followups": "id, prospect_id, content",
        "material_folders": "id, name, parent_id, sort",
        "materials": "id, name, description, folder_id, status, tags, size, excerpt",
        "material_shares": "id, material_id, expires_at, disabled_at",
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
    assert routes - PUBLIC == {(method, path) for method, path, _ in MATRIX}


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
        ("POST", "/api/v1/todos", {"type_id": other["type_id"], "title": "x"}),
        (
            "POST",
            "/api/v1/todos",
            {"type_id": own["type_id"], "title": "x", "customer_id": other["customer_id"]},
        ),
        (
            "POST",
            "/api/v1/todos",
            {"type_id": own["type_id"], "title": "x", "session_id": other["session_id"]},
        ),
        (
            "POST",
            "/api/v1/todos",
            {"type_id": own["type_id"], "title": "x", "assignee_id": other["staff_id"]},
        ),
        (
            "POST",
            "/api/v1/todos",
            {"type_id": own["type_id"], "title": "x", "skill_group_id": other["group_id"]},
        ),
        (
            "POST",
            f"/api/v1/todos/{own['todo_id']}/assign",
            {"assignee_id": other["staff_id"]},
        ),
        (
            "POST",
            f"/api/v1/todos/{own['todo_id']}/assign",
            {"skill_group_id": other["group_id"]},
        ),
        (
            "POST",
            "/api/v1/todos/extract",
            {"session_id": other["session_id"], "message_ids": [other["session_id"]]},
        ),
        ("POST", "/api/v1/orders/extract", {"session_id": other["session_id"]}),
        (
            "POST",
            f"/api/v1/production/orders/{own['order_id']}/assign",
            {"worker_id": other["staff_id"]},
        ),
        (
            "PUT",
            f"/api/v1/admin/todo-types/{own['type_id']}",
            {
                "code": "other",
                "name": "x",
                "assign_rule": {"steps": ["skill_group"], "skill_group_id": other["group_id"]},
            },
        ),
        (
            "PUT",
            f"/api/v1/admin/todo-types/{own['type_id']}",
            {
                "code": "other",
                "name": "x",
                "assign_rule": {"steps": ["staff"], "staff_id": other["staff_id"]},
            },
        ),
    ]
    own_order = await acme.desk.client.get(
        f"/api/v1/orders/{own['order_id']}", headers=acme.desk.admin
    )
    version = own_order.json()["version"]
    vectors += [
        (
            "POST",
            "/api/v1/orders",
            {
                "customer_id": other["customer_id"],
                "items": [{"product_id": own["product_id"], "quantity": 1}],
            },
        ),
        (
            "POST",
            "/api/v1/orders",
            {
                "customer_id": own["customer_id"],
                "items": [{"product_id": other["product_id"], "quantity": 1}],
            },
        ),
        (
            "POST",
            "/api/v1/orders",
            {
                "customer_id": own["customer_id"],
                "session_id": other["session_id"],
                "items": [{"product_id": own["product_id"], "quantity": 1}],
            },
        ),
        (
            "PATCH",
            f"/api/v1/orders/{own['order_id']}",
            {
                "version": version,
                "items": [{"product_id": other["product_id"], "quantity": 1}],
                "reason": "other",
            },
        ),
        ("POST", f"/api/v1/orders/{own['order_id']}/assign", {"assignee_id": other["staff_id"]}),
        (
            "POST",
            f"/api/v1/orders/{own['order_id']}/assign",
            {"skill_group_id": other["group_id"]},
        ),
        # 表单知识（§25.18）：对方租户的商品、材料。
        (
            "POST",
            "/api/v1/form-kb/entries",
            {"kind": "alias", "text": "越权叫法", "product_id": other["product_id"]},
        ),
        (
            "POST",
            "/api/v1/form-kb/entries",
            {
                "kind": "usage",
                "product_id": own["product_id"],
                "related_id": other["raw_material_id"],
                "value": 1,
            },
        ),
        (
            "PUT",
            f"/api/v1/form-kb/entries/{own['entry_id']}",
            {"product_id": other["product_id"]},
        ),
        (
            "POST",
            "/api/v1/materials/folders",
            {"name": "x", "parent_id": other["material_folder_id"]},
        ),
        (
            "PATCH",
            f"/api/v1/materials/folders/{own['material_folder_id']}",
            {"parent_id": other["material_folder_id"]},
        ),
        (
            "POST",
            "/api/v1/materials/uploads",
            {"filename": "a.pdf", "size": 10, "folder_id": other["material_folder_id"]},
        ),
        (
            "POST",
            "/api/v1/materials/texts",
            {"name": "x", "body": "x", "folder_id": other["material_folder_id"]},
        ),
        (
            "PATCH",
            f"/api/v1/materials/{own['material_id']}",
            {"folder_id": other["material_folder_id"]},
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
    dave_order_id = await dave_order(desk, chat, acme.ids["product_id"])
    dave_ids = {
        "customer_id": str(chat["customer_id"]),
        "room_id": str(chat["room_id"]),
        "session_id": str(chat["id"]),
        "reply_id": dave_reply.json()["id"],
        "staff_id": str(acme.other_agent.staff_id),
        "channel_id": acme.ids["channel_id"],
        "todo_id": acme.ids["todo_id"],
        "type_id": acme.ids["type_id"],
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
        "order_id": dave_order_id,
        "order_item_id": await first_item(desk, dave_order_id),
        "payment_id": acme.ids["payment_id"],
        "order_version": "1",
        "version": "1",
        "product_id": acme.ids["product_id"],
        "import_id": acme.ids["import_id"],
        "gap_id": acme.ids["gap_id"],
        "key_id": acme.ids["key_id"],
        "endpoint_id": acme.ids["endpoint_id"],
        "delivery_id": acme.ids["delivery_id"],
        "raw_material_id": acme.ids["raw_material_id"],
        "document_id": acme.ids["document_id"],
        "entry_id": acme.ids["entry_id"],
        "record_type": "order",
        "record_id": dave_order_id,
        "account_id": acme.ids["account_id"],
        "printer_id": acme.ids["printer_id"],
        "print_job_id": acme.ids["print_job_id"],
        "profit_entry_id": acme.ids["profit_entry_id"],
        "finding_id": acme.ids["finding_id"],
        "contract_category_id": acme.ids["contract_category_id"],
        # 管理员上传的资料、建的文件夹和分享链接：坐席能看、能分享，不能修改和删除。
        "material_folder_id": acme.ids["material_folder_id"],
        "material_id": acme.ids["material_id"],
        "material_share_id": acme.ids["material_share_id"],
        "contract_template_id": acme.ids["contract_template_id"],
        "contract_id": acme.ids["contract_id"],
        "prospect_id": await prospect_of(desk, chat["customer_id"]),
        "message_id": await email(desk, chat),
        # Dave 自己的个人待办和 IM 绑定；机器人和群只有管理员能管理。
        **await dave_assistant(desk, acme.other_agent.staff_id, acme.ids["bot_id"]),
        "bot_id": acme.ids["bot_id"],
        "assistant_group_id": acme.ids["assistant_group_id"],
    }
    before = await snapshot(desk)

    results = {}
    for method, template, body in MATRIX:
        # 待确认的转接是 Carol 自己发起的，她可以撤回；这里只验证她不能替 Dave 接受或拒绝。
        if template.endswith("/cancel") and "/todos/" not in template:
            continue
        # 商品库是全租户共享的：能查看订单的坐席都可以查看商品（不含成本价）和它的库存记录。
        if (method, template) in (
            ("GET", "/api/v1/products/{product_id}"),
            ("GET", "/api/v1/products/{product_id}/stock-movements"),
            ("GET", "/api/v1/products/{product_id}/materials"),
            ("GET", "/api/v1/products/{product_id}/materials/history"),
            ("GET", "/api/v1/form-kb/entries/{entry_id}"),
            # 合同模板全员共享：能用合同的坐席都能查看模板、下载原件（修改只有创建人和管理员）。
            ("GET", "/api/v1/contracts/templates/{contract_template_id}"),
            ("GET", "/api/v1/contracts/templates/{contract_template_id}/file"),
            # 企业资料全员共享：能用资料的坐席都能查看、下载和分享（修改只有上传的人和管理员）。
            ("GET", "/api/v1/materials/{material_id}"),
            ("GET", "/api/v1/materials/{material_id}/text"),
            ("GET", "/api/v1/materials/{material_id}/link"),
            ("GET", "/api/v1/materials/{material_id}/shares"),
            ("POST", "/api/v1/materials/{material_id}/shares"),
        ):
            continue
        path = fill(template, dave_ids, acme.ids)
        response = await call(
            desk.client, method, path, acme.agent.headers, fill(body, dave_ids, acme.ids)
        )
        results[f"{method} {template}"] = response.status_code
    await desk.flush()

    assert {k: code for k, code in results.items() if code not in DENIED} == {}
    assert await snapshot(desk) == before
