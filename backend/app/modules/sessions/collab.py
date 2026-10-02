"""会话协作（设计文档 §8.2、§8.3、§14.1）。

- 交还 AI：坐席把人工接待中的会话交回 AI 接待（AI 可用时），坐席退出服务群，名额立即用于分配。
- 客户取消排队：回到 AI 接待；AI 不能接待时结束会话。
- 主管转人工：AI 接待中的会话由主管直接转入人工排队。
- 旁听：主管以观察者身份加入服务群，只看不说，客户看不到；
  邀请协助：接待坐席邀请同事加入会话，协助者可以发言，客户会看到"客服 X 加入了会话"。
  会话结束或交还 AI 时，旁听和协助的员工一并退出。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.modules.ai import reasons
from app.modules.ai import service as ai_service
from app.modules.ai.models import AiSessionState
from app.modules.conversation import outbox
from app.modules.conversation.models import (
    ChatSession,
    CloseReason,
    Room,
    SessionStatus,
    SessionWatcher,
    WatcherRole,
)
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.iam.service import staff_permissions
from app.modules.routing.assign import PolicyResolver
from app.modules.sessions import engine
from app.modules.sessions.engine import ActorType, Signal, record_event
from app.modules.sessions.service import visible_session


class Notice:
    RETURNED = "已为您转回智能客服，如需人工服务，可以随时转人工。"
    CANCELLED_TO_AI = "已取消排队，智能客服继续为您服务。"
    CANCELLED = "已取消排队。如需帮助，请随时留言。"
    ASSIST_JOINED = "客服 {name} 加入了会话。"


def _now() -> datetime:
    return datetime.now(UTC)


async def _locked(session: AsyncSession, session_id: UUID) -> ChatSession:
    chat = await session.scalar(
        select(ChatSession)
        .where(ChatSession.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if chat is None:
        raise NotFound("会话不存在")
    return chat


async def _resume_ai(session: AsyncSession, chat: ChatSession, now: datetime) -> None:
    """会话回到 AI 接待：之前的消息不再回答，计数从头开始。"""
    chat.status = SessionStatus.AI_SERVING
    chat.assignee_id = None
    chat.queued_at = None
    values = {
        "due_at": None,
        "turns": 0,
        "repeats": 0,
        "guard_failures": 0,
        "answered_until": now,
    }
    await session.execute(
        insert(AiSessionState)
        .values(session_id=chat.id, tenant_id=chat.tenant_id, **values)
        .on_conflict_do_update(index_elements=[AiSessionState.session_id], set_=values)
    )


async def _ai_blocker(ctx: AppContext, session: AsyncSession, tenant_id: UUID) -> str | None:
    settings = await ai_service.load(session, tenant_id)
    return await ai_service.unavailable_reason(ctx, session, settings, _now())


# ---- 交还 AI、取消排队、主管转人工 ----


async def return_to_ai(
    ctx: AppContext, session: AsyncSession, principal: Principal, session_id: UUID
) -> None:
    chat, *_ = await visible_session(session, principal, session_id)
    if chat.assignee_id != principal.staff_id and not principal.has(
        Permission.SESSION_TRANSFER_ANY
    ):
        raise Forbidden("只能交还自己接待的会话")
    if await PolicyResolver(session).is_email(chat.channel_account_id):
        # 邮件会话不经过 AI（§10.8），AI 的回复也不会发成邮件。
        raise Conflict("邮件会话由客服回复，不能交还 AI")
    blocker = await _ai_blocker(ctx, session, principal.tenant_id)
    if blocker:
        raise Conflict(f"AI 接待暂时不可用：{reasons.label(blocker)}")
    now = _now()
    chat = await _locked(session, session_id)
    if chat.status != SessionStatus.HUMAN_SERVING:
        raise Conflict("只有人工接待中的会话可以交还 AI")
    staff_id = chat.assignee_id
    await engine.release_watchers(session, chat, now)
    await _resume_ai(session, chat, now)
    record_event(
        session,
        chat,
        "returned_to_ai",
        actor_type=ActorType.STAFF,
        actor_id=principal.staff_id,
        payload={"staff_id": str(staff_id) if staff_id else None},
    )
    if staff_id is not None:
        outbox.enqueue_signal(
            session,
            chat.room_id,
            staff_id,
            {"type": Signal.REVOKED, "session_id": str(chat.id), "room_id": str(chat.room_id)},
        )
        outbox.enqueue_kick(session, chat.room_id, staff_id)
    outbox.enqueue_notice(session, chat.room_id, Notice.RETURNED)
    await session.commit()
    await outbox.flush_rooms(ctx, principal.tenant_id, [chat.room_id])
    await engine.assign_queued(ctx, principal.tenant_id)


async def cancel_queue(ctx: AppContext, tenant_id: UUID, room_id: UUID) -> None:
    """访客取消排队：AI 可以接待时回到 AI 接待，否则结束会话。"""
    now = _now()
    async with ctx.db.tenant_session(tenant_id) as session:
        room = await session.scalar(select(Room).where(Room.id == room_id).with_for_update())
        if room is None:
            raise NotFound("会话不存在")
        chat = await engine.open_session_of_room(session, room.id)
        if chat is None or chat.status != SessionStatus.QUEUED:
            raise Conflict("当前没有在排队")
        if await _ai_blocker(ctx, session, tenant_id) is None:
            await _resume_ai(session, chat, now)
            record_event(
                session, chat, "queue_cancelled", actor_type=ActorType.VISITOR, payload={"to": "ai"}
            )
            outbox.enqueue_notice(session, room.id, Notice.CANCELLED_TO_AI)
        else:
            await engine.mark_closed(
                session,
                chat,
                now,
                reason=CloseReason.VISITOR_CANCEL,
                actor_type=ActorType.VISITOR,
                notice=Notice.CANCELLED,
            )
        await session.commit()
    await outbox.flush_rooms(ctx, tenant_id, [room_id])


async def supervisor_handoff(
    ctx: AppContext, session: AsyncSession, principal: Principal, session_id: UUID
) -> None:
    """主管把 AI 接待中的会话转入人工排队。"""
    chat, *_ = await visible_session(session, principal, session_id)
    if chat.status != SessionStatus.AI_SERVING:
        raise Conflict("只有 AI 接待中的会话可以转人工")
    room_id = chat.room_id
    await session.commit()
    await engine.request_handoff(
        ctx, principal.tenant_id, room_id, reason="supervisor", actor_type=ActorType.STAFF
    )


# ---- 旁听与协助 ----


async def _join(
    session: AsyncSession,
    chat: ChatSession,
    staff: Staff,
    role: WatcherRole,
    *,
    invited_by: UUID | None,
) -> None:
    now = _now()
    values = {"role": role.value, "invited_by": invited_by, "joined_at": now, "left_at": None}
    await session.execute(
        insert(SessionWatcher)
        .values(tenant_id=chat.tenant_id, session_id=chat.id, staff_id=staff.id, **values)
        .on_conflict_do_update(index_elements=["tenant_id", "session_id", "staff_id"], set_=values)
    )
    outbox.enqueue_invite(session, chat.room_id, staff.id, staff.display_name)


async def monitor(
    ctx: AppContext, session: AsyncSession, principal: Principal, session_id: UUID
) -> None:
    """主管旁听：加入服务群，能实时看到消息，客户看不到。"""
    chat, *_ = await visible_session(session, principal, session_id)
    if chat.status == SessionStatus.CLOSED:
        raise Conflict("会话已结束")
    if chat.assignee_id == principal.staff_id:
        raise Conflict("你正在接待这个会话")
    staff = await session.get(Staff, principal.staff_id)
    assert staff is not None
    await _join(session, chat, staff, WatcherRole.MONITOR, invited_by=None)
    record_event(
        session, chat, "monitor_joined", actor_type=ActorType.STAFF, actor_id=principal.staff_id
    )
    await session.commit()
    await outbox.flush_rooms(ctx, principal.tenant_id, [chat.room_id])


async def _can_serve(session: AsyncSession, staff_id: UUID) -> bool:
    staff = await session.get(Staff, staff_id)
    return staff is not None and Permission.WORKBENCH_USE in await staff_permissions(session, staff)


async def invite_assist(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    session_id: UUID,
    staff_id: UUID,
) -> None:
    """接待坐席邀请同事协助：同事加入服务群，可以发言。"""
    chat, *_ = await visible_session(session, principal, session_id)
    if chat.assignee_id != principal.staff_id and not principal.has(
        Permission.SESSION_TRANSFER_ANY
    ):
        raise Forbidden("只有接待这个会话的坐席可以邀请协助")
    if chat.status not in (SessionStatus.HUMAN_SERVING, SessionStatus.TRANSFERRING):
        raise Conflict("会话不在人工接待中")
    if staff_id == chat.assignee_id:
        raise Unprocessable("不能邀请接待坐席本人")
    staff = await session.get(Staff, staff_id)
    if (
        staff is None
        or staff.status != StaffStatus.ACTIVE
        or not await _can_serve(session, staff_id)
    ):
        raise Unprocessable("被邀请的员工不存在、已停用或没有接待权限")
    await _join(session, chat, staff, WatcherRole.ASSIST, invited_by=principal.staff_id)
    record_event(
        session,
        chat,
        "assist_invited",
        actor_type=ActorType.STAFF,
        actor_id=principal.staff_id,
        payload={"staff_id": str(staff_id)},
    )
    outbox.enqueue_notice(
        session, chat.room_id, Notice.ASSIST_JOINED.format(name=staff.display_name)
    )
    outbox.enqueue_signal(
        session,
        chat.room_id,
        staff_id,
        {"type": "session.assist", "session_id": str(chat.id), "room_id": str(chat.room_id)},
    )
    await session.commit()
    await outbox.flush_rooms(ctx, principal.tenant_id, [chat.room_id])


async def leave(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    session_id: UUID,
    staff_id: UUID,
) -> None:
    """退出旁听或协助：本人退出，或接待坐席、主管请协助者退出。"""
    chat, *_ = await visible_session(session, principal, session_id)
    allowed = staff_id == principal.staff_id or chat.assignee_id == principal.staff_id
    if not allowed and not principal.has(Permission.SESSION_TRANSFER_ANY):
        raise Forbidden("不能让其他员工退出会话")
    watcher = await session.scalar(
        select(SessionWatcher)
        .where(
            SessionWatcher.session_id == session_id,
            SessionWatcher.staff_id == staff_id,
            SessionWatcher.left_at.is_(None),
        )
        .with_for_update()
    )
    if watcher is None:
        raise NotFound("没有在旁听或协助这个会话")
    watcher.left_at = _now()
    record_event(
        session,
        chat,
        "watcher_left",
        actor_type=ActorType.STAFF,
        actor_id=principal.staff_id,
        payload={"staff_id": str(staff_id), "role": watcher.role},
    )
    if staff_id != chat.assignee_id:
        outbox.enqueue_kick(session, chat.room_id, staff_id)
    await session.commit()
    await outbox.flush_rooms(ctx, principal.tenant_id, [chat.room_id])
