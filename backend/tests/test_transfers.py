"""会话转接（接受、拒绝、超时、撤回、转技能组、强制）与客户归属（转移、交接、历史）。"""

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.sessions.transfer import run_transfer_timers
from tests.desk import Agent, Desk, Visitor
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


async def serving(desk: Desk) -> tuple[Agent, Agent, Visitor, uuid.UUID]:
    """Alice 接待一位访客；Bob 在线但还没有会话（上线在后，分配给先上线的 Alice）。"""
    alice = await desk.agent("alice")
    visitor = await desk.visitor()
    await desk.say(visitor, "你好")
    bob = await desk.agent("bob")
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == alice.staff_id
    return alice, bob, visitor, chat["id"]


async def transfer(
    desk: Desk, who: Agent | None, session_id: uuid.UUID, **body: Any
) -> httpx.Response:
    headers = who.headers if who else desk.admin
    response = await desk.client.post(
        f"/api/v1/sessions/{session_id}/transfer",
        headers=headers,
        json={k: str(v) if isinstance(v, uuid.UUID) else v for k, v in body.items()},
    )
    await desk.flush()
    return response


def signal_types(desk: Desk, agent: Agent) -> list[str]:
    return [s["type"] for s in desk.im.signals_to(agent.im_user)]


async def test_transfer_to_an_agent_who_accepts(desk: Desk) -> None:
    alice, bob, visitor, session_id = await serving(desk)
    await desk.reply(alice, visitor, "请稍等，我帮您转给同事")

    response = await transfer(desk, alice, session_id, to_staff_id=bob.staff_id, note="报价问题")

    assert response.status_code == 200, response.text
    pending = response.json()
    assert (pending["status"], pending["note"]) == ("pending", "报价问题")
    assert (await desk.session_of(visitor))["status"] == "transferring"
    requested = desk.im.signals_to(bob.im_user)[-1]
    assert (requested["type"], requested["from"], requested["note"]) == (
        "transfer.requested",
        "Alice",
        "报价问题",
    )
    listed = await desk.client.get("/api/v1/transfers/pending", headers=bob.headers)
    assert [t["id"] for t in listed.json()["items"]] == [pending["id"]]

    accepted = await desk.client.post(
        f"/api/v1/transfers/{pending['id']}/accept", headers=bob.headers
    )
    await desk.flush()

    assert accepted.json()["status"] == "accepted"
    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", bob.staff_id)
    assert bob.im_user in desk.members(visitor)
    assert alice.im_user not in desk.members(visitor)
    assert desk.notices(visitor)[-1] == "已为您转接至客服 Bob，请稍候。"
    assert signal_types(desk, alice)[-2:] == ["transfer.accepted", "session.revoked"]
    assert signal_types(desk, bob)[-1] == "session.assigned"
    events = await desk.events_of(chat["id"])
    assert events[-2:] == ["transfer_requested", "transferred"]

    # 新坐席看到完整历史，原坐席不再看到这个客户和会话。
    history = await desk.client.get(
        f"/api/v1/rooms/{visitor.room_id}/messages", headers=bob.headers
    )
    texts = [m["text_plain"] for m in reversed(history.json()["items"])]
    assert texts[:3] == ["你好", "客服 Alice 为您服务。", "请稍等，我帮您转给同事"]
    customer = f"/api/v1/customers/{chat['customer_id']}"
    assert (await desk.client.get(customer, headers=alice.headers)).status_code == 404
    assert (await desk.client.get(customer, headers=bob.headers)).status_code == 200
    mine = await desk.client.get("/api/v1/sessions?mine=true&status=open", headers=alice.headers)
    assert mine.json()["items"] == []


async def test_rejected_expired_and_cancelled_transfers_stay_with_the_agent(desk: Desk) -> None:
    alice, bob, visitor, session_id = await serving(desk)

    rejected = await transfer(desk, alice, session_id, to_staff_id=bob.staff_id)
    await desk.client.post(f"/api/v1/transfers/{rejected.json()['id']}/reject", headers=bob.headers)
    await desk.flush()
    assert (await desk.session_of(visitor))["status"] == "human_serving"
    assert signal_types(desk, alice)[-1] == "transfer.rejected"

    expiring = await transfer(desk, alice, session_id, to_staff_id=bob.staff_id)
    expires_at = (
        await desk.sql("SELECT expires_at FROM session_transfers WHERE status = 'pending'")
    )[0]["expires_at"]
    assert await run_transfer_timers(desk.ctx, now=expires_at - timedelta(seconds=1)) == 0
    assert await run_transfer_timers(desk.ctx, now=expires_at + timedelta(seconds=1)) == 1
    await desk.flush()
    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"]) == ("human_serving", alice.staff_id)
    assert signal_types(desk, alice)[-1] == "transfer.expired"
    late = await desk.client.post(
        f"/api/v1/transfers/{expiring.json()['id']}/accept", headers=bob.headers
    )
    assert late.status_code == 404

    cancelled = await transfer(desk, alice, session_id, to_staff_id=bob.staff_id)
    response = await desk.client.post(
        f"/api/v1/transfers/{cancelled.json()['id']}/cancel", headers=alice.headers
    )
    await desk.flush()
    assert response.json()["status"] == "cancelled"
    assert signal_types(desk, bob)[-1] == "transfer.cancelled"
    assert alice.im_user in desk.members(visitor)
    assert bob.im_user not in desk.members(visitor)


async def test_transfer_needs_an_online_target_and_one_pending_request(desk: Desk) -> None:
    alice, bob, _, session_id = await serving(desk)
    carol = await desk.agent("carol", online=False)

    offline = await transfer(desk, alice, session_id, to_staff_id=carol.staff_id)
    assert offline.status_code == 409

    first = await transfer(desk, alice, session_id, to_staff_id=bob.staff_id)
    second = await transfer(desk, alice, session_id, to_staff_id=bob.staff_id)
    assert (first.status_code, second.status_code) == (200, 409)

    both = await transfer(
        desk, alice, session_id, to_staff_id=bob.staff_id, to_group_id=uuid.uuid4()
    )
    assert both.status_code == 422


async def test_transfer_to_a_skill_group_requeues_and_assigns(desk: Desk) -> None:
    alice, bob, visitor, session_id = await serving(desk)
    group = await desk.client.post(
        "/api/v1/skill-groups",
        headers=desk.admin,
        json={"name": "售后", "members": [{"staff_id": str(bob.staff_id)}]},
    )

    response = await transfer(desk, alice, session_id, to_group_id=group.json()["id"])

    assert response.json()["status"] == "completed"
    chat = await desk.session_of(visitor)
    assert (chat["status"], chat["assignee_id"], str(chat["skill_group_id"])) == (
        "human_serving",
        bob.staff_id,
        group.json()["id"],
    )
    assert desk.notices(visitor)[-2:] == ["正在为您转接其他客服，请稍候。", "客服 Bob 为您服务。"]
    assert alice.im_user not in desk.members(visitor)


async def test_forced_transfer_with_ownership(desk: Desk) -> None:
    alice, bob, visitor, session_id = await serving(desk)
    lead = await desk.agent("lead", roles=["supervisor"], online=False)
    await desk.client.post(
        "/api/v1/skill-groups",
        headers=desk.admin,
        json={
            "name": "一组",
            "members": [
                {"staff_id": str(lead.staff_id), "is_lead": True},
                {"staff_id": str(alice.staff_id)},
            ],
        },
    )

    # 坐席不能强制转接；主管可以；变更归属需要 customer:assign（管理员）。
    assert (
        await transfer(desk, alice, session_id, to_staff_id=bob.staff_id, force=True)
    ).status_code == 403
    no_assign = await transfer(
        desk, lead, session_id, to_staff_id=bob.staff_id, force=True, transfer_ownership=True
    )
    assert no_assign.status_code == 403
    response = await transfer(
        desk, None, session_id, to_staff_id=bob.staff_id, force=True, transfer_ownership=True
    )

    assert response.json()["status"] == "completed"
    chat = await desk.session_of(visitor)
    assert chat["assignee_id"] == bob.staff_id
    [customer] = await desk.sql("SELECT owner_id FROM customers WHERE id = $1", chat["customer_id"])
    assert customer["owner_id"] == bob.staff_id
    history = await desk.client.get(
        f"/api/v1/customers/{chat['customer_id']}/owner-history", headers=bob.headers
    )
    [entry] = history.json()["items"]
    assert (entry["reason"], entry["to_owner_name"], entry["actor_name"]) == (
        "session_transfer",
        "Bob",
        "管理员",
    )


async def test_closing_a_transferring_session_cancels_the_request(desk: Desk) -> None:
    alice, bob, visitor, session_id = await serving(desk)
    pending = await transfer(desk, alice, session_id, to_staff_id=bob.staff_id)

    await desk.client.post(f"/api/v1/sessions/{session_id}/close", headers=alice.headers)
    await desk.flush()

    [row] = await desk.sql(
        "SELECT status FROM session_transfers WHERE id = $1", uuid.UUID(pending.json()["id"])
    )
    assert row["status"] == "cancelled"
    assert (await desk.session_of(visitor))["status"] == "closed"


async def test_only_the_assignee_or_a_supervisor_can_transfer(desk: Desk) -> None:
    _, bob, _, session_id = await serving(desk)

    # Bob 看不到 Alice 的会话。
    assert (await transfer(desk, bob, session_id, to_staff_id=bob.staff_id)).status_code == 404


async def test_customer_transfer_handover_and_history(desk: Desk) -> None:
    alice = await desk.agent("alice", online=False)
    bob = await desk.agent("bob", online=False)
    carol = await desk.agent("carol", online=False)
    ids = []
    for name in ("甲", "乙", "丙", "丁"):
        response = await desk.client.post(
            "/api/v1/customers",
            headers=desk.admin,
            json={"display_name": name, "owner_id": str(alice.staff_id)},
        )
        ids.append(response.json()["id"])

    forbidden = await desk.client.post(
        "/api/v1/customers/transfer",
        headers=alice.headers,
        json={"customer_ids": ids[:1], "to_owner_id": str(bob.staff_id)},
    )
    assert forbidden.status_code == 403
    moved = await desk.client.post(
        "/api/v1/customers/transfer",
        headers=desk.admin,
        json={"customer_ids": ids[:1], "to_owner_id": str(bob.staff_id), "note": "调整"},
    )
    assert moved.json() == {"transferred": 1, "wecom": None}

    group = await desk.client.post(
        "/api/v1/skill-groups",
        headers=desk.admin,
        json={
            "name": "接手组",
            "members": [{"staff_id": str(bob.staff_id)}, {"staff_id": str(carol.staff_id)}],
        },
    )
    handed = await desk.client.post(
        f"/api/v1/customers/handover/{alice.staff_id}",
        headers=desk.admin,
        json={"to_group_id": group.json()["id"]},
    )
    assert handed.json() == {"transferred": 3, "wecom": None}
    owners = await desk.sql("SELECT owner_id, count(*) AS n FROM customers GROUP BY owner_id")
    assert {r["owner_id"]: r["n"] for r in owners} == {bob.staff_id: 2, carol.staff_id: 2}
    alice_customers = await desk.client.get("/api/v1/customers", headers=alice.headers)
    assert alice_customers.json()["total"] == 0

    history = await desk.client.get(f"/api/v1/customers/{ids[0]}/owner-history", headers=desk.admin)
    [entry] = history.json()["items"]
    assert (entry["reason"], entry["from_owner_name"], entry["to_owner_name"], entry["note"]) == (
        "manual",
        "Alice",
        "Bob",
        "调整",
    )
