"""知识运营指标（设计文档 §12.7）与知识周报（§12.6）。

周报在每周一由调度进程生成上一周的内容（按 EDP_USAGE_TIMEZONE 划分），保存在 kb_digests，
控制台的知识库页查看；企业微信应用消息推送在接入企业微信（P2）后加上。
"""

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Float, and_, cast, column, func, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.dates import day_bounds, today
from app.core.permissions import Permission
from app.modules.ai.models import AiDecision, AiSuggestion, DecisionAction
from app.modules.conversation.models import (
    ChatSession,
    CloseReason,
    Message,
    SenderType,
    SessionEvent,
)
from app.modules.kb.distribution import audience
from app.modules.kb.models import (
    CandidateKind,
    CandidateStatus,
    ItemStatus,
    KbCandidate,
    KbDigest,
    KbItem,
    KbItemVersion,
    VersionChange,
)
from app.modules.kb.schemas import KbItemStat, KbItemStats, KbMetrics, KbReasonCount
from app.modules.kb.service import STALE_AFTER
from app.modules.notifications import service as notifications
from app.modules.notifications.push import notify_staff
from app.modules.tenancy.models import Tenant, TenantStatus

logger = logging.getLogger(__name__)

TOP = 10
ACCEPTED = (CandidateStatus.APPROVED, CandidateStatus.MERGED)


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


async def _top_items(
    session: AsyncSession, since: datetime, until: datetime, limit: int = TOP
) -> list[KbItemStat]:
    """AI 回复时引用最多的知识（按判定记录里的引用计数）。"""
    ref = (
        func.jsonb_array_elements(AiDecision.knowledge)
        .table_valued(column("value", JSONB))
        .render_derived()
    )
    item_id = ref.c.value["item_id"].astext
    rows = await session.execute(
        select(item_id.label("item_id"), func.count().label("n"))
        .select_from(AiDecision)
        .join(ref, ref.c.value["used"].astext == "true")
        .where(
            AiDecision.action == DecisionAction.REPLY,
            AiDecision.created_at >= since,
            AiDecision.created_at < until,
        )
        .group_by(item_id)
        .order_by(func.count().desc(), item_id)
        .limit(limit)
    )
    counts = [(uuid.UUID(i), n) for i, n in rows]
    if not counts:
        return []
    titles = dict(
        (
            await session.execute(
                select(KbItem.id, KbItem.title).where(KbItem.id.in_([i for i, _ in counts]))
            )
        ).all()
    )
    return [KbItemStat(item_id=i, title=titles[i], count=n) for i, n in counts if i in titles]


async def knowledge_metrics(
    session: AsyncSession,
    start: date,
    end: date,
    tz: ZoneInfo,
    *,
    now: datetime | None = None,
) -> KbMetrics:
    now = now or datetime.now(UTC)
    since, until = day_bounds(start, tz)[0], day_bounds(end, tz)[1]

    replies, with_knowledge = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(func.jsonb_array_length(AiDecision.knowledge) > 0),
            ).where(
                AiDecision.action == DecisionAction.REPLY,
                AiDecision.created_at >= since,
                AiDecision.created_at < until,
            )
        )
    ).one()
    reasons = [
        KbReasonCount(reason=reason or "", count=count)
        for reason, count in await session.execute(
            select(AiDecision.reason, func.count())
            .where(
                AiDecision.action == DecisionAction.HANDOFF,
                AiDecision.created_at >= since,
                AiDecision.created_at < until,
            )
            .group_by(AiDecision.reason)
            .order_by(func.count().desc(), AiDecision.reason)
        )
    ]

    gap = KbCandidate.kind == CandidateKind.GAP
    reviewed = and_(KbCandidate.reviewed_at >= since, KbCandidate.reviewed_at < until)
    hours = func.extract("epoch", KbCandidate.reviewed_at - KbCandidate.first_seen_at) / 3600
    row = (
        await session.execute(
            select(
                func.count().filter(gap, KbCandidate.status == CandidateStatus.PENDING),
                func.count().filter(
                    gap, KbCandidate.first_seen_at >= since, KbCandidate.first_seen_at < until
                ),
                func.count().filter(gap, reviewed),
                cast(func.avg(hours).filter(gap, reviewed), Float),
                func.count().filter(
                    KbCandidate.first_seen_at >= since, KbCandidate.first_seen_at < until
                ),
                func.count().filter(reviewed),
                func.count().filter(reviewed, KbCandidate.status.in_(ACCEPTED)),
            )
        )
    ).one()
    gaps_open, gaps_new, gaps_closed, gap_hours, new, reviewed_count, accepted = row

    suggestions = await session.scalar(
        select(func.count()).where(
            AiSuggestion.created_at >= since, AiSuggestion.created_at < until
        )
    )
    adopted = await session.scalar(
        select(func.count()).where(
            Message.sender_type == SenderType.AGENT,
            Message.sent_at >= since,
            Message.sent_at < until,
            Message.content["origin"].astext == "suggestion",
        )
    )
    cutoff = now - STALE_AFTER
    stale = await session.scalar(
        select(func.count()).where(
            KbItem.status == ItemStatus.PUBLISHED,
            KbItem.published_at < cutoff,
            (KbItem.last_hit_at.is_(None)) | (KbItem.last_hit_at < cutoff),
        )
    )
    disliked = [
        KbItemStat(item_id=item_id, title=title, count=dislikes)
        for item_id, title, dislikes in await session.execute(
            select(KbItem.id, KbItem.title, KbItem.dislikes)
            .where(KbItem.dislikes > 0)
            .order_by(KbItem.dislikes.desc(), KbItem.likes, KbItem.id)
            .limit(TOP)
        )
    ]
    visitor_disliked = [
        KbItemStat(item_id=item_id, title=title, count=dislikes)
        for item_id, title, dislikes in await session.execute(
            select(KbItem.id, KbItem.title, KbItem.visitor_dislikes)
            .where(KbItem.visitor_dislikes > 0)
            .order_by(KbItem.visitor_dislikes.desc(), KbItem.visitor_likes, KbItem.id)
            .limit(TOP)
        )
    ]
    return KbMetrics(
        start=start,
        end=end,
        timezone=tz.key,
        ai_replies=replies,
        knowledge_hit_rate=_rate(with_knowledge, replies),
        handoff_reasons=reasons,
        gaps_open=gaps_open,
        gaps_new=gaps_new,
        gaps_closed=gaps_closed,
        gap_close_hours=round(gap_hours, 1) if gap_hours is not None else None,
        candidates_new=new,
        candidates_reviewed=reviewed_count,
        candidates_accepted=accepted,
        pass_rate=_rate(accepted, reviewed_count),
        suggestions=suggestions or 0,
        suggestions_adopted=adopted or 0,
        adoption_rate=_rate(adopted or 0, suggestions or 0),
        stale_items=stale or 0,
        top_items=await _top_items(session, since, until),
        disliked_items=disliked,
        visitor_disliked_items=visitor_disliked,
    )


ITEM_WINDOW = timedelta(days=90)


async def item_stats(
    session: AsyncSession, item: KbItem, *, now: datetime | None = None
) -> KbItemStats:
    """一条知识的引用、员工与访客评价、引用它的 AI 会话的满意度和转人工情况。"""
    now = now or datetime.now(UTC)
    cited = (
        select(AiDecision.session_id)
        .where(
            AiDecision.created_at >= now - ITEM_WINDOW,
            AiDecision.knowledge.op("@>")(cast([{"item_id": str(item.id)}], JSONB)),
        )
        .distinct()
    )
    sessions, handoffs, rated, csat = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(ChatSession.assigned_at.is_not(None)),
                func.count(ChatSession.csat),
                func.avg(ChatSession.csat),
            ).where(ChatSession.id.in_(cited))
        )
    ).one()
    votes = item.visitor_likes + item.visitor_dislikes
    cutoff = now - STALE_AFTER
    zombie = (
        item.status == ItemStatus.PUBLISHED
        and item.published_at is not None
        and item.published_at < cutoff
        and (item.last_hit_at is None or item.last_hit_at < cutoff)
    )
    return KbItemStats(
        item_id=item.id,
        hits=item.hits,
        last_hit_at=item.last_hit_at,
        likes=item.likes,
        dislikes=item.dislikes,
        visitor_likes=item.visitor_likes,
        visitor_dislikes=item.visitor_dislikes,
        visitor_satisfaction=_rate(item.visitor_likes, votes),
        ai_sessions=sessions,
        handoff_sessions=handoffs,
        csat_count=rated,
        csat_avg=round(float(csat), 2) if csat is not None else None,
        zombie=zombie,
    )


# ---- 周报 ----


def week_of(day: date) -> date:
    """这一天所在的周（周一开始）。"""
    return day - timedelta(days=day.weekday())


async def build_digest(
    session: AsyncSession, week_start: date, tz: ZoneInfo, *, now: datetime | None = None
) -> dict[str, Any]:
    """一周的知识周报：新增与更新的知识、到期下线、AI 引用最多的知识、知识缺口 Top 10、
    候选审核情况与 AI 接待效果。"""
    since, until = day_bounds(week_start, tz)[0], day_bounds(week_start + timedelta(days=6), tz)[1]

    versions = (
        await session.execute(
            select(
                KbItemVersion.item_id,
                KbItemVersion.title,
                KbItemVersion.version,
                KbItemVersion.change,
            )
            .where(KbItemVersion.created_at >= since, KbItemVersion.created_at < until)
            .order_by(KbItemVersion.created_at)
        )
    ).all()
    created = {i for i, _, _, c in versions if c == VersionChange.CREATED}
    new_items = [
        {"item_id": str(i), "title": t, "version": v}
        for i, t, v, c in versions
        if c == VersionChange.CREATED
    ]
    # 本周新增的知识不再列入"更新"；同一条更新多次时只列最后一次。
    latest: dict[uuid.UUID, dict[str, Any]] = {}
    for i, t, v, c in versions:
        if c != VersionChange.CREATED and i not in created:
            latest[i] = {"item_id": str(i), "title": t, "version": v, "change": c}
    updated_items = list(latest.values())

    expired = [
        {"item_id": str(i), "title": t}
        for i, t in await session.execute(
            select(KbItem.id, KbItem.title).where(
                KbItem.archived_at >= since,
                KbItem.archived_at < until,
                KbItem.valid_to.is_not(None),
                KbItem.valid_to <= KbItem.archived_at,
            )
        )
    ]
    gaps = [
        {"candidate_id": str(i), "question": q, "occurrences": n}
        for i, q, n in await session.execute(
            select(KbCandidate.id, KbCandidate.question, KbCandidate.occurrences)
            .where(
                KbCandidate.kind == CandidateKind.GAP,
                KbCandidate.status == CandidateStatus.PENDING,
            )
            .order_by(KbCandidate.occurrences.desc(), KbCandidate.last_seen_at.desc())
            .limit(TOP)
        )
    ]
    pending, reviewed, accepted = (
        await session.execute(
            select(
                func.count().filter(KbCandidate.status == CandidateStatus.PENDING),
                func.count().filter(
                    KbCandidate.reviewed_at >= since, KbCandidate.reviewed_at < until
                ),
                func.count().filter(
                    KbCandidate.reviewed_at >= since,
                    KbCandidate.reviewed_at < until,
                    KbCandidate.status.in_(ACCEPTED),
                ),
            )
        )
    ).one()
    ai_started = (
        select(SessionEvent.session_id)
        .where(
            SessionEvent.type == "ai_serving",
            SessionEvent.created_at >= since,
            SessionEvent.created_at < until,
        )
        .scalar_subquery()
    )
    ai_sessions, ai_resolved = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(ChatSession.close_reason == CloseReason.AI_RESOLVED),
            ).where(ChatSession.id.in_(ai_started))
        )
    ).one()
    hot = await _top_items(session, since, until)
    return {
        "week_start": week_start.isoformat(),
        "week_end": (week_start + timedelta(days=6)).isoformat(),
        "new_items": new_items,
        "updated_items": updated_items,
        "expired_items": expired,
        "hot_items": [{"item_id": str(h.item_id), "title": h.title, "count": h.count} for h in hot],
        "top_gaps": gaps,
        "candidates": {"pending": pending, "reviewed": reviewed, "accepted": accepted},
        "ai": {
            "sessions": ai_sessions,
            "resolved": ai_resolved,
            "resolution_rate": _rate(ai_resolved, ai_sessions),
        },
        "generated_at": (now or datetime.now(UTC)).isoformat(),
    }


async def generate_digest(
    ctx: AppContext, tenant_id: uuid.UUID, week_start: date, *, now: datetime | None = None
) -> dict[str, Any]:
    """生成（或重新生成）一个租户某一周的周报。"""
    tz = ZoneInfo(ctx.settings.usage_timezone)
    async with ctx.db.tenant_session(tenant_id) as session:
        data = await build_digest(session, week_start, tz, now=now)
        statement = insert(KbDigest).values(tenant_id=tenant_id, week_start=week_start, data=data)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[KbDigest.tenant_id, KbDigest.week_start],
                set_={"data": statement.excluded.data, "created_at": func.now()},
            )
        )
        await session.commit()
    return data


async def run_digests(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：还没有上一周周报的租户生成周报，返回生成的数量。"""
    now = now or datetime.now(UTC)
    tz = ZoneInfo(ctx.settings.usage_timezone)
    last_week = week_of(today(tz, now)) - timedelta(days=7)
    async with ctx.db.platform_sessionmaker() as session:
        done = select(KbDigest.tenant_id).where(KbDigest.week_start == last_week)
        tenant_ids = list(
            (
                await session.scalars(
                    select(Tenant.id).where(
                        Tenant.status == TenantStatus.ACTIVE,
                        Tenant.created_at < day_bounds(last_week + timedelta(days=7), tz)[0],
                        Tenant.id.not_in(done),
                    )
                )
            ).all()
        )
    generated = 0
    for tenant_id in tenant_ids:
        try:
            data = await generate_digest(ctx, tenant_id, last_week, now=now)
            generated += 1
        except Exception:
            logger.exception("knowledge digest failed for tenant %s", tenant_id)
            continue
        await _announce_digest(ctx, tenant_id, data)
    return generated


async def _announce_digest(ctx: AppContext, tenant_id: uuid.UUID, data: dict[str, Any]) -> None:
    """周报生成后提醒知识管理员：站内信，有企业微信时另发应用消息。"""
    added = len(data.get("new_items") or [])
    updated = len(data.get("updated_items") or [])
    gaps = len(data.get("top_gaps") or [])
    title = f"知识周报（{data.get('week_start')} 起的一周）"
    body = f"新增 {added} 条、更新 {updated} 条，待处理的知识缺口 {gaps} 个。"
    async with ctx.db.tenant_session(tenant_id) as session:
        managers = [s.id for s in await audience(session, Permission.KB_MANAGE)]
        notifications.add(
            session,
            tenant_id,
            managers,
            kind="kb_digest",
            title=title,
            body=body,
            link="/knowledge",
        )
        await session.commit()
    await notify_staff(ctx, tenant_id, managers, title=title, description=body, path="/knowledge")
