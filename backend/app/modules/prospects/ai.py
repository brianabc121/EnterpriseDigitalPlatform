"""意向客户的 AI 部分（设计文档 §35.3、§35.4）。

- 调度任务每 5 分钟：
  1. 跟进中、待确认的意向记录，这个客户的订单确认以后变成"已成交"（企业系统同步过来的订单也算）；
  2. 跟进中的客户又来咨询了（新的会话结束）：记一条跟进记录，意向更高时调高等级；
  3. 最近 2 天结束的会话，最高意向达到设置的等级、之后没有下单、客户不在名单里（最近 30 天
     也没有被放弃或忽略）：按设置自动转入或者建议。AI 读脱敏后的会话，写出想要什么、顾虑和
     建议的跟进天数；没有 AI 或者大模型不可用时用意图判断和会话小结。
- AI 写跟进话术：按意向、顾虑、最近的跟进和知识库，员工修改后自己发送。
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
from app.modules.kb import search as kb_search
from app.modules.kb.service import visibilities_for
from app.modules.orders.models import Order, OrderStatus
from app.modules.prospects import service
from app.modules.prospects import settings as prospect_settings
from app.modules.prospects.models import (
    OPEN_STATUSES,
    CustomerProspect,
    FollowMethod,
    ProspectFollowup,
    ProspectLevel,
    ProspectSource,
    ProspectStatus,
)
from app.modules.prospects.schemas import ProspectMessage

logger = logging.getLogger(__name__)

SCENE = "prospect"
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


# ---- 成交 ----


async def sweep_won(session: AsyncSession) -> int:
    """这个客户的订单确认以后（确认时间在开始跟进之后），待确认、跟进中的意向记录变成"已成交"。"""
    rows = (
        await session.execute(
            select(CustomerProspect, Order)
            .join(
                Order,
                and_(
                    Order.tenant_id == CustomerProspect.tenant_id,
                    Order.customer_id == CustomerProspect.customer_id,
                ),
            )
            .where(
                CustomerProspect.status.in_(OPEN_STATUSES),
                Order.status.in_(service.DEAL_STATUSES),
                Order.confirmed_at >= CustomerProspect.opened_at,
            )
            .order_by(Order.confirmed_at)
            .with_for_update(of=CustomerProspect)
        )
    ).all()
    done: set[uuid.UUID] = set()
    for prospect, order in rows:
        if prospect.id in done:
            continue
        done.add(prospect.id)
        prospect.order_id = order.id
        prospect.status = ProspectStatus.WON
        prospect.closed_at = order.confirmed_at
        prospect.closed_by = None
        prospect.updated_at = datetime.now(UTC)
    return len(done)


# ---- 客户又来咨询了 ----


async def record_returns(session: AsyncSession, now: datetime) -> int:
    """跟进中的客户又来咨询了：每个新结束的会话记一条跟进记录（系统记的），意向更高时调高等级。"""
    recorded = exists().where(
        ProspectFollowup.prospect_id == CustomerProspect.id,
        ProspectFollowup.session_id == ChatSession.id,
    )
    rows = (
        await session.execute(
            select(CustomerProspect, ChatSession, SessionIntent.peak_stage)
            .join(
                ChatSession,
                and_(
                    ChatSession.tenant_id == CustomerProspect.tenant_id,
                    ChatSession.customer_id == CustomerProspect.customer_id,
                ),
            )
            .outerjoin(SessionIntent, SessionIntent.session_id == ChatSession.id)
            .where(
                CustomerProspect.status == ProspectStatus.ACTIVE,
                ChatSession.status == SessionStatus.CLOSED,
                ChatSession.closed_at >= now - WINDOW,
                ChatSession.created_at > CustomerProspect.opened_at,
                CustomerProspect.session_id.is_distinct_from(ChatSession.id),
                ~recorded,
            )
            .order_by(ChatSession.closed_at)
        )
    ).all()
    for prospect, chat, stage in rows:
        summary = await _summary(session, chat)
        text = "客户又来咨询了"
        if stage is not None:
            text += f"（{_stage_label(stage)}）"
        if summary:
            text += f"：{summary}"
        session.add(
            ProspectFollowup(
                tenant_id=prospect.tenant_id,
                prospect_id=prospect.id,
                method=FollowMethod.CHAT,
                content=text[:MAX_TEXT],
                session_id=chat.id,
            )
        )
        level = service.level_of(stage)
        rank = service.LEVEL_RANK
        if stage is not None and rank[level] > rank[ProspectLevel(prospect.level)]:
            prospect.level = level
        prospect.updated_at = now
    return len(rows)


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
        CustomerProspect.tenant_id == ChatSession.tenant_id,
        CustomerProspect.customer_id == ChatSession.customer_id,
        CustomerProspect.status.in_(OPEN_STATUSES),
    )
    given_up = exists().where(
        CustomerProspect.tenant_id == ChatSession.tenant_id,
        CustomerProspect.customer_id == ChatSession.customer_id,
        CustomerProspect.status.in_((ProspectStatus.LOST, ProspectStatus.DISMISSED)),
        CustomerProspect.closed_at >= now - COOLDOWN,
    )
    used = exists().where(
        CustomerProspect.tenant_id == ChatSession.tenant_id,
        CustomerProspect.session_id == ChatSession.id,
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
    messages = prompts.prospect_messages(
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
        logger.warning("prospect draft failed for session %s: %s", session_id, exc)
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
    """按会话转入意向客户（或者建议）。返回 "created"、"suggested"，没有转入时返回 None。"""
    interest, concerns, days = await _draft(ctx, tenant_id, session_id)
    async with ctx.db.tenant_session(tenant_id) as session:
        settings = await prospect_settings.load(session, tenant_id)
        if settings.ai_mode == "off":
            return None
        chat = await session.get(ChatSession, session_id)
        judged = await session.get(SessionIntent, session_id)
        if chat is None or judged is None:
            return None
        customer = await session.get(Customer, chat.customer_id)
        if customer is None:
            return None
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
        status = ProspectStatus.ACTIVE if settings.ai_mode == "auto" else ProspectStatus.SUGGESTED
        prospect = CustomerProspect(
            tenant_id=tenant_id,
            customer_id=customer.id,
            status=status,
            level=service.level_of(judged.peak_stage),
            interest=interest[:MAX_TEXT] if interest else None,
            concerns=concerns,
            source=ProspectSource.AI,
            session_id=chat.id,
            follower_id=customer.owner_id or chat.assignee_id,
            next_follow_at=day + timedelta(days=days or settings.follow_days),
        )
        session.add(prospect)
        try:
            await session.commit()
        except IntegrityError:
            # 同时有员工转入了这个客户（同一客户只能有一条待确认、跟进中的）。
            await session.rollback()
            return None
    return "created" if status == ProspectStatus.ACTIVE else "suggested"


async def scan(
    ctx: AppContext, *, now: datetime | None = None, tenant_id: uuid.UUID | None = None
) -> dict[str, int]:
    """调度任务（每 5 分钟）：成交、客户又来咨询、AI 转入。返回各项的数量。"""
    now = now or datetime.now(UTC)
    stats = {"won": 0, "returns": 0, "created": 0, "suggested": 0}
    async with ctx.db.platform_sessionmaker() as session:
        recent = select(ChatSession.tenant_id).where(
            ChatSession.status == SessionStatus.CLOSED, ChatSession.closed_at >= now - WINDOW
        )
        listed = select(CustomerProspect.tenant_id).where(
            CustomerProspect.status.in_(OPEN_STATUSES)
        )
        tenant_ids = list((await session.scalars(union(recent, listed))).all())
    if tenant_id is not None:
        tenant_ids = [t for t in tenant_ids if t == tenant_id]
    for tid in tenant_ids:
        async with ctx.db.tenant_session(tid) as session:
            stats["won"] += await sweep_won(session)
            stats["returns"] += await record_returns(session, now)
            await session.commit()
            settings = await prospect_settings.load(session, tid)
            if settings.ai_mode == "off":
                continue
            session_ids = await candidates(session, now, settings.min_stage)
        for session_id in session_ids:
            try:
                outcome = await adopt(ctx, tid, session_id)
            except Exception:
                logger.exception("prospect adopt failed for session %s", session_id)
                continue
            if outcome:
                stats[outcome] += 1
    return stats


# ---- AI 写跟进话术 ----


async def message(
    ctx: AppContext, session: AsyncSession, principal: Principal, prospect: CustomerProspect
) -> ProspectMessage:
    await require_feature(session, principal.tenant_id, "ai")
    customer = await session.get(Customer, prospect.customer_id)
    name = customer.display_name if customer else "客户"
    rows = (
        await session.scalars(
            select(ProspectFollowup)
            .where(ProspectFollowup.prospect_id == prospect.id)
            .order_by(ProspectFollowup.created_at.desc())
            .limit(3)
        )
    ).all()
    followups = [r.content[:200] for r in reversed(rows)]
    query = " ".join(part for part in (prospect.interest, prospect.concerns) if part)[:300]
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
    messages = prompts.prospect_message_messages(
        company=principal.tenant_name,
        customer=name,
        interest=prospect.interest or "",
        concerns=prospect.concerns or "",
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
    return ProspectMessage(text=text[:1000], knowledge=titles)
