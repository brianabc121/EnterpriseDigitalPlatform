"""AI 接待（设计文档 §11）：处理到期的待回复会话。由实时消费进程循环调用（见 app/worker.py）。

每个会话分三步，避免在调用大模型时占着数据库事务：
1. 读取：会话仍在 AI 接待中，取出还没回复的客户消息（合并成一个问题）和最近的对话；
2. 判定：前置规则、检索、生成、护栏、转人工决策（pipeline.evaluate），需要转人工时生成交接摘要；
3. 写入：再次确认会话状态，记录判定、更新计数，经发件箱以机器人身份回复；需要时转入人工排队。

领取时把 due_at 推后两分钟作为租约：进程中途退出，租约到期后会重新处理。处理期间客户又发了消息时
due_at 会被改写，处理完不清除它，新消息会在下一轮与之前未回复的消息一起处理。
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.context import AppContext
from app.modules.ai import pipeline
from app.modules.ai import service as ai_service
from app.modules.ai.models import AiDecision, AiSessionState, DecisionAction
from app.modules.ai.prompts import Turn
from app.modules.ai.segments import split_reply
from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.conversation import outbox
from app.modules.conversation.models import (
    ChatSession,
    Message,
    SenderType,
    SessionStatus,
)
from app.modules.customer.models import Customer
from app.modules.kb.models import KbItem
from app.modules.routing.assign import PolicyResolver
from app.modules.routing.priority import intent_names
from app.modules.sessions import engine
from app.modules.tenancy.models import Tenant
from app.modules.wecom import menus

logger = logging.getLogger(__name__)

LEASE = timedelta(minutes=2)
CONCURRENCY = 4
_PLACEHOLDER = {"image": "[图片]", "file": "[文件]", "voice": "[语音]", "video": "[视频]"}
_ROLES: dict[str, str] = {
    SenderType.CUSTOMER: "customer",
    SenderType.BOT: "bot",
    SenderType.AGENT: "agent",
}


def _text(message: Message) -> str:
    return message.text_plain or _PLACEHOLDER.get(message.content_type, "[消息]")


def channel_ai(channel: ChannelAccount | None) -> pipeline.ChannelAi:
    """渠道的 AI 参数覆盖和知识空间范围。"""
    if channel is None:
        return pipeline.ChannelAi()
    overrides = channel.ai_overrides or {}
    spaces = channel.kb_space_ids

    def number(key: str) -> float | None:
        value = overrides.get(key)
        return float(value) if isinstance(value, int | float) else None

    turns = overrides.get("max_turns")
    return pipeline.ChannelAi(
        handoff_threshold=number("handoff_threshold"),
        relevance_threshold=number("relevance_threshold"),
        max_turns=int(turns) if isinstance(turns, int) else None,
        space_ids=list(spaces or []),
    )


async def claim(
    ctx: AppContext, *, limit: int, now: datetime
) -> list[tuple[uuid.UUID, uuid.UUID, datetime]]:
    """领取到期的待回复会话（跨租户，平台连接），返回 (租户, 会话, 租约到期时间)。"""
    lease = now + LEASE
    async with ctx.db.platform_sessionmaker() as session:
        due = (
            select(AiSessionState.session_id)
            .where(AiSessionState.due_at <= now)
            .order_by(AiSessionState.due_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = await session.execute(
            update(AiSessionState)
            .where(AiSessionState.session_id.in_(due.scalar_subquery()))
            .values(due_at=lease)
            .returning(AiSessionState.tenant_id, AiSessionState.session_id)
        )
        claimed = [(tenant_id, session_id, lease) for tenant_id, session_id in rows]
        await session.commit()
    return claimed


async def run_due(ctx: AppContext, *, limit: int = 20, now: datetime | None = None) -> int:
    """处理所有到期的会话，返回处理的数量。"""
    claimed = await claim(ctx, limit=limit, now=now or datetime.now(UTC))
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(tenant_id: uuid.UUID, session_id: uuid.UUID, lease: datetime) -> None:
        async with semaphore:
            try:
                await respond(ctx, tenant_id, session_id, lease)
            except Exception:
                logger.exception("AI reply failed for session %s", session_id)

    await asyncio.gather(*(one(*item) for item in claimed))
    return len(claimed)


def _release(state: AiSessionState, lease: datetime) -> None:
    """处理完成：处理期间没有新消息（due_at 仍是租约）时清除待回复。"""
    if state.due_at == lease:
        state.due_at = None


async def respond(
    ctx: AppContext, tenant_id: uuid.UUID, session_id: uuid.UUID, lease: datetime
) -> None:
    now = datetime.now(UTC)
    # 1. 读取
    async with ctx.db.tenant_session(tenant_id) as session:
        chat = await session.get(ChatSession, session_id)
        state = await session.get(AiSessionState, session_id)
        if chat is None or state is None:
            return
        policy = await PolicyResolver(session).for_channel(chat.channel_account_id)
        # 排队中：策略允许时 AI 继续回答客户的其他问题，但不再转人工（已经在排队）。
        queued = chat.status == SessionStatus.QUEUED
        serving = chat.status == SessionStatus.AI_SERVING or (queued and policy.ai_while_queued)
        if not serving:
            _release(state, lease)
            await session.commit()
            return
        intents = intent_names(policy.intent_routes or [])
        settings = await ai_service.load(session, tenant_id)
        unavailable = await ai_service.unavailable_reason(ctx, session, settings, now)
        if unavailable and queued:
            _release(state, lease)
            await session.commit()
            return
        customer = await session.get(Customer, chat.customer_id)
        company = await session.scalar(select(Tenant.name).where(Tenant.id == tenant_id)) or ""
        messages = (
            await session.scalars(
                select(Message)
                .where(Message.session_id == chat.id)
                .order_by(Message.sent_at, Message.id)
            )
        ).all()
        pending = [
            m
            for m in messages
            if m.sender_type == SenderType.CUSTOMER
            and (state.answered_until is None or m.sent_at > state.answered_until)
        ]
        if not pending:
            _release(state, lease)
            await session.commit()
            return
        history = [
            Turn(_ROLES[m.sender_type], _text(m))
            for m in messages
            if m not in pending and m.sender_type in _ROLES
        ]
        context = pipeline.Context(
            question="\n".join(_text(m) for m in pending),
            history=history,
            customer_tags=list(customer.tags) if customer else [],
            previous_question=state.last_question,
            repeats=state.repeats,
            turns=state.turns,
            guard_failures=state.guard_failures,
        )
        account = await session.get(ChannelAccount, chat.channel_account_id)
        channel = channel_ai(account)
        # 网页 Widget：生成回答期间显示"正在输入"，较长的回答分段发送。
        web = account is not None and account.type == ChannelType.WEB
        segmented = web and settings.segment_replies and not unavailable
        if segmented:
            outbox.enqueue_typing(session, chat.room_id)
        customer_id = chat.customer_id
        room_id = chat.room_id
        answered_until = pending[-1].sent_at
        await session.commit()
    if segmented:
        await outbox.flush_rooms(ctx, tenant_id, [room_id])

    # 2. 判定
    if unavailable:
        outcome = pipeline.Outcome(action=DecisionAction.HANDOFF, reason=unavailable)
    else:
        outcome = await pipeline.evaluate(
            ctx,
            tenant_id,
            settings,
            context,
            company=company,
            session_id=session_id,
            intents=intents,
            channel=channel,
            customer_id=customer_id,
        )
    summary = outcome.summary
    if outcome.action == DecisionAction.HANDOFF and not queued and summary is None:
        summary = await pipeline.summarize(
            ctx,
            tenant_id,
            [*context.history, Turn("customer", context.question)],
            outcome.reason or "",
            session_id=session_id,
        )

    # 3. 写入
    async with ctx.db.tenant_session(tenant_id) as session:
        chat = await session.get(ChatSession, session_id, with_for_update=True)
        state = await session.get(AiSessionState, session_id, with_for_update=True)
        if chat is None or state is None:
            return
        expected = SessionStatus.QUEUED if queued else SessionStatus.AI_SERVING
        if chat.status != expected:
            # 访客在这期间点了"转人工"，或者排队的会话已经分配给坐席：不再回复。
            _release(state, lease)
            await session.commit()
            return
        if outcome.intent:
            chat.intent = outcome.intent
        signals = dict(outcome.signals)
        if outcome.guard:
            signals["guard"] = outcome.guard
        session.add(
            AiDecision(
                tenant_id=tenant_id,
                session_id=session_id,
                question=context.question,
                action=outcome.action,
                reason=outcome.reason,
                score=outcome.score,
                signals=signals,
                reply=outcome.reply,
                knowledge=outcome.knowledge,
            )
        )
        state.answered_until = answered_until
        state.last_question = context.question
        state.repeats = outcome.repeats
        state.guard_failures = outcome.guard_failures
        if outcome.action == DecisionAction.REPLY:
            state.turns += 1
        _release(state, lease)
        # 排队中的会话只发回答，不发转人工的过渡话术（客户已经在等人工）。
        if outcome.reply and not (queued and outcome.action == DecisionAction.HANDOFF):
            menu = None
            if outcome.action == DecisionAction.REPLY:
                # 微信客服：AI 的回答带一个「转人工」按钮（菜单消息）。
                kf = await menus.kf_settings(session, chat.channel_account_id)
                if kf is not None and kf.kf_handoff_menu:
                    menu = menus.handoff_menu()
            texts = (
                split_reply(outcome.reply)
                if segmented and outcome.action == DecisionAction.REPLY
                else [outcome.reply]
            )
            for index, text in enumerate(texts):
                last = index == len(texts) - 1
                outbox.enqueue_bot_message(
                    session, room_id, text, settings.bot_name, menu=menu if last else None
                )
        if outcome.action == DecisionAction.REPLY and outcome.used_items:
            await session.execute(
                update(KbItem)
                .where(KbItem.id.in_(outcome.used_items))
                .values(hits=KbItem.hits + 1, last_hit_at=now)
            )
        if outcome.action == DecisionAction.HANDOFF:
            chat.ai_summary = summary
        await session.commit()
    await outbox.flush_rooms(ctx, tenant_id, [room_id])
    if outcome.action == DecisionAction.HANDOFF and not queued:
        await engine.request_handoff(
            ctx, tenant_id, room_id, reason=outcome.reason or "ai", actor_type=engine.ActorType.AI
        )
