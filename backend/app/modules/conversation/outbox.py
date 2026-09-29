"""IM 发件箱：把业务状态变化引起的 IM 操作（拉人、踢人、提示、信令）和渠道投递可靠地执行。

- 业务代码在同一个事务里调用 enqueue_* 写入操作，提交后调用 flush_rooms 立即尝试执行；
  没执行成功的由调度进程按退避时间重试（dispatch_due）。
- 同一个 Room 的操作按写入顺序执行（例如先拉坐席进群，再给坐席发分配信令），
  前一个操作失败时，后面的操作等它成功后再执行。每个 Room 同一时刻只有一个执行者（咨询锁）。
- 信令是在线消息，过期就没有意义，失败后不重试；坐席端重连时会从 API 拉取最新状态。
- 外部渠道（微信客服）的 Room 以平台消息库为准：提示和 AI 回复先写入消息库，再投递到渠道，
  投递成功后镜像到服务群（ex 带 pmid）；客户消息从渠道拉取入库后镜像到服务群。
  一个操作分几步执行，每步单独提交，外部调用的结果立即落库。
  这类 Room 的操作分两条通道排队：渠道投递和 IM 操作。OpenIM 暂时不可用时，客户照常收到回复，
  镜像等 OpenIM 恢复后按顺序补上。
"""

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.ids import new_id
from app.integrations.openim import ContentType, OpenIMError
from app.integrations.storage import StorageError
from app.integrations.wecom import WeComUnavailable
from app.modules.ai.models import AiSettings
from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.conversation import imids
from app.modules.conversation.content import im_payload
from app.modules.conversation.ingest import message_received
from app.modules.conversation.models import (
    Direction,
    ImOp,
    ImOpStatus,
    ImOpType,
    Message,
    MessageSource,
    Room,
    SenderType,
    SendStatus,
)
from app.modules.conversation.provisioning import BOT_NICKNAME, SYSTEM_NICKNAME
from app.modules.customer.models import Customer, CustomerIdentity
from app.modules.iam.models import Staff
from app.modules.tenancy.models import Tenant

logger = logging.getLogger(__name__)

SIGNAL_DESCRIPTION = "edp.signal"
_LOCK_NAMESPACE = 1001
# 在线信令：对方不在线时丢弃，失败不重试。
_ONLINE_ONLY = (ImOpType.SIGNAL, ImOpType.TYPING)
_MAX_ATTEMPTS = 12
_MAX_BACKOFF_SECONDS = 300
_RETENTION = timedelta(days=7)
# 一次执行最多推进的步数（防止单个 Room 占住执行者）。
_MAX_STEPS = 200

# 分步执行的操作（渠道 Room 的提示、AI 回复、坐席消息）的当前步骤，记在 payload.stage。
_STAGE_CREATE = "create"  # 写入消息库
_STAGE_DELIVER = "deliver"  # 投递到外部渠道
_STAGE_MIRROR = "mirror"  # 镜像到服务群
_LANE_IM = "im"
_LANE_CHANNEL = "channel"
_RETRYABLE = (OpenIMError, ValueError, WeComUnavailable, StorageError)


def enqueue_invite(session: AsyncSession, room_id: UUID, staff_id: UUID, nickname: str) -> None:
    _enqueue(session, room_id, ImOpType.INVITE, {"staff_id": str(staff_id), "nickname": nickname})


def enqueue_kick(session: AsyncSession, room_id: UUID, staff_id: UUID) -> None:
    _enqueue(session, room_id, ImOpType.KICK, {"staff_id": str(staff_id)})


def enqueue_notice(
    session: AsyncSession, room_id: UUID, text: str, *, menu: list[dict[str, str]] | None = None
) -> None:
    """系统提示。menu 是可点选的选项（微信客服发成菜单消息，其他渠道忽略）。"""
    payload: dict[str, Any] = {"text": text}
    if menu:
        payload["menu"] = menu
    _enqueue(session, room_id, ImOpType.NOTICE, payload)


def enqueue_bot_message(
    session: AsyncSession,
    room_id: UUID,
    text: str,
    nickname: str,
    *,
    menu: list[dict[str, str]] | None = None,
) -> None:
    """AI 回复：以机器人身份发到服务群，ex 带 ai 标记（客户端据此显示"AI"标识）。"""
    payload: dict[str, Any] = {"text": text, "nickname": nickname}
    if menu:
        payload["menu"] = menu
    _enqueue(session, room_id, ImOpType.BOT_MESSAGE, payload)


def enqueue_signal(
    session: AsyncSession, room_id: UUID, staff_id: UUID, signal: dict[str, Any]
) -> None:
    _enqueue(session, room_id, ImOpType.SIGNAL, {"staff_id": str(staff_id), "signal": signal})


def enqueue_typing(session: AsyncSession, room_id: UUID) -> None:
    """智能客服正在生成回答：给服务群发"正在输入"的在线信令（只用于网页 Widget，不落库）。"""
    _enqueue(session, room_id, ImOpType.TYPING, {})


def enqueue_channel_send(session: AsyncSession, room_id: UUID, message_id: UUID) -> None:
    """已写入消息库的出站消息：投递到外部渠道，成功后镜像到服务群。"""
    _enqueue(
        session,
        room_id,
        ImOpType.CHANNEL_SEND,
        {"message_id": str(message_id), "stage": _STAGE_DELIVER},
    )


def enqueue_mirror(session: AsyncSession, room_id: UUID, message_id: UUID) -> None:
    """从外部渠道拉取的消息：镜像到服务群。"""
    _enqueue(session, room_id, ImOpType.MIRROR, {"message_id": str(message_id)})


def _enqueue(session: AsyncSession, room_id: UUID, op: ImOpType, payload: dict[str, Any]) -> None:
    session.add(ImOp(room_id=room_id, op=op, payload=payload))


async def flush_rooms(ctx: AppContext, tenant_id: UUID, room_ids: Iterable[UUID]) -> None:
    """立即执行这些 Room 的待执行操作。尽力而为：失败的留给调度进程重试。"""
    for room_id in dict.fromkeys(room_ids):
        try:
            await dispatch_room(ctx, tenant_id, room_id)
        except Exception:
            logger.exception("im ops for room %s failed; will retry", room_id)


async def dispatch_due(ctx: AppContext, *, now: datetime | None = None) -> int:
    """执行所有到期的待执行操作，返回成功执行的数量。"""
    now = now or datetime.now(UTC)
    async with ctx.db.platform_sessionmaker() as session:
        rows = (
            await session.execute(
                select(ImOp.tenant_id, ImOp.room_id)
                .where(ImOp.status == ImOpStatus.PENDING, ImOp.next_attempt_at <= now)
                .group_by(ImOp.tenant_id, ImOp.room_id)
                .order_by(func.min(ImOp.id))
                .limit(500)
            )
        ).all()
        await session.execute(
            delete(ImOp).where(
                ImOp.status != ImOpStatus.PENDING, ImOp.created_at < now - _RETENTION
            )
        )
        await session.commit()
    done = 0
    for tenant_id, room_id in rows:
        try:
            done += await dispatch_room(ctx, tenant_id, room_id, now=now)
        except Exception:
            logger.exception("im ops for room %s failed; will retry", room_id)
    return done


def _channel_first(channel: ChannelAccount | None) -> bool:
    """以平台消息库为准、经外部渠道收发的 Room（微信客服）。"""
    return channel is not None and channel.type == ChannelType.WECOM_KF


def _stage(op: ImOp) -> str | None:
    stage = (op.payload or {}).get("stage")
    return stage if isinstance(stage, str) else None


def _lane(op: ImOp, channel_first: bool) -> str:
    if not channel_first:
        return _LANE_IM
    if op.op == ImOpType.CHANNEL_SEND and _stage(op) != _STAGE_MIRROR:
        return _LANE_CHANNEL
    if op.op in (ImOpType.NOTICE, ImOpType.BOT_MESSAGE) and _stage(op) != _STAGE_MIRROR:
        return _LANE_CHANNEL
    return _LANE_IM


async def dispatch_room(
    ctx: AppContext, tenant_id: UUID, room_id: UUID, *, now: datetime | None = None
) -> int:
    """按顺序执行一个 Room 的待执行操作，每步单独提交。返回执行完成的操作数。"""
    now = now or datetime.now(UTC)
    done = 0
    blocked: set[str] = set()
    created: list[UUID] = []
    for _ in range(_MAX_STEPS):
        async with ctx.db.tenant_session(tenant_id) as session:
            locked = await session.scalar(
                select(func.pg_try_advisory_xact_lock(_LOCK_NAMESPACE, func.hashtext(str(room_id))))
            )
            if not locked:
                break
            room = await session.get(Room, room_id)
            channel = (
                await session.get(ChannelAccount, room.channel_account_id)
                if room is not None
                else None
            )
            channel_first = _channel_first(channel)
            ops = (
                await session.scalars(
                    select(ImOp)
                    .where(ImOp.room_id == room_id, ImOp.status == ImOpStatus.PENDING)
                    .order_by(ImOp.id)
                )
            ).all()
            # 每条通道只执行排在最前面的操作；它还没到重试时间时，这条通道后面的操作都等着。
            op: ImOp | None = None
            seen: set[str] = set()
            for candidate in ops:
                lane = _lane(candidate, channel_first)
                if lane in seen or lane in blocked:
                    continue
                seen.add(lane)
                if candidate.next_attempt_at > now:
                    blocked.add(lane)
                    continue
                op = candidate
                break
            if op is None:
                break
            lane = _lane(op, channel_first)
            try:
                if room is None:
                    raise ValueError("room is missing")
                finished = await _step(ctx, session, room, channel, op, now, created)
            except _RETRYABLE as exc:
                op.attempts += 1
                op.last_error = str(exc)[:500]
                if op.op in _ONLINE_ONLY or op.attempts >= _MAX_ATTEMPTS:
                    logger.warning("im op %s (%s) given up: %s", op.id, op.op, exc)
                    op.status = ImOpStatus.FAILED
                    op.done_at = now
                else:
                    op.next_attempt_at = now + timedelta(
                        seconds=min(2**op.attempts, _MAX_BACKOFF_SECONDS)
                    )
                    blocked.add(lane)
                await session.commit()
                continue
            if finished:
                op.status = ImOpStatus.DONE
                op.done_at = now
                done += 1
            await session.commit()
    for message_id in created:
        # 平台写入的提示和 AI 回复：与回调入库的消息一样发布事件，由实时消费进程归入会话。
        try:
            await ctx.bus.publish(message_received(tenant_id, room_id, message_id))
        except Exception:
            logger.exception("publishing message.received for %s failed", message_id)
    return done


async def _step(
    ctx: AppContext,
    session: AsyncSession,
    room: Room,
    channel: ChannelAccount | None,
    op: ImOp,
    now: datetime,
    created: list[UUID],
) -> bool:
    """执行操作的一步。返回操作是否已完成；失败时抛出可重试的异常。"""
    parsed = imids.parse(room.im_group_id)
    if parsed is None:
        raise ValueError("room is not a platform room")
    tenant_code = parsed.tenant_code
    if _channel_first(channel):
        if op.op in (ImOpType.NOTICE, ImOpType.BOT_MESSAGE, ImOpType.CHANNEL_SEND):
            return await _channel_step(ctx, session, room, op, now, created)
        if op.op == ImOpType.MIRROR:
            await _mirror(ctx, session, room, UUID(op.payload["message_id"]))
            return True
        await _ensure_room(ctx, session, room, tenant_code)
    await _execute(ctx, tenant_code, room.im_group_id, op)
    return True


async def _channel_step(
    ctx: AppContext,
    session: AsyncSession,
    room: Room,
    op: ImOp,
    now: datetime,
    created: list[UUID],
) -> bool:
    stage = _stage(op) or _STAGE_CREATE
    if stage == _STAGE_CREATE:
        is_bot = op.op == ImOpType.BOT_MESSAGE
        text = str(op.payload.get("text") or "")
        content: dict[str, Any] = {"text": text}
        if op.payload.get("menu"):
            content["menu"] = op.payload["menu"]
        message = Message(
            id=new_id(),
            tenant_id=room.tenant_id,
            room_id=room.id,
            channel_account_id=room.channel_account_id,
            direction=Direction.OUT,
            sender_type=SenderType.BOT if is_bot else SenderType.SYSTEM,
            content_type="text",
            content=content,
            text_plain=text,
            source=MessageSource.API,
            send_status=SendStatus.PENDING,
            sent_at=now,
        )
        session.add(message)
        created.append(message.id)
        op.payload = {**op.payload, "message_id": str(message.id), "stage": _STAGE_DELIVER}
        return False
    message_id = UUID(op.payload["message_id"])
    if stage == _STAGE_DELIVER:
        # 延迟导入：企业微信模块依赖发件箱来写入镜像操作。
        from app.modules.wecom.kf import deliver

        pending = await session.get(Message, message_id)
        if pending is None or pending.send_status == SendStatus.FAILED:
            return True
        if pending.send_status != SendStatus.SENT and not await deliver(
            ctx, session, room, pending, now
        ):
            return True
        op.payload = {**op.payload, "stage": _STAGE_MIRROR}
        return False
    await _mirror(ctx, session, room, message_id)
    return True


async def _ensure_room(
    ctx: AppContext, session: AsyncSession, room: Room, tenant_code: str
) -> None:
    """渠道 Room 的客户身份和服务群在第一次需要时才在 OpenIM 里开通（入站消息不依赖 OpenIM）。"""
    if room.im_ready_at is not None:
        return
    identity = await session.get(CustomerIdentity, room.identity_id)
    customer = await session.get(Customer, room.customer_id)
    tenant = await session.get(Tenant, room.tenant_id)
    if identity is None or customer is None or tenant is None:
        raise ValueError("room identity is missing")
    await ctx.provisioner.ensure_identity(identity, nickname=customer.display_name)
    await ctx.provisioner.ensure_room(
        room, identity, tenant_code=tenant_code, group_name=tenant.name
    )


async def _mirror(ctx: AppContext, session: AsyncSession, room: Room, message_id: UUID) -> None:
    """把消息库里的一条消息以原发送人的身份发到服务群，ex 带 pmid（回调据此关联，不重复入库）。"""
    message = await session.get(Message, message_id)
    if message is None or message.channel_msg_id:
        return
    if message.direction == Direction.OUT and message.send_status != SendStatus.SENT:
        return
    parsed = imids.parse(room.im_group_id)
    assert parsed is not None
    tenant_code = parsed.tenant_code
    await _ensure_room(ctx, session, room, tenant_code)
    ex: dict[str, Any] = {"pmid": str(message.id)}
    match message.sender_type:
        case SenderType.CUSTOMER:
            identity = await session.get(CustomerIdentity, room.identity_id)
            customer = await session.get(Customer, room.customer_id)
            assert identity is not None
            send_id = identity.im_user_id
            nickname = customer.display_name if customer is not None else ""
        case SenderType.AGENT if message.source == MessageSource.API and message.sender_id:
            staff = await session.get(Staff, message.sender_id)
            send_id = imids.staff_user(tenant_code, message.sender_id)
            nickname = staff.display_name if staff is not None else ""
        case SenderType.BOT:
            send_id = imids.bot_user(tenant_code)
            bot_name = await session.scalar(
                select(AiSettings.bot_name).where(AiSettings.tenant_id == room.tenant_id)
            )
            nickname = bot_name or BOT_NICKNAME
            ex["ai"] = True
        case _:
            # 系统提示，以及接待人员在企业微信客户端直接发出的消息（混合模式）。
            send_id = imids.system_user(tenant_code)
            nickname = SYSTEM_NICKNAME
    content_type, content = im_payload(message)
    sent = await ctx.im.send_group_message(
        send_id=send_id,
        group_id=room.im_group_id,
        content_type=content_type,
        content=content,
        sender_nickname=nickname,
        ex=json.dumps(ex),
    )
    await session.execute(
        update(Message)
        .where(
            Message.id == message.id,
            or_(Message.channel_msg_id.is_(None), Message.channel_msg_id == sent.server_msg_id),
        )
        .values(channel_msg_id=sent.server_msg_id)
    )


async def _execute(ctx: AppContext, tenant_code: str, group_id: str, op: ImOp) -> None:
    match op.op:
        case ImOpType.INVITE:
            staff_user = await ctx.provisioner.ensure_staff(
                tenant_code, UUID(op.payload["staff_id"]), nickname=op.payload["nickname"]
            )
            await ctx.im.add_group_members(group_id, [staff_user])
        case ImOpType.KICK:
            staff_user = imids.staff_user(tenant_code, UUID(op.payload["staff_id"]))
            await ctx.im.remove_group_members(group_id, [staff_user])
        case ImOpType.NOTICE:
            await ctx.im.send_group_message(
                send_id=imids.system_user(tenant_code),
                group_id=group_id,
                content_type=ContentType.TEXT,
                content={"content": op.payload["text"]},
                sender_nickname=SYSTEM_NICKNAME,
            )
        case ImOpType.BOT_MESSAGE:
            await ctx.im.send_group_message(
                send_id=imids.bot_user(tenant_code),
                group_id=group_id,
                content_type=ContentType.TEXT,
                content={"content": op.payload["text"]},
                sender_nickname=op.payload.get("nickname") or BOT_NICKNAME,
                ex=json.dumps({"ai": True}),
            )
        case ImOpType.SIGNAL:
            await ctx.im.send_online_only(
                send_id=imids.system_user(tenant_code),
                recv_id=imids.staff_user(tenant_code, UUID(op.payload["staff_id"])),
                content={
                    "data": json.dumps(op.payload["signal"]),
                    "description": SIGNAL_DESCRIPTION,
                    "extension": "",
                },
            )
        case ImOpType.TYPING:
            # 系统用户的自定义消息不会入库（见 ingest._classify_sender）；群消息不需要好友关系。
            await ctx.im.send_group_message(
                send_id=imids.system_user(tenant_code),
                group_id=group_id,
                content_type=ContentType.CUSTOM,
                content={
                    "data": json.dumps({"type": "typing"}),
                    "description": SIGNAL_DESCRIPTION,
                    "extension": "",
                },
                sender_nickname=SYSTEM_NICKNAME,
                online_only=True,
            )
        case _:
            raise ValueError(f"unknown im op {op.op}")
