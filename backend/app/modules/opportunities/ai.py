"""商机的 AI 部分（设计文档 §35.3、§35.4、§40.6）。

- 调度任务每 5 分钟：
  1. 跟进中、待确认的商机，这个客户的订单确认以后变成"赢单"（企业系统同步过来的订单也算）；
  2. 跟进中的客户又来咨询了（新的会话结束）：时间线上记一条，意向更高时调高等级；
  3. 最近 2 天结束的会话，最高意向达到设置的等级、之后没有下单、客户不在名单里（最近 30 天
     也没有输单或忽略）：按设置自动转入或者建议，进第一个进行中的阶段。AI 读脱敏后的会话，写出想要
     什么、顾虑和建议的跟进天数；没有 AI 或者大模型不可用时用意图判断和会话小结。
- AI 写跟进话术：按想要什么、顾虑、最近的跟进和知识库，员工修改后自己发送。
"""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, select, union
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import ServiceUnavailable
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway, intent, prompts
from app.modules.ai.models import SessionIntent, SessionSummary, SummaryStatus
from app.modules.ai.summaries import transcript
from app.modules.billing.entitlements import has_feature, require_feature
from app.modules.conversation.models import ChatSession, SessionStatus
from app.modules.customer.models import Customer
from app.modules.iam.principal import Principal
from app.modules.integration.models import WebhookEventType
from app.modules.kb import search as kb_search
from app.modules.kb.service import visibilities_for
from app.modules.opportunities import service
from app.modules.opportunities import settings as opportunity_settings
from app.modules.opportunities.models import (
    OPEN_STATUSES,
    STATUS_LABELS,
    ActivityKind,
    FollowMethod,
    Opportunity,
    OpportunityActivity,
    OpportunityLevel,
    OpportunitySource,
    OpportunityStatus,
)
from app.modules.opportunities.schemas import OpportunityDigest, OpportunityMessage
from app.modules.orders.models import STATUS_LABELS as ORDER_STATUS_LABELS
from app.modules.orders.models import Order, OrderStatus

logger = logging.getLogger(__name__)
# 意图判断的"准备下单"（§32.5）。
READY_STAGE = 4
# AI 小结读时间线的最近多少条。
SUMMARY_LINES = 15

SCENE = "opportunity"
WINDOW = timedelta(days=2)
COOLDOWN = timedelta(days=30)
BATCH = 20
MAX_TEXT = 500


def _stage_label(stage: int | None) -> str:
    return intent.stage_label(stage) or ""


def _concerns(judged: SessionIntent | None) -> list[str]:
    """意图判断的"在意什么"（价格、发货时效……）。"""
    if judged is None:
        return []
    return [intent.concern_label(c) for c in judged.concerns or [] if c != "none"]


def _parse(content: str) -> dict[str, Any] | None:
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ---- 赢单 ----


async def sweep_won(session: AsyncSession) -> int:
    """这个客户的订单确认以后（确认时间在开始跟进之后），待确认、跟进中的商机变成"赢单"。"""
    rows = (
        await session.execute(
            select(Opportunity, Order)
            .join(
                Order,
                and_(
                    Order.tenant_id == Opportunity.tenant_id,
                    Order.customer_id == Opportunity.customer_id,
                ),
            )
            .where(
                Opportunity.status.in_(OPEN_STATUSES),
                Order.status.in_(service.DEAL_STATUSES),
                Order.confirmed_at >= Opportunity.opened_at,
            )
            .order_by(Order.confirmed_at)
            .with_for_update(of=Opportunity)
        )
    ).all()
    done: set[uuid.UUID] = set()
    stages_by_tenant: dict[uuid.UUID, Any] = {}
    for opportunity, order in rows:
        if opportunity.id in done:
            continue
        done.add(opportunity.id)
        if opportunity.tenant_id not in stages_by_tenant:
            stages_by_tenant[opportunity.tenant_id] = await service.stages(
                session, opportunity.tenant_id
            )
        await service.win_by_order(
            session, opportunity, order, stages_by_tenant[opportunity.tenant_id]
        )
    return len(done)


# ---- 客户又来咨询了 ----


async def record_returns(session: AsyncSession, now: datetime) -> int:
    """跟进中的客户又来咨询了：每个新结束的会话在时间线上记一条（系统记的），意向更高时调高等级。"""
    recorded = exists().where(
        OpportunityActivity.opportunity_id == Opportunity.id,
        OpportunityActivity.session_id == ChatSession.id,
        OpportunityActivity.kind == ActivityKind.SESSION,
    )
    rows = (
        await session.execute(
            select(Opportunity, ChatSession, SessionIntent.peak_stage)
            .join(
                ChatSession,
                and_(
                    ChatSession.tenant_id == Opportunity.tenant_id,
                    ChatSession.customer_id == Opportunity.customer_id,
                ),
            )
            .outerjoin(SessionIntent, SessionIntent.session_id == ChatSession.id)
            .where(
                Opportunity.status == OpportunityStatus.ACTIVE,
                ChatSession.status == SessionStatus.CLOSED,
                ChatSession.closed_at >= now - WINDOW,
                ChatSession.created_at > Opportunity.opened_at,
                Opportunity.session_id.is_distinct_from(ChatSession.id),
                ~recorded,
            )
            .order_by(ChatSession.closed_at)
        )
    ).all()
    for opportunity, chat, stage in rows:
        summary = await _summary(session, chat)
        text = "客户又来咨询了"
        if stage is not None:
            text += f"（{_stage_label(stage)}）"
        if summary:
            text += f"：{summary}"
        service.record(
            session,
            opportunity,
            ActivityKind.SESSION,
            "客户又来咨询了" + (f"（{_stage_label(stage)}）" if stage is not None else ""),
            content=text[:MAX_TEXT],
            method=FollowMethod.CHAT,
            properties={"stage": stage},
            linked_type="session",
            linked_id=chat.id,
            session_id=chat.id,
            at=now,
        )
        level = service.level_of(stage)
        rank = service.LEVEL_RANK
        if stage is not None and rank[level] > rank[OpportunityLevel(opportunity.level)]:
            opportunity.level = level
    return len(rows)


async def record_ready(session: AsyncSession, now: datetime) -> int:
    """跟进中的商机，客户的会话里意图判断第一次到"准备下单"（§40.7）：时间线上记一条（每条商机
    一次），等级调到高。"""
    recorded = exists().where(
        OpportunityActivity.opportunity_id == Opportunity.id,
        OpportunityActivity.kind == ActivityKind.SESSION,
        OpportunityActivity.linked_type == "intent",
    )
    rows = (
        await session.execute(
            select(Opportunity, ChatSession.id, SessionIntent.peak_at)
            .join(
                ChatSession,
                and_(
                    ChatSession.tenant_id == Opportunity.tenant_id,
                    ChatSession.customer_id == Opportunity.customer_id,
                ),
            )
            .join(SessionIntent, SessionIntent.session_id == ChatSession.id)
            .where(
                Opportunity.status == OpportunityStatus.ACTIVE,
                SessionIntent.peak_stage >= READY_STAGE,
                SessionIntent.peak_at >= Opportunity.opened_at,
                ~recorded,
            )
            .order_by(SessionIntent.peak_at)
        )
    ).all()
    done: set[uuid.UUID] = set()
    for opportunity, session_id, peak_at in rows:
        if opportunity.id in done:
            continue
        done.add(opportunity.id)
        service.record(
            session,
            opportunity,
            ActivityKind.SESSION,
            "客户准备下单了（意图判断）",
            method=FollowMethod.CHAT,
            properties={"stage": READY_STAGE, "ready": True},
            linked_type="intent",
            linked_id=session_id,
            at=peak_at or now,
        )
        opportunity.level = OpportunityLevel.HIGH
    return len(done)


async def _summary(session: AsyncSession, chat: ChatSession) -> str | None:
    row = await session.get(SessionSummary, chat.id)
    if row is not None and row.status != SummaryStatus.DISCARDED and row.summary:
        return row.summary
    return chat.ai_summary


# ---- AI 转入 ----


async def candidates(session: AsyncSession, now: datetime, min_stage: int) -> list[uuid.UUID]:
    """可以转入的会话（每个客户取最近的一个）。"""
    later_order = exists().where(
        Order.tenant_id == ChatSession.tenant_id,
        Order.customer_id == ChatSession.customer_id,
        Order.created_at >= ChatSession.created_at,
        Order.status != OrderStatus.CANCELLED,
    )
    listed = exists().where(
        Opportunity.tenant_id == ChatSession.tenant_id,
        Opportunity.customer_id == ChatSession.customer_id,
        Opportunity.status.in_(OPEN_STATUSES),
    )
    given_up = exists().where(
        Opportunity.tenant_id == ChatSession.tenant_id,
        Opportunity.customer_id == ChatSession.customer_id,
        Opportunity.status.in_((OpportunityStatus.LOST, OpportunityStatus.DISMISSED)),
        Opportunity.closed_at >= now - COOLDOWN,
    )
    used = exists().where(
        Opportunity.tenant_id == ChatSession.tenant_id,
        Opportunity.session_id == ChatSession.id,
    )
    rows = (
        await session.execute(
            select(ChatSession.id, ChatSession.customer_id)
            .join(SessionIntent, SessionIntent.session_id == ChatSession.id)
            .where(
                ChatSession.status == SessionStatus.CLOSED,
                ChatSession.closed_at >= now - WINDOW,
                SessionIntent.peak_stage >= min_stage,
                ~later_order,
                ~listed,
                ~given_up,
                ~used,
            )
            .order_by(ChatSession.closed_at.desc())
        )
    ).all()
    seen: set[uuid.UUID] = set()
    result = []
    for session_id, customer_id in rows:
        if customer_id in seen:
            continue
        seen.add(customer_id)
        result.append(session_id)
    return result[:BATCH]


async def _draft(
    ctx: AppContext, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> tuple[str | None, str | None, int | None]:
    """AI 整理：想要什么、顾虑、建议的跟进天数。大模型不可用时返回 (None, None, None)。"""
    async with ctx.db.tenant_session(tenant_id) as session:
        if not await has_feature(session, tenant_id, "ai"):
            return None, None, None
        chat = await session.get(ChatSession, session_id)
        judged = await session.get(SessionIntent, session_id)
        if chat is None:
            return None, None, None
        lines = await transcript(session, session_id)
        summary = await _summary(session, chat)
    if not lines or not await ctx.llms.chat_enabled(tenant_id, SCENE):
        return None, None, None
    messages = prompts.opportunity_messages(
        transcript=lines,
        intent=_stage_label(judged.peak_stage) if judged else "",
        concerns=_concerns(judged),
        summary=summary,
    )
    try:
        result = await gateway.chat(
            ctx,
            tenant_id,
            messages,
            scene=SCENE,
            json_mode=True,
            max_tokens=400,
            session_id=session_id,
        )
    except LLMUnavailable as exc:
        logger.warning("opportunity draft failed for session %s: %s", session_id, exc)
        return None, None, None
    data = _parse(result.content) or {}
    interest = str(data.get("interest") or "").strip()[:MAX_TEXT] or None
    concerns = str(data.get("concerns") or "").strip()[:MAX_TEXT] or None
    try:
        days = int(data.get("follow_days"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        days = None
    if days is not None and not 1 <= days <= 30:
        days = None
    return interest, concerns, days


async def adopt(ctx: AppContext, tenant_id: uuid.UUID, session_id: uuid.UUID) -> str | None:
    """按会话转入商机（或者建议）。返回 "created"、"suggested"，没有转入时返回 None。"""
    interest, concerns, days = await _draft(ctx, tenant_id, session_id)
    async with ctx.db.tenant_session(tenant_id) as session:
        settings = await opportunity_settings.load(session, tenant_id)
        if settings.ai_mode == "off":
            return None
        chat = await session.get(ChatSession, session_id)
        judged = await session.get(SessionIntent, session_id)
        if chat is None or judged is None:
            return None
        customer = await session.get(Customer, chat.customer_id)
        if customer is None:
            return None
        drafted = interest is not None
        if interest is None:
            # 没有 AI 整理时：意图判断的"关心的点"和会话小结。
            points = "、".join(_concerns(judged))
            summary = await _summary(session, chat)
            interest = (
                "；".join(
                    part for part in (f"关心{points}" if points else "", summary or "") if part
                )
                or None
            )
        day = await service.today(session)
        all_stages = await service.stages(session, tenant_id)
        stage = service.open_stages(all_stages)[0]
        status = (
            OpportunityStatus.ACTIVE if settings.ai_mode == "auto" else OpportunityStatus.SUGGESTED
        )
        now = datetime.now(UTC)
        opportunity = Opportunity(
            tenant_id=tenant_id,
            customer_id=customer.id,
            name=service.default_name(None, interest, customer),
            stage_id=stage.id,
            status=status,
            level=service.level_of(judged.peak_stage),
            interest=interest[:MAX_TEXT] if interest else None,
            concerns=concerns,
            source=OpportunitySource.AI,
            session_id=chat.id,
            # 负责人：客户的归属坐席，没有时是接待的坐席，再没有时按设置轮流分配（§40.6）。
            owner_id=customer.owner_id
            or chat.assignee_id
            or await service.round_robin_owner(session, settings),
            next_follow_at=day + timedelta(days=days or settings.follow_days),
            stage_entered_at=now,
            last_activity_at=now,
        )
        session.add(opportunity)
        try:
            await session.flush()
        except IntegrityError:
            # 同时有员工转入了这个客户（同一客户只能有一条待确认、跟进中的）。
            await session.rollback()
            return None
        service.record(
            session,
            opportunity,
            ActivityKind.AI,
            "AI 按会话转入" if status == OpportunityStatus.ACTIVE else "AI 建议转入，等员工确认",
            content=interest,
            properties={
                "stage": stage.code,
                "peak_stage": judged.peak_stage,
                "drafted": drafted,
                "concerns": concerns,
            },
            linked_type="session",
            linked_id=chat.id,
            session_id=None,
            at=now,
        )
        if status == OpportunityStatus.ACTIVE:
            service.emit(
                session,
                opportunity,
                WebhookEventType.OPPORTUNITY_CREATED,
                {"stage": stage.code, "source": OpportunitySource.AI},
                actor_type="ai",
            )
        await session.commit()
    return "created" if status == OpportunityStatus.ACTIVE else "suggested"


async def scan(
    ctx: AppContext, *, now: datetime | None = None, tenant_id: uuid.UUID | None = None
) -> dict[str, int]:
    """调度任务（每 5 分钟）：赢单、客户又来咨询、AI 转入。返回各项的数量。"""
    now = now or datetime.now(UTC)
    stats = {"won": 0, "returns": 0, "ready": 0, "created": 0, "suggested": 0}
    async with ctx.db.platform_sessionmaker() as session:
        recent = select(ChatSession.tenant_id).where(
            ChatSession.status == SessionStatus.CLOSED, ChatSession.closed_at >= now - WINDOW
        )
        listed = select(Opportunity.tenant_id).where(Opportunity.status.in_(OPEN_STATUSES))
        tenant_ids = list((await session.scalars(union(recent, listed))).all())
    if tenant_id is not None:
        tenant_ids = [t for t in tenant_ids if t == tenant_id]
    for tid in tenant_ids:
        async with ctx.db.tenant_session(tid) as session:
            stats["won"] += await sweep_won(session)
            stats["returns"] += await record_returns(session, now)
            stats["ready"] += await record_ready(session, now)
            await session.commit()
            settings = await opportunity_settings.load(session, tid)
            if settings.ai_mode == "off":
                continue
            session_ids = await candidates(session, now, settings.min_stage)
        for session_id in session_ids:
            try:
                outcome = await adopt(ctx, tid, session_id)
            except Exception:
                logger.exception("opportunity adopt failed for session %s", session_id)
                continue
            if outcome:
                stats[outcome] += 1
    return stats


# ---- AI 写跟进话术 ----


async def message(
    ctx: AppContext, session: AsyncSession, principal: Principal, opportunity: Opportunity
) -> OpportunityMessage:
    await require_feature(session, principal.tenant_id, "ai")
    customer = await session.get(Customer, opportunity.customer_id)
    name = customer.display_name if customer else "客户"
    rows = (
        await session.scalars(
            select(OpportunityActivity)
            .where(
                OpportunityActivity.opportunity_id == opportunity.id,
                OpportunityActivity.kind.in_((ActivityKind.FOLLOWUP, ActivityKind.SESSION)),
                OpportunityActivity.content.is_not(None),
            )
            .order_by(OpportunityActivity.created_at.desc())
            .limit(3)
        )
    ).all()
    followups = [(r.content or "")[:200] for r in reversed(rows)]
    query = " ".join(part for part in (opportunity.interest, opportunity.concerns) if part)[:300]
    hits = (
        await kb_search.search(
            ctx,
            session,
            principal.tenant_id,
            query,
            visibilities=visibilities_for(principal),
            limit=4,
            customer_facing=True,
        )
        if query
        else []
    )
    knowledge = [(i, h.title, h.text[:600]) for i, h in enumerate(hits, start=1)]
    messages = prompts.opportunity_message_messages(
        company=principal.tenant_name,
        customer=name,
        interest=opportunity.interest or "",
        concerns=opportunity.concerns or "",
        followups=followups,
        knowledge=knowledge,
    )
    try:
        result = await gateway.chat(
            ctx, principal.tenant_id, messages, scene=SCENE, json_mode=True, max_tokens=400
        )
    except LLMUnavailable as exc:
        raise ServiceUnavailable("AI 暂时不可用，请稍后再试") from exc
    data = _parse(result.content) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        raise ServiceUnavailable("AI 没有写出话术，请稍后再试")
    used = set()
    for value in data.get("used") or []:
        try:
            used.add(int(value))
        except (TypeError, ValueError):
            continue
    titles = list(dict.fromkeys(title for i, title, _ in knowledge if i in used))
    return OpportunityMessage(text=text[:1000], knowledge=titles)


# ---- AI 小结 ----


async def summary(
    ctx: AppContext, session: AsyncSession, principal: Principal, opportunity: Opportunity
) -> OpportunityDigest:
    """AI 小结（§40.7）：按时间线、依据的会话小结和订单写三句话——现在到哪一步、客户在意什么、
    建议下一步；同时记进时间线。大模型场景"商机"，计入企业 token。"""
    await require_feature(session, principal.tenant_id, "ai")
    customer = await session.get(Customer, opportunity.customer_id)
    all_stages = await service.stages(session, principal.tenant_id)
    stage = service.stage_by_id(all_stages, opportunity.stage_id)
    rows = (
        await session.scalars(
            select(OpportunityActivity)
            .where(
                OpportunityActivity.opportunity_id == opportunity.id,
                OpportunityActivity.kind != ActivityKind.AI,
            )
            .order_by(OpportunityActivity.created_at.desc())
            .limit(SUMMARY_LINES)
        )
    ).all()
    timeline = []
    for row in reversed(rows):
        line = f"{row.created_at:%m-%d} {row.title or ''}".strip()
        if row.content:
            line += f"：{row.content[:120]}"
        timeline.append(line)
    chat_summary = None
    if opportunity.session_id is not None:
        chat = await session.get(ChatSession, opportunity.session_id)
        if chat is not None:
            chat_summary = await _summary(session, chat)
    orders = []
    if opportunity.order_id is not None:
        order = await session.get(Order, opportunity.order_id)
        if order is not None:
            status = ORDER_STATUS_LABELS.get(order.status, order.status)
            orders.append(f"订单 {order.no}：{status}，金额 {order.total:.2f} 元")
    messages = prompts.opportunity_summary_messages(
        customer=customer.display_name if customer else "客户",
        name=opportunity.name,
        stage=stage.name,
        status=STATUS_LABELS.get(opportunity.status, opportunity.status),
        amount=f"{opportunity.amount:.2f}" if opportunity.amount is not None else None,
        expected_close_at=(
            str(opportunity.expected_close_at) if opportunity.expected_close_at else None
        ),
        interest=opportunity.interest,
        concerns=opportunity.concerns,
        timeline=timeline,
        chat_summary=chat_summary,
        orders=orders,
    )
    try:
        result = await gateway.chat(
            ctx, principal.tenant_id, messages, scene=SCENE, json_mode=True, max_tokens=400
        )
    except LLMUnavailable as exc:
        raise ServiceUnavailable("AI 暂时不可用，请稍后再试") from exc
    data = _parse(result.content) or {}
    parts = [str(data.get(key) or "").strip()[:200] for key in ("status", "cares", "next")]
    if not any(parts):
        raise ServiceUnavailable("AI 没有写出小结，请稍后再试")
    text = " ".join(part for part in parts if part)
    now = datetime.now(UTC)
    service.record(
        session,
        opportunity,
        ActivityKind.AI,
        "AI 小结",
        staff_id=principal.staff_id,
        content=text,
        properties={"summary": True, "status": parts[0], "cares": parts[1], "next": parts[2]},
        at=now,
    )
    await session.commit()
    return OpportunityDigest(
        status=parts[0], cares=parts[1], next=parts[2], text=text, generated_at=now
    )
