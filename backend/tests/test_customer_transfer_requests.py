"""客户转移申请：坐席申请、管理员审批（设计文档 §14.1）。"""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from tests.desk import Agent, Desk
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


async def _customer(desk: Desk, agent: Agent, name: str) -> str:
    response = await desk.client.post(
        "/api/v1/customers", headers=agent.headers, json={"display_name": name}
    )
    assert response.status_code == 201, response.text
    customer_id: str = response.json()["id"]
    return customer_id


async def _request(desk: Desk, agent: Agent, customer_id: str, **body: Any) -> httpx.Response:
    return await desk.client.post(
        f"/api/v1/customers/{customer_id}/transfer-requests",
        headers=agent.headers,
        json={"reason": "客户换了对接人", **body},
    )


async def test_agent_requests_and_admin_approves(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)
    customer_id = await _customer(desk, alice, "王先生")

    created = await _request(desk, alice, customer_id, to_owner_id=str(bob.staff_id))
    assert created.status_code == 201, created.text
    request = created.json()
    assert (request["status"], request["from_owner_name"], request["to_owner_name"]) == (
        "pending",
        "Alice",
        "Bob",
    )
    duplicate = await _request(desk, alice, customer_id, to_owner_id=str(bob.staff_id))
    assert duplicate.status_code == 409

    mine = await desk.client.get("/api/v1/customers/transfer-requests", headers=alice.headers)
    assert (mine.json()["pending"], len(mine.json()["items"])) == (1, 1)
    others = await desk.client.get("/api/v1/customers/transfer-requests", headers=bob.headers)
    assert others.json()["items"] == []
    agent_approve = await desk.client.post(
        f"/api/v1/customers/transfer-requests/{request['id']}/approve",
        headers=bob.headers,
        json={},
    )
    assert agent_approve.status_code == 403

    admin_list = await desk.client.get(
        "/api/v1/customers/transfer-requests?status=pending", headers=desk.admin
    )
    assert [r["id"] for r in admin_list.json()["items"]] == [request["id"]]
    approved = await desk.client.post(
        f"/api/v1/customers/transfer-requests/{request['id']}/approve",
        headers=desk.admin,
        json={"note": "同意"},
    )
    assert approved.status_code == 200, approved.text
    assert (approved.json()["status"], approved.json()["decided_by_name"]) == ("approved", "管理员")
    customer = await desk.client.get(f"/api/v1/customers/{customer_id}", headers=desk.admin)
    assert customer.json()["owner_id"] == str(bob.staff_id)
    history = await desk.client.get(
        f"/api/v1/customers/{customer_id}/owner-history", headers=desk.admin
    )
    [latest] = [h for h in history.json()["items"] if h["reason"] == "request"]
    assert (latest["from_owner_name"], latest["to_owner_name"]) == ("Alice", "Bob")
    twice = await desk.client.post(
        f"/api/v1/customers/transfer-requests/{request['id']}/approve",
        headers=desk.admin,
        json={},
    )
    assert twice.status_code == 409
    actions = {
        r["action"]
        for r in await desk.sql("SELECT action FROM audit_logs WHERE action LIKE 'customer.%'")
    }
    assert {"customer.transfer_request", "customer.transfer_approve"} <= actions


async def test_reject_cancel_and_visibility(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)
    mine = await _customer(desk, alice, "李女士")
    bobs = await _customer(desk, bob, "赵先生")

    # 看不到的客户不能申请。
    hidden = await _request(desk, alice, bobs)
    assert hidden.status_code == 404
    # 已经归属于目标员工。
    same = await _request(desk, alice, mine)
    assert same.status_code == 422

    first = (await _request(desk, alice, mine, to_owner_id=str(bob.staff_id))).json()
    rejected = await desk.client.post(
        f"/api/v1/customers/transfer-requests/{first['id']}/reject",
        headers=desk.admin,
        json={"note": "客户仍由 Alice 跟进"},
    )
    assert (rejected.json()["status"], rejected.json()["decision_note"]) == (
        "rejected",
        "客户仍由 Alice 跟进",
    )
    second = (await _request(desk, alice, mine, to_owner_id=str(bob.staff_id))).json()
    not_mine = await desk.client.post(
        f"/api/v1/customers/transfer-requests/{second['id']}/cancel", headers=bob.headers
    )
    assert not_mine.status_code == 403
    cancelled = await desk.client.post(
        f"/api/v1/customers/transfer-requests/{second['id']}/cancel", headers=alice.headers
    )
    assert cancelled.json()["status"] == "cancelled"
    [owner] = await desk.sql("SELECT owner_id FROM customers WHERE display_name = '李女士'")
    assert owner["owner_id"] == alice.staff_id
