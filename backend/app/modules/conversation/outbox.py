"""IM 发件箱：把业务状态变化引起的 IM 操作（拉人、踢人、提示、信令）可靠地执行。

- 业务代码在同一个事务里调用 enqueue_* 写入操作，提交后调用 flush_rooms 立即尝试执行；
  没执行成功的由调度进程按退避时间重试（dispatch_due）。
- 同一个 Room 的操作按写入顺序执行（例如先拉坐席进群，再给坐席发分配信令），
  前一个操作失败时，后面的操作等它成功后再执行。每个 Room 同一时刻只有一个执行者（咨询锁）。
- 信令是在线消息，过期就没有意义，失败后不重试；坐席端重连时会从 API 拉取最新状态。
"""

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.integrations.openim import ContentType, OpenIMError
from app.modules.conversation import imids
from app.modules.conversation.models import ImOp, ImOpStatus, ImOpType, Room
from app.modules.conversation.provisioning import BOT_NICKNAME, SYSTEM_NICKNAME

logger = logging.getLogger(__name__)

SIGNAL_DESCRIPTION = "edp.signal"
_LOCK_NAMESPACE = 1001
_MAX_ATTEMPTS = 12
_MAX_BACKOFF_SECONDS = 300
_RETENTION = timedelta(days=7)


def enqueue_invite(session: AsyncSession, room_id: UUID, staff_id: UUID, nickname: str) -> None:
    _enqueue(session, room_id, ImOpType.INVITE, {"staff_id": str(staff_id), "nickname": nickname})


def enqueue_kick(session: AsyncSession, room_id: UUID, staff_id: UUID) -> None:
    _enqueue(session, room_id, ImOpType.KICK, {"staff_id": str(staff_id)})


def enqueue_notice(session: AsyncSession, room_id: UUID, text: str) -> None:
    _enqueue(session, room_id, ImOpType.NOTICE, {"text": text})


def enqueue_bot_message(session: AsyncSession, room_id: UUID, text: str, nickname: str) -> None:
    """AI 回复：以机器人身份发到服务群，ex 带 ai 标记（客户端据此显示"AI"标识）。"""
    _enqueue(session, room_id, ImOpType.BOT_MESSAGE, {"text": text, "nickname": nickname})


def enqueue_signal(
    session: AsyncSession, room_id: UUID, staff_id: UUID, signal: dict[str, Any]
) -> None:
    _enqueue(session, room_id, ImOpType.SIGNAL, {"staff_id": str(staff_id), "signal": signal})


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


async def dispatch_room(
    ctx: AppContext, tenant_id: UUID, room_id: UUID, *, now: datetime | None = None
) -> int:
    now = now or datetime.now(UTC)
    done = 0
    async with ctx.db.tenant_session(tenant_id) as session:
        locked = await session.scalar(
            select(func.pg_try_advisory_xact_lock(_LOCK_NAMESPACE, func.hashtext(str(room_id))))
        )
        if not locked:
            return 0
        room = await session.get(Room, room_id)
        parsed = imids.parse(room.im_group_id) if room is not None else None
        ops = (
            await session.scalars(
                select(ImOp)
                .where(ImOp.room_id == room_id, ImOp.status == ImOpStatus.PENDING)
                .order_by(ImOp.id)
            )
        ).all()
        for op in ops:
            if op.next_attempt_at > now:
                break
            try:
                if room is None or parsed is None:
                    raise ValueError("room is missing or not a platform room")
                await _execute(ctx, parsed.tenant_code, room.im_group_id, op)
            except (OpenIMError, ValueError) as exc:
                op.attempts += 1
                op.last_error = str(exc)[:500]
                if op.op == ImOpType.SIGNAL or op.attempts >= _MAX_ATTEMPTS:
                    logger.warning("im op %s (%s) given up: %s", op.id, op.op, exc)
                    op.status = ImOpStatus.FAILED
                    op.done_at = now
                    continue
                op.next_attempt_at = now + timedelta(
                    seconds=min(2**op.attempts, _MAX_BACKOFF_SECONDS)
                )
                break
            op.status = ImOpStatus.DONE
            op.done_at = now
            done += 1
        await session.commit()
    return done


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
        case _:
            raise ValueError(f"unknown im op {op.op}")
