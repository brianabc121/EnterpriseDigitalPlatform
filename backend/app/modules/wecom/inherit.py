"""离职继承与客户群继承（设计文档 §10.4、§14.3）。

- 待分配的离职成员客户：企业微信里成员离职后，他的客户进入"待分配"列表（get_unassigned_list）。
  管理员在平台上把这些客户分配给接替的员工：平台归属随之变更，企业微信里走离职继承
  （resigned/transfer_customer），结果由 contacts.poll_transfers 定时回收。
- 客户群继承：员工离职或调岗交接时，把他作为群主的客户群转给接替的员工。群主已离职用
  groupchat/transfer，在职用 groupchat/onjob_transfer；每次最多 100 个群，结果同步返回。
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, NotFound, Unprocessable
from app.integrations.wecom import WeComError
from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.customer import ownership
from app.modules.customer.models import Customer, CustomerIdentity
from app.modules.customer.schemas import TransferResult, WecomTransferSummary
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.wecom.contacts import TransferItem, left_members, submit_transfers
from app.modules.wecom.models import (
    GroupChatStatus,
    TransferKind,
    WecomGroupChat,
    WecomGroupTransfer,
    WecomMember,
)
from app.modules.wecom.schemas import (
    AssignUnassignedRequest,
    GroupTransferOut,
    UnassignedCustomerOut,
)
from app.modules.wecom.service import active_corp, client_of, require_corp

logger = logging.getLogger(__name__)

_GROUP_PATHS = {
    TransferKind.ONJOB: "/cgi-bin/externalcontact/groupchat/onjob_transfer",
    TransferKind.RESIGNED: "/cgi-bin/externalcontact/groupchat/transfer",
}
_GROUP_BATCH = 100
_UNASSIGNED_PAGES = 50
WECOM_CHANNELS = (ChannelType.WECOM_CONTACT, ChannelType.WECOM_KF)


@dataclass
class GroupTransferSummary:
    transferred: int = 0
    failed: int = 0


def _ts(value: Any) -> datetime | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None


# ---- 客户群继承 ----


async def transfer_groups(
    ctx: AppContext,
    tenant_id: UUID,
    actor_id: UUID | None,
    handover_userid: str,
    takeover_userids: list[str],
    chat_ids: list[str] | None = None,
) -> GroupTransferSummary:
    """把 handover 作为群主的客户群转给接替的成员（有多位时轮流分配）。chat_ids 为空时转移
    他名下的全部客户群。"""
    summary = GroupTransferSummary()
    takeovers = [u for u in dict.fromkeys(takeover_userids) if u and u != handover_userid]
    if ctx.wecom is None or not takeovers:
        return summary
    async with ctx.db.tenant_session(tenant_id) as session:
        corp = await active_corp(session)
        if corp is None:
            return summary
        corp_id = corp.corp_id
        if chat_ids is None:
            chat_ids = list(
                (
                    await session.scalars(
                        select(WecomGroupChat.chat_id)
                        .where(
                            WecomGroupChat.owner_userid == handover_userid,
                            WecomGroupChat.status == GroupChatStatus.NORMAL,
                        )
                        .order_by(WecomGroupChat.chat_id)
                    )
                ).all()
            )
        left = await left_members(session)
    if not chat_ids:
        return summary
    kind = TransferKind.RESIGNED if handover_userid in left else TransferKind.ONJOB
    plan: dict[str, list[str]] = defaultdict(list)
    for index, chat_id in enumerate(chat_ids):
        plan[takeovers[index % len(takeovers)]].append(chat_id)
    rows: list[WecomGroupTransfer] = []
    for takeover, ids in plan.items():
        for start in range(0, len(ids), _GROUP_BATCH):
            chunk = ids[start : start + _GROUP_BATCH]
            failed: dict[str, tuple[int, str]] = {}
            try:
                data = await ctx.wecom.corp_call(
                    corp_id,
                    "POST",
                    _GROUP_PATHS[kind],
                    json={"chat_id_list": chunk, "new_owner": takeover},
                )
                failed = {
                    str(f.get("chat_id")): (int(f.get("errcode") or 0), str(f.get("errmsg") or ""))
                    for f in data.get("failed_chat_list") or []
                }
            except WeComError as exc:
                failed = {chat_id: (exc.errcode, exc.errmsg) for chat_id in chunk}
            for chat_id in chunk:
                error = failed.get(chat_id)
                rows.append(
                    WecomGroupTransfer(
                        tenant_id=tenant_id,
                        chat_id=chat_id,
                        handover_userid=handover_userid,
                        takeover_userid=takeover,
                        kind=kind,
                        status="failed" if error else "success",
                        errcode=error[0] if error else None,
                        error=(
                            f"{error[1] or '企业微信拒绝转移'}（{error[0]}）" if error else None
                        ),
                        created_by=actor_id,
                    )
                )
                if error:
                    summary.failed += 1
                else:
                    summary.transferred += 1
    async with ctx.db.tenant_session(tenant_id) as session:
        session.add_all(rows)
        for row in rows:
            if row.status == "success":
                await session.execute(
                    update(WecomGroupChat)
                    .where(WecomGroupChat.chat_id == row.chat_id)
                    .values(owner_userid=row.takeover_userid)
                )
        await session.commit()
    return summary


async def hand_over_groups(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    from_staff_id: UUID,
    *,
    to_owner_id: UUID | None,
    to_group_id: UUID | None,
) -> GroupTransferSummary:
    """离职或调岗交接时的客户群继承：接手的员工（或技能组里绑定了企业微信的成员）轮流接任群主；
    和客户一样，只能是客服、主管和企业所有者（设计文档 §39.6）。"""
    handover = await session.scalar(select(Staff.wecom_userid).where(Staff.id == from_staff_id))
    if not handover:
        return GroupTransferSummary()
    receivers = await ownership.handover_receivers(
        session, from_staff_id, to_owner_id=to_owner_id, to_group_id=to_group_id
    )
    query = select(Staff.wecom_userid).where(Staff.id.in_(receivers)).order_by(Staff.id)
    takeovers = [u for u in (await session.scalars(query)).all() if u]
    await session.commit()
    return await transfer_groups(ctx, principal.tenant_id, principal.staff_id, handover, takeovers)


async def recent_group_transfers(session: AsyncSession, limit: int = 50) -> list[GroupTransferOut]:
    rows = (
        await session.execute(
            select(WecomGroupTransfer, WecomGroupChat.name)
            .outerjoin(WecomGroupChat, WecomGroupChat.chat_id == WecomGroupTransfer.chat_id)
            .order_by(WecomGroupTransfer.created_at.desc())
            .limit(limit)
        )
    ).all()
    names = await _member_names(
        session, {u for t, _ in rows for u in (t.handover_userid, t.takeover_userid)}
    )
    return [
        GroupTransferOut(
            id=t.id,
            chat_id=t.chat_id,
            group_name=name or None,
            handover_userid=t.handover_userid,
            handover_name=names.get(t.handover_userid),
            takeover_userid=t.takeover_userid,
            takeover_name=names.get(t.takeover_userid),
            kind=t.kind,
            status=t.status,
            error=t.error,
            created_at=t.created_at,
        )
        for t, name in rows
    ]


async def _member_names(session: AsyncSession, userids: set[str]) -> dict[str, str]:
    if not userids:
        return {}
    rows = await session.execute(
        select(WecomMember.userid, WecomMember.name).where(WecomMember.userid.in_(userids))
    )
    return {userid: name for userid, name in rows if name}


# ---- 待分配的离职成员客户 ----


async def _unassigned_raw(ctx: AppContext, corp_id: str) -> list[dict[str, Any]]:
    wecom = client_of(ctx)
    infos: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(_UNASSIGNED_PAGES):
        data = await wecom.corp_call(
            corp_id,
            "POST",
            "/cgi-bin/externalcontact/get_unassigned_list",
            json={"cursor": cursor, "page_size": 1000},
        )
        infos += [i for i in data.get("info") or [] if i.get("external_userid")]
        cursor = str(data.get("next_cursor") or "")
        if data.get("is_last") or not cursor:
            break
    return infos


async def _customers_by_external(
    session: AsyncSession, externals: set[str]
) -> dict[str, tuple[UUID, str, UUID | None]]:
    """外部联系人 → (客户, 客户名称, 归属坐席)。"""
    if not externals:
        return {}
    rows = await session.execute(
        select(CustomerIdentity.external_id, Customer.id, Customer.display_name, Customer.owner_id)
        .join(Customer, Customer.id == CustomerIdentity.customer_id)
        .join(ChannelAccount, ChannelAccount.id == CustomerIdentity.channel_account_id)
        .where(
            CustomerIdentity.external_id.in_(externals),
            ChannelAccount.type.in_(WECOM_CHANNELS),
        )
    )
    return {external: (customer_id, name, owner) for external, customer_id, name, owner in rows}


async def unassigned(ctx: AppContext, session: AsyncSession) -> list[UnassignedCustomerOut]:
    """企业微信里待分配的离职成员客户，关联平台上的客户档案。"""
    corp = await require_corp(session)
    corp_id = corp.corp_id
    await session.commit()
    infos = await _unassigned_raw(ctx, corp_id)
    externals = {str(i["external_userid"]) for i in infos}
    handovers = {str(i.get("handover_userid") or "") for i in infos}
    customers = await _customers_by_external(session, externals)
    names = await _member_names(session, handovers)
    owner_ids = {c[2] for c in customers.values() if c[2]}
    owners = dict(
        (
            await session.execute(
                select(Staff.id, Staff.display_name).where(Staff.id.in_(owner_ids))
            )
        ).all()
    )
    items = []
    for info in infos:
        external = str(info["external_userid"])
        handover = str(info.get("handover_userid") or "")
        customer = customers.get(external)
        items.append(
            UnassignedCustomerOut(
                handover_userid=handover,
                handover_name=names.get(handover),
                external_userid=external,
                customer_id=customer[0] if customer else None,
                customer_name=customer[1] if customer else None,
                owner_name=owners.get(customer[2]) if customer and customer[2] else None,
                dimission_time=_ts(info.get("dimission_time")),
            )
        )
    return items


async def assign_unassigned(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    payload: AssignUnassignedRequest,
) -> TransferResult:
    """把离职成员的客户分配给接替的员工：平台归属变更 + 企业微信离职继承（可选同时转移客户群）。"""
    corp = await require_corp(session)
    corp_id = corp.corp_id
    staff = await ownership.active_staff(session, payload.to_owner_id)
    takeover = staff.wecom_userid
    if not takeover:
        raise Unprocessable("接手的员工还没有绑定企业微信成员")
    if takeover == payload.handover_userid:
        raise Unprocessable("不能分配给离职成员自己")
    await session.commit()
    infos = [
        i
        for i in await _unassigned_raw(ctx, corp_id)
        if str(i.get("handover_userid") or "") == payload.handover_userid
    ]
    pending = {str(i["external_userid"]) for i in infos}
    wanted = set(payload.external_userids) if payload.external_userids else pending
    if not wanted:
        raise NotFound("这位成员没有待分配的客户")
    unknown = wanted - pending
    if unknown:
        raise Conflict(f"{len(unknown)} 位客户不在待分配列表里（可能已经分配过）")
    customers = await _customers_by_external(session, wanted)
    customer_ids = list(dict.fromkeys(c[0] for c in customers.values()))
    changes = []
    if customer_ids:
        changes = await ownership.transfer_customers(
            session,
            principal,
            customer_ids,
            staff.id,
            note=payload.note or f"企业微信离职继承：{payload.handover_userid}",
        )
    history = {h.customer_id: h.id for h in changes}
    items = [
        TransferItem(customer[0], external, history.get(customer[0]))
        for external, customer in sorted(customers.items())
    ]
    part = await submit_transfers(
        ctx,
        principal.tenant_id,
        corp_id,
        principal.staff_id,
        payload.handover_userid,
        takeover,
        TransferKind.RESIGNED,
        items,
    )
    groups = GroupTransferSummary()
    if payload.transfer_groups:
        groups = await transfer_groups(
            ctx, principal.tenant_id, principal.staff_id, payload.handover_userid, [takeover]
        )
    return TransferResult(
        transferred=len(changes),
        wecom=WecomTransferSummary(
            requested=part.requested,
            skipped=len(wanted) - len(items),
            failed=part.failed,
            resigned=part.requested,
            groups_transferred=groups.transferred,
            groups_failed=groups.failed,
        ),
    )
