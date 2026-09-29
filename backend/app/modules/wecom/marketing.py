"""客户群活码与群发任务（设计文档 §10.4）。

- "加入群聊"二维码（groupchat/add_join_way，scene=2）：扫码进入指定的客户群，群满后可以自动建新群。
  每个二维码带一个 state，经它进群的成员在群成员详情里带同样的 state，平台据此统计进群人数。
- 群发任务（add_msg_template）：企业微信不允许经 API 直接给客户发消息，平台只能创建任务，由员工
  （发给客户）或群主（发到客户群）在企业微信里确认后发出。发给客户时按归属坐席（或最早的添加人）
  分别创建，由他来发；结果（get_groupmsg_task、get_groupmsg_send_result）由调度进程定时回收。
"""

import logging
import secrets
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, func, or_, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import NotFound, Unprocessable
from app.core.permissions import Permission
from app.integrations.wecom import WeComError, WeComUnavailable
from app.modules.customer.models import Customer
from app.modules.customer.service import visible_to
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.routing.scope import team_members
from app.modules.wecom.models import (
    BroadcastResultStatus,
    BroadcastStatus,
    GroupChatStatus,
    GroupMemberType,
    WecomBroadcast,
    WecomBroadcastResult,
    WecomContactFollow,
    WecomGroupChat,
    WecomGroupMember,
    WecomJoinWay,
    WecomMember,
)
from app.modules.wecom.schemas import (
    BroadcastCreate,
    BroadcastDetail,
    BroadcastLink,
    BroadcastMemberOut,
    BroadcastOptions,
    BroadcastOut,
    BroadcastOwner,
    JoinWayCreate,
    JoinWayOut,
)
from app.modules.wecom.service import active_corp, client_of, require_corp
from app.modules.wecom.sidebar import group_out

logger = logging.getLogger(__name__)

_PAGE = 1000
_MAX_PAGES = 20
# 发给客户时每个任务最多 1 万位客户；发到客户群时每个任务最多 2000 个群。
_SINGLE_LIMIT = 10000
_GROUP_LIMIT = 2000
POLL_WINDOW = timedelta(days=30)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ts(value: Any) -> datetime | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None


# ---- 客户群活码 ----


async def _group_names(session: AsyncSession, chat_ids: set[str]) -> dict[str, str]:
    if not chat_ids:
        return {}
    rows = await session.execute(
        select(WecomGroupChat.chat_id, WecomGroupChat.name).where(
            WecomGroupChat.chat_id.in_(chat_ids)
        )
    )
    return {chat_id: name for chat_id, name in rows}


async def list_join_ways(session: AsyncSession) -> list[JoinWayOut]:
    ways = (
        await session.scalars(select(WecomJoinWay).order_by(WecomJoinWay.created_at.desc()))
    ).all()
    names = await _group_names(session, {c for w in ways for c in w.chat_ids})
    joined = dict(
        (
            await session.execute(
                select(WecomGroupMember.state, func.count())
                .where(
                    WecomGroupMember.state.in_([w.state for w in ways]),
                    WecomGroupMember.type == GroupMemberType.EXTERNAL,
                )
                .group_by(WecomGroupMember.state)
            )
        ).all()
    )
    return [_join_way_out(w, names, int(joined.get(w.state, 0))) for w in ways]


def _join_way_out(way: WecomJoinWay, names: dict[str, str], joined: int) -> JoinWayOut:
    return JoinWayOut(
        id=way.id,
        config_id=way.config_id,
        name=way.name,
        chat_ids=list(way.chat_ids),
        group_names=[names.get(c) or c for c in way.chat_ids],
        auto_create_room=way.auto_create_room,
        room_base_name=way.room_base_name,
        room_base_id=way.room_base_id,
        state=way.state,
        qr_code=way.qr_code,
        joined=joined,
        created_at=way.created_at,
    )


async def create_join_way(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: JoinWayCreate
) -> JoinWayOut:
    corp = await require_corp(session)
    chat_ids = list(dict.fromkeys(payload.chat_ids))
    names = await _group_names(session, set(chat_ids))
    missing = [c for c in chat_ids if c not in names]
    if missing:
        raise Unprocessable("客户群不存在，请先同步客户群")
    if payload.auto_create_room and not payload.room_base_name:
        raise Unprocessable("自动建群时需要填写群名前缀")
    corp_id = corp.corp_id
    await session.commit()
    state = f"edp{secrets.token_hex(8)}"
    body: dict[str, Any] = {
        "scene": 2,
        "remark": payload.name,
        "auto_create_room": 1 if payload.auto_create_room else 0,
        "chat_id_list": chat_ids,
        "state": state,
    }
    if payload.auto_create_room:
        body["room_base_name"] = payload.room_base_name
        body["room_base_id"] = payload.room_base_id or 1
    wecom = client_of(ctx)
    try:
        data = await wecom.corp_call(
            corp_id, "POST", "/cgi-bin/externalcontact/groupchat/add_join_way", json=body
        )
        config_id = str(data["config_id"])
        detail = await wecom.corp_call(
            corp_id,
            "POST",
            "/cgi-bin/externalcontact/groupchat/get_join_way",
            json={"config_id": config_id},
        )
    except WeComUnavailable:
        raise
    except WeComError as exc:
        raise Unprocessable(f"企业微信拒绝创建二维码：{exc.errmsg or exc.errcode}") from exc
    way = WecomJoinWay(
        tenant_id=principal.tenant_id,
        config_id=config_id,
        name=payload.name,
        chat_ids=chat_ids,
        auto_create_room=payload.auto_create_room,
        room_base_name=payload.room_base_name if payload.auto_create_room else None,
        room_base_id=(payload.room_base_id or 1) if payload.auto_create_room else None,
        state=state,
        qr_code=(detail.get("join_way") or {}).get("qr_code"),
        created_by=principal.staff_id,
    )
    session.add(way)
    await session.commit()
    return _join_way_out(way, names, 0)


async def delete_join_way(ctx: AppContext, session: AsyncSession, way_id: UUID) -> None:
    way = await session.get(WecomJoinWay, way_id)
    if way is None:
        raise NotFound("二维码不存在")
    corp = await require_corp(session)
    try:
        await client_of(ctx).corp_call(
            corp.corp_id,
            "POST",
            "/cgi-bin/externalcontact/groupchat/del_join_way",
            json={"config_id": way.config_id},
        )
    except WeComUnavailable:
        raise
    except WeComError as exc:
        # 企业微信里已经删除了的二维码照样从平台删除。
        logger.info("del_join_way %s: %s", way.config_id, exc)
    await session.delete(way)
    await session.commit()


# ---- 群发任务 ----


async def _single_targets(
    session: AsyncSession, principal: Principal, payload: BroadcastCreate
) -> dict[str, list[str]]:
    """发给客户：按员工可见范围和筛选条件找到客户，每位客户由归属坐席（没有时由最早添加他的成员）
    发送。返回 发送成员 → 外部联系人。"""
    audience = payload.audience
    if not (audience.customer_ids or audience.tags or audience.owner_ids):
        raise Unprocessable("请选择要发送的客户（客户、标签或归属坐席）")
    query = (
        select(
            WecomContactFollow.customer_id,
            WecomContactFollow.userid,
            WecomContactFollow.external_userid,
            Staff.wecom_userid,
        )
        .join(Customer, Customer.id == WecomContactFollow.customer_id)
        .outerjoin(Staff, Staff.id == Customer.owner_id)
        .where(WecomContactFollow.deleted_at.is_(None), visible_to(principal))
        .order_by(WecomContactFollow.added_at.nulls_last(), WecomContactFollow.userid)
    )
    if audience.customer_ids:
        query = query.where(Customer.id.in_(audience.customer_ids))
    if audience.tags:
        query = query.where(Customer.tags.overlap(audience.tags))
    if audience.owner_ids:
        query = query.where(Customer.owner_id.in_(audience.owner_ids))
    rows = (await session.execute(query)).all()
    chosen: dict[UUID, tuple[str, str]] = {}
    for customer_id, userid, external, owner_userid in rows:
        current = chosen.get(customer_id)
        if current is None or (userid == owner_userid and current[0] != owner_userid):
            chosen[customer_id] = (userid, external)
    targets: dict[str, list[str]] = defaultdict(list)
    for userid, external in chosen.values():
        if external not in targets[userid]:
            targets[userid].append(external)
    return dict(targets)


async def _group_targets(
    session: AsyncSession, principal: Principal, payload: BroadcastCreate
) -> dict[str, list[str]]:
    """发到客户群：按群或群主筛选，每个群由群主发送。没有"查看全部客户"权限的员工只能发到
    自己（或所带组员）作为群主的群。返回 群主 → 客户群。"""
    audience = payload.audience
    if not (audience.chat_ids or audience.owner_ids):
        raise Unprocessable("请选择客户群或群主")
    query = select(WecomGroupChat.owner_userid, WecomGroupChat.chat_id).where(
        WecomGroupChat.status == GroupChatStatus.NORMAL, WecomGroupChat.owner_userid.is_not(None)
    )
    if audience.chat_ids:
        query = query.where(WecomGroupChat.chat_id.in_(audience.chat_ids))
    if audience.owner_ids:
        owners = select(Staff.wecom_userid).where(Staff.id.in_(audience.owner_ids))
        query = query.where(WecomGroupChat.owner_userid.in_(owners))
    if not principal.has(Permission.CUSTOMER_READ_ALL):
        mine: ColumnElement[bool] = Staff.id == principal.staff_id
        if principal.has(Permission.SESSION_READ_TEAM):
            mine = or_(mine, Staff.id.in_(team_members(principal.staff_id)))
        query = query.where(WecomGroupChat.owner_userid.in_(select(Staff.wecom_userid).where(mine)))
    targets: dict[str, list[str]] = defaultdict(list)
    for owner, chat_id in (await session.execute(query.order_by(WecomGroupChat.chat_id))).all():
        targets[str(owner)].append(chat_id)
    return dict(targets)


def _attachments(link: BroadcastLink | None) -> list[dict[str, Any]]:
    if link is None:
        return []
    return [
        {
            "msgtype": "link",
            "link": {"title": link.title, "url": link.url, "desc": link.desc or ""},
        }
    ]


async def create_broadcast(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: BroadcastCreate
) -> BroadcastOut:
    corp = await require_corp(session)
    corp_id = corp.corp_id
    if payload.kind == "single":
        targets = await _single_targets(session, principal, payload)
        limit = _SINGLE_LIMIT
    else:
        targets = await _group_targets(session, principal, payload)
        limit = _GROUP_LIMIT
    if not targets:
        raise Unprocessable(
            "没有符合条件、可以发送的客户（客户需要是企业微信好友）"
            if payload.kind == "single"
            else "没有符合条件的客户群"
        )
    broadcast = WecomBroadcast(
        tenant_id=principal.tenant_id,
        kind=payload.kind,
        title=payload.title,
        content=payload.content,
        link=payload.link.model_dump() if payload.link else None,
        audience=payload.audience.model_dump(mode="json", exclude_none=True),
        target_count=sum(len(v) for v in targets.values()),
        status=BroadcastStatus.CREATED,
        created_by=principal.staff_id,
    )
    session.add(broadcast)
    await session.commit()
    wecom = client_of(ctx)
    msgids: list[str] = []
    fail_list: list[Any] = []
    errors: list[str] = []
    for sender, ids in sorted(targets.items()):
        for start in range(0, len(ids), limit):
            chunk = ids[start : start + limit]
            body: dict[str, Any] = {
                "chat_type": payload.kind,
                "sender": sender,
                "text": {"content": payload.content},
                "attachments": _attachments(payload.link),
            }
            if payload.kind == "single":
                body["external_userid"] = chunk
            else:
                body["chat_id_list"] = chunk
            try:
                data = await wecom.corp_call(
                    corp_id, "POST", "/cgi-bin/externalcontact/add_msg_template", json=body
                )
            except WeComError as exc:
                errors.append(f"{sender}：{exc.errmsg or '企业微信拒绝'}（{exc.errcode}）")
                continue
            if data.get("msgid"):
                msgids.append(str(data["msgid"]))
            fail_list += list(data.get("fail_list") or [])
    broadcast.msgids = msgids
    broadcast.fail_list = fail_list
    broadcast.error = "；".join(errors)[:2000] or None
    broadcast.status = BroadcastStatus.CREATED if msgids else BroadcastStatus.FAILED
    await session.commit()
    names = await _staff_names(session, {broadcast.created_by} - {None})
    return _broadcast_out(broadcast, names)


async def _staff_names(session: AsyncSession, ids: set[UUID | None]) -> dict[UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(wanted)))
    return {staff_id: name for staff_id, name in rows}


def _broadcast_out(b: WecomBroadcast, names: dict[UUID, str]) -> BroadcastOut:
    return BroadcastOut(
        id=b.id,
        kind=b.kind,
        title=b.title,
        content=b.content,
        link=BroadcastLink.model_validate(b.link) if b.link else None,
        target_count=b.target_count,
        status=b.status,
        error=b.error,
        stats=b.stats or {},
        created_by_name=names.get(b.created_by) if b.created_by else None,
        created_at=b.created_at,
        polled_at=b.polled_at,
    )


def _scope(principal: Principal) -> ColumnElement[bool]:
    """没有"查看全部客户"权限的员工只看到自己创建的群发任务。"""
    if principal.has(Permission.CUSTOMER_READ_ALL):
        return true()
    return WecomBroadcast.created_by == principal.staff_id


async def broadcast_options(session: AsyncSession, principal: Principal) -> BroadcastOptions:
    """群发表单的候选：客户标签、归属坐席、客户群（都限于员工的可见范围）。"""
    tag = func.unnest(Customer.tags).label("tag")
    tags: list[str] = list(
        (
            await session.scalars(select(tag).where(visible_to(principal)).distinct().order_by(tag))
        ).all()
    )
    staff = select(Staff.id, Staff.display_name).where(Staff.status == StaffStatus.ACTIVE)
    groups = (
        select(WecomGroupChat, WecomMember.name)
        .outerjoin(WecomMember, WecomMember.userid == WecomGroupChat.owner_userid)
        .where(WecomGroupChat.status == GroupChatStatus.NORMAL)
    )
    if not principal.has(Permission.CUSTOMER_READ_ALL):
        mine: ColumnElement[bool] = Staff.id == principal.staff_id
        if principal.has(Permission.SESSION_READ_TEAM):
            mine = or_(mine, Staff.id.in_(team_members(principal.staff_id)))
        staff = staff.where(mine)
        groups = groups.where(
            WecomGroupChat.owner_userid.in_(select(Staff.wecom_userid).where(mine))
        )
    owners = (await session.execute(staff.order_by(Staff.display_name))).all()
    chats = (await session.execute(groups.order_by(WecomGroupChat.name))).all()
    return BroadcastOptions(
        tags=[t for t in tags if t],
        owners=[BroadcastOwner(id=staff_id, name=name) for staff_id, name in owners],
        group_chats=[group_out(g, owner_name) for g, owner_name in chats],
    )


async def list_broadcasts(session: AsyncSession, principal: Principal) -> list[BroadcastOut]:
    rows = (
        await session.scalars(
            select(WecomBroadcast)
            .where(_scope(principal))
            .order_by(WecomBroadcast.created_at.desc())
            .limit(100)
        )
    ).all()
    names = await _staff_names(session, {b.created_by for b in rows})
    return [_broadcast_out(b, names) for b in rows]


async def get_broadcast(
    session: AsyncSession, principal: Principal, broadcast_id: UUID
) -> WecomBroadcast:
    broadcast = await session.scalar(
        select(WecomBroadcast).where(WecomBroadcast.id == broadcast_id, _scope(principal))
    )
    if broadcast is None:
        raise NotFound("群发任务不存在")
    return broadcast


async def broadcast_detail(session: AsyncSession, broadcast: WecomBroadcast) -> BroadcastDetail:
    names = await _staff_names(session, {broadcast.created_by})
    counts = (
        await session.execute(
            select(WecomBroadcastResult.userid, WecomBroadcastResult.status, func.count())
            .where(
                WecomBroadcastResult.broadcast_id == broadcast.id,
                (WecomBroadcastResult.external_userid.is_not(None))
                | (WecomBroadcastResult.chat_id.is_not(None)),
            )
            .group_by(WecomBroadcastResult.userid, WecomBroadcastResult.status)
        )
    ).all()
    per_member: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for userid, status, count in counts:
        per_member[userid][int(status)] += int(count)
    tasks = {str(t["userid"]): t for t in (broadcast.stats or {}).get("members") or []}
    userids = set(per_member) | set(tasks)
    member_names = dict(
        (
            await session.execute(
                select(WecomMember.userid, WecomMember.name).where(WecomMember.userid.in_(userids))
            )
        ).all()
    )
    members = [
        BroadcastMemberOut(
            userid=userid,
            name=member_names.get(userid) or None,
            confirmed=bool((tasks.get(userid) or {}).get("confirmed")),
            send_time=_parse_time((tasks.get(userid) or {}).get("send_time")),
            sent=per_member[userid][BroadcastResultStatus.SENT],
            failed=per_member[userid][BroadcastResultStatus.NOT_FRIEND]
            + per_member[userid][BroadcastResultStatus.RECEIVED_OTHER],
            unsent=per_member[userid][BroadcastResultStatus.UNSENT],
        )
        for userid in sorted(userids)
    ]
    out = _broadcast_out(broadcast, names)
    return BroadcastDetail(**out.model_dump(), members=members, fail_list=broadcast.fail_list)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def _paged(
    ctx: AppContext, corp_id: str, path: str, body: dict[str, Any], key: str
) -> list[dict[str, Any]]:
    wecom = client_of(ctx)
    items: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(_MAX_PAGES):
        data = await wecom.corp_call(
            corp_id, "POST", path, json={**body, "limit": _PAGE, "cursor": cursor}
        )
        items += data.get(key) or []
        cursor = str(data.get("next_cursor") or "")
        if not cursor:
            break
    return items


async def refresh_broadcast(ctx: AppContext, tenant_id: UUID, broadcast_id: UUID) -> None:
    """回收一个群发任务的结果：各成员是否已确认发送，以及每位客户（或每个群）的发送情况。"""
    async with ctx.db.tenant_session(tenant_id) as session:
        broadcast = await session.get(WecomBroadcast, broadcast_id)
        corp = await active_corp(session)
        if broadcast is None or corp is None or not broadcast.msgids:
            return
        msgids = list(broadcast.msgids)
        corp_id = corp.corp_id
    members: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for msgid in msgids:
        tasks = await _paged(
            ctx,
            corp_id,
            "/cgi-bin/externalcontact/get_groupmsg_task",
            {"msgid": msgid},
            "task_list",
        )
        for task in tasks:
            userid = str(task.get("userid") or "")
            if not userid:
                continue
            send_time = _ts(task.get("send_time"))
            members.append(
                {
                    "userid": userid,
                    "confirmed": int(task.get("status") or 0) == 2,
                    "send_time": send_time.isoformat() if send_time else None,
                }
            )
            sends = await _paged(
                ctx,
                corp_id,
                "/cgi-bin/externalcontact/get_groupmsg_send_result",
                {"msgid": msgid, "userid": userid},
                "send_list",
            )
            for send in sends:
                results.append({**send, "msgid": msgid, "userid": userid})
    now = utcnow()
    async with ctx.db.tenant_session(tenant_id) as session:
        for r in results:
            values = {
                "msgid": r["msgid"],
                "status": int(r.get("status") or 0),
                "send_time": _ts(r.get("send_time")),
                "updated_at": now,
            }
            await session.execute(
                insert(WecomBroadcastResult)
                .values(
                    tenant_id=tenant_id,
                    broadcast_id=broadcast_id,
                    userid=r["userid"],
                    external_userid=r.get("external_userid") or None,
                    chat_id=r.get("chat_id") or None,
                    **values,
                )
                .on_conflict_do_update(constraint="uq_wecom_broadcast_results", set_=values)
            )
        broadcast = await session.get(WecomBroadcast, broadcast_id)
        if broadcast is not None:
            totals = (
                await session.execute(
                    select(WecomBroadcastResult.status, func.count())
                    .where(WecomBroadcastResult.broadcast_id == broadcast_id)
                    .group_by(WecomBroadcastResult.status)
                )
            ).all()
            by_status = {int(status): int(count) for status, count in totals}
            broadcast.stats = {
                "members": members,
                "members_total": len(members),
                "members_confirmed": sum(1 for m in members if m["confirmed"]),
                "sent": by_status.get(BroadcastResultStatus.SENT, 0),
                "unsent": by_status.get(BroadcastResultStatus.UNSENT, 0),
                "failed": by_status.get(BroadcastResultStatus.NOT_FRIEND, 0)
                + by_status.get(BroadcastResultStatus.RECEIVED_OTHER, 0),
            }
            broadcast.polled_at = now
        await session.commit()


async def cancel_broadcast(
    ctx: AppContext, session: AsyncSession, broadcast: WecomBroadcast
) -> None:
    """停止群发：还没确认发送的成员不能再发送。"""
    if broadcast.status != BroadcastStatus.CREATED:
        raise Unprocessable("这个群发任务已经停止或创建失败")
    corp = await require_corp(session)
    for msgid in broadcast.msgids:
        try:
            await client_of(ctx).corp_call(
                corp.corp_id,
                "POST",
                "/cgi-bin/externalcontact/cancel_groupmsg_send",
                json={"msgid": msgid},
            )
        except WeComUnavailable:
            raise
        except WeComError as exc:
            logger.info("cancel_groupmsg_send %s: %s", msgid, exc)
    broadcast.status = BroadcastStatus.CANCELLED
    await session.commit()


async def remind_broadcast(
    ctx: AppContext, session: AsyncSession, broadcast: WecomBroadcast
) -> None:
    """提醒还没确认的成员发送（企业微信限制每个任务每天最多提醒 3 次）。"""
    if broadcast.status != BroadcastStatus.CREATED:
        raise Unprocessable("这个群发任务已经停止或创建失败")
    corp = await require_corp(session)
    for msgid in broadcast.msgids:
        try:
            await client_of(ctx).corp_call(
                corp.corp_id,
                "POST",
                "/cgi-bin/externalcontact/remind_groupmsg_send",
                json={"msgid": msgid},
            )
        except WeComUnavailable:
            raise
        except WeComError as exc:
            raise Unprocessable(f"企业微信拒绝提醒：{exc.errmsg or exc.errcode}") from exc


async def poll_broadcasts(ctx: AppContext) -> int:
    """调度进程：回收最近 30 天群发任务的结果。返回处理的任务数。"""
    if ctx.wecom is None:
        return 0
    async with ctx.db.platform_sessionmaker() as session:
        rows = (
            await session.execute(
                select(WecomBroadcast.tenant_id, WecomBroadcast.id).where(
                    WecomBroadcast.status == BroadcastStatus.CREATED,
                    WecomBroadcast.created_at >= utcnow() - POLL_WINDOW,
                )
            )
        ).all()
    done = 0
    for tenant_id, broadcast_id in rows:
        try:
            await refresh_broadcast(ctx, tenant_id, broadcast_id)
            done += 1
        except WeComError as exc:
            logger.warning("broadcast %s results failed: %s", broadcast_id, exc)
    return done
