"""知识审核台（设计文档 §12.5）：处理从会话提炼的候选。

- 新问题、知识缺口：通过（可以先编辑，缺口需要补充答案）后新建为问答并发布；
- 相似问法：通过后把问法并入原问答；
- 冲突：通过后用候选的答案更新原问答（新版本，可回滚）；
- 任意候选都可以合并到审核人选定的已有知识，或驳回（需填写理由，用于改进提炼）。
可选的自动通过规则只适用于"给已有问答增加相似问法"，且要求证据不少于 3 条、相似度足够高。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, NotFound, Unprocessable
from app.modules.audit.service import record_audit
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.kb import service
from app.modules.kb.models import (
    CandidateKind,
    CandidateStatus,
    ItemSource,
    ItemStatus,
    KbCandidate,
    KbItem,
    VersionChange,
)
from app.modules.kb.schemas import (
    KbCandidateApprove,
    KbCandidateDetail,
    KbCandidateMerge,
    KbCandidateOut,
    KbCandidatePage,
    KbEvidence,
    KbItemCreate,
    KbSearchHit,
)
from app.modules.kb.search import search
from app.modules.kb.text import normalize
from app.modules.quickreply.models import QuickReply

RECENT = timedelta(days=7)
CANDIDATE_NOT_FOUND = "候选不存在"


def variants(candidate: KbCandidate) -> list[str]:
    """候选及其证据里出现过的全部问法（去重，保持先后）。"""
    found = [candidate.question]
    for entry in candidate.evidence:
        question = entry.get("question") if isinstance(entry, dict) else None
        if isinstance(question, str) and question.strip():
            found.append(question.strip())
    return list(dict.fromkeys(found))


def _recent(candidate: KbCandidate, now: datetime) -> int:
    count = 0
    for entry in candidate.evidence:
        try:
            seen = datetime.fromisoformat(str(entry.get("seen_at")))
        except (TypeError, ValueError, AttributeError):
            continue
        count += seen >= now - RECENT
    return count


def candidate_out(
    candidate: KbCandidate,
    *,
    target_titles: dict[uuid.UUID, str],
    reviewers: dict[uuid.UUID, str],
    now: datetime,
) -> KbCandidateOut:
    return KbCandidateOut(
        id=candidate.id,
        kind=candidate.kind,
        status=candidate.status,
        question=candidate.question,
        answer=candidate.answer,
        category=candidate.category,
        target_item_id=candidate.target_item_id,
        target_title=target_titles.get(candidate.target_item_id)
        if candidate.target_item_id
        else None,
        similarity=candidate.similarity,
        confidence=candidate.confidence,
        time_sensitive=candidate.time_sensitive,
        occurrences=candidate.occurrences,
        recent=_recent(candidate, now),
        variants=variants(candidate),
        first_seen_at=candidate.first_seen_at,
        last_seen_at=candidate.last_seen_at,
        review_note=candidate.review_note,
        reviewed_at=candidate.reviewed_at,
        reviewed_by_name=reviewers.get(candidate.reviewed_by) if candidate.reviewed_by else None,
        result_item_id=candidate.result_item_id,
        model=candidate.model,
        prompt_version=candidate.prompt_version,
    )


async def _names(
    session: AsyncSession, candidates: list[KbCandidate]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    targets = {c.target_item_id for c in candidates if c.target_item_id}
    reviewers = {c.reviewed_by for c in candidates if c.reviewed_by}
    titles: dict[uuid.UUID, str] = {}
    names: dict[uuid.UUID, str] = {}
    if targets:
        rows = await session.execute(select(KbItem.id, KbItem.title).where(KbItem.id.in_(targets)))
        titles = {item_id: title for item_id, title in rows}
    if reviewers:
        rows = await session.execute(
            select(Staff.id, Staff.display_name).where(Staff.id.in_(reviewers))
        )
        names = {staff_id: name for staff_id, name in rows}
    return titles, names


async def list_candidates(
    session: AsyncSession,
    *,
    status: str,
    kind: str | None,
    limit: int,
    offset: int,
    now: datetime | None = None,
) -> KbCandidatePage:
    """按影响排序：出现次数多、最近还在出现的在前（设计 §12.5）。"""
    now = now or datetime.now(UTC)
    query = select(KbCandidate).where(KbCandidate.status == status)
    if kind:
        query = query.where(KbCandidate.kind == kind)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    order = (
        (KbCandidate.occurrences.desc(), KbCandidate.last_seen_at.desc())
        if status == CandidateStatus.PENDING
        else (KbCandidate.reviewed_at.desc().nulls_last(), KbCandidate.updated_at.desc())
    )
    rows = list(
        await session.scalars(query.order_by(*order, KbCandidate.id).limit(limit).offset(offset))
    )
    counts = dict(
        (
            await session.execute(
                select(KbCandidate.kind, func.count())
                .where(KbCandidate.status == CandidateStatus.PENDING)
                .group_by(KbCandidate.kind)
            )
        ).all()
    )
    titles, names = await _names(session, rows)
    return KbCandidatePage(
        items=[candidate_out(c, target_titles=titles, reviewers=names, now=now) for c in rows],
        total=total or 0,
        pending={k.value: counts.get(k.value, 0) for k in CandidateKind},
    )


async def _get(
    session: AsyncSession, candidate_id: uuid.UUID, *, lock: bool = False
) -> KbCandidate:
    query = select(KbCandidate).where(KbCandidate.id == candidate_id)
    candidate = await session.scalar(query.with_for_update() if lock else query)
    if candidate is None:
        raise NotFound(CANDIDATE_NOT_FOUND)
    return candidate


async def candidate_detail(
    ctx: AppContext, session: AsyncSession, principal: Principal, candidate_id: uuid.UUID
) -> KbCandidateDetail:
    """候选、证据对话（已脱敏）、原问答，以及相似的已有知识（便于判断合并还是新建）。"""
    candidate = await _get(session, candidate_id)
    titles, names = await _names(session, [candidate])
    base = candidate_out(candidate, target_titles=titles, reviewers=names, now=datetime.now(UTC))
    target = (
        await session.get(KbItem, candidate.target_item_id) if candidate.target_item_id else None
    )
    hits = await search(
        ctx,
        session,
        principal.tenant_id,
        candidate.question,
        visibilities=service.visibilities_for(principal),
        limit=3,
    )
    return KbCandidateDetail(
        **base.model_dump(),
        evidence=[KbEvidence.model_validate(e) for e in candidate.evidence],
        target=service.item_out(target) if target else None,
        similar=[
            KbSearchHit(
                item_id=h.item_id,
                kind=h.kind,
                title=h.title,
                text=h.text,
                score=round(h.score, 4),
                dense=None if h.dense is None else round(h.dense, 4),
                lexical=h.lexical,
            )
            for h in hits
        ],
    )


def _pending(candidate: KbCandidate) -> None:
    if candidate.status != CandidateStatus.PENDING:
        raise Conflict("这条候选已经处理过了")


def _reviewed(
    session: AsyncSession,
    principal: Principal | None,
    candidate: KbCandidate,
    status: str,
    *,
    item: KbItem | None,
    note: str | None = None,
    now: datetime | None = None,
) -> None:
    candidate.status = status
    candidate.reviewed_by = principal.staff_id if principal else None
    candidate.reviewed_at = now or datetime.now(UTC)
    candidate.result_item_id = item.id if item else None
    candidate.review_note = note
    record_audit(
        session,
        action=f"kb_candidate.{status}",
        actor_type="staff" if principal else "system",
        actor_id=principal.staff_id if principal else None,
        tenant_id=candidate.tenant_id,
        resource_type="kb_candidate",
        resource_id=str(candidate.id),
        detail={"kind": candidate.kind, "item_id": str(item.id) if item else None, "note": note},
        ip=None,
    )


async def revise(
    ctx: AppContext,
    session: AsyncSession,
    item: KbItem,
    *,
    staff_id: uuid.UUID | None,
    questions: list[str] | None = None,
    content: str | None = None,
    change: str = VersionChange.MERGED,
    note: str | None = None,
) -> bool:
    """给已有知识补充问法、替换答案；已发布的知识生成新版本并立即生效。返回是否有改动。"""
    changed = False
    if questions:
        known = {normalize(q) for q in [item.title, *item.questions]}
        added = list({normalize(q): q for q in questions if normalize(q) not in known}.values())
        if added:
            item.questions = list(dict.fromkeys([*item.questions, *added]))[:50]
            changed = True
    if content and content.strip() and content.strip() != item.content:
        item.content = content.strip()
        changed = True
    if not changed:
        return False
    item.updated_by = staff_id
    if item.status == ItemStatus.PUBLISHED:
        item.version += 1
        await service.reindex(ctx, session, item)
        await service.snapshot(session, item, change, staff_id, note=note)
    return True


async def approve(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    candidate_id: uuid.UUID,
    payload: KbCandidateApprove,
) -> KbCandidateOut:
    candidate = await _get(session, candidate_id, lock=True)
    _pending(candidate)
    question = (payload.question or candidate.question).strip()
    answer = (payload.answer or candidate.answer or "").strip()
    target = (
        await session.get(KbItem, candidate.target_item_id, with_for_update=True)
        if candidate.target_item_id
        else None
    )
    if candidate.kind == CandidateKind.PHRASE:
        # 优秀话术：通过后成为共享快捷话术（不进入知识库）。
        if not answer:
            raise Unprocessable("请填写话术内容")
        session.add(
            QuickReply(
                tenant_id=candidate.tenant_id,
                owner_id=None,
                category=(payload.category or candidate.category or "优秀话术")[:32],
                title=question[:64],
                content=answer,
            )
        )
        _reviewed(
            session,
            principal,
            candidate,
            CandidateStatus.APPROVED,
            item=None,
            note="已加入共享话术",
        )
    elif candidate.kind == CandidateKind.SIMILAR and target is not None:
        await revise(
            ctx,
            session,
            target,
            staff_id=principal.staff_id,
            questions=[question, *variants(candidate)],
            note=f"合并候选问法：{question}",
        )
        _reviewed(session, principal, candidate, CandidateStatus.MERGED, item=target)
    elif candidate.kind == CandidateKind.CONFLICT and target is not None:
        if not answer:
            raise Unprocessable("请填写答案")
        await revise(
            ctx,
            session,
            target,
            staff_id=principal.staff_id,
            content=answer,
            change=VersionChange.UPDATED,
            note="按审核台候选更新答案",
        )
        _reviewed(session, principal, candidate, CandidateStatus.APPROVED, item=target)
    else:
        if not answer:
            raise Unprocessable("知识缺口需要补充答案后才能通过")
        others = [q for q in variants(candidate) if q != question]
        item = await service.create_item(
            ctx,
            session,
            principal,
            KbItemCreate(
                kind="faq",
                title=question,
                content=answer,
                questions=others[:50],
                category=(payload.category if payload.category is not None else candidate.category),
                visibility=payload.visibility or "public",
                publish=True,
            ),
            source=ItemSource.EXTRACTED,
            commit=False,
        )
        _reviewed(session, principal, candidate, CandidateStatus.APPROVED, item=item)
        target = item
    await session.commit()
    return await _out(session, candidate)


async def merge(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    candidate_id: uuid.UUID,
    payload: KbCandidateMerge,
) -> KbCandidateOut:
    """合并到审核人选定的已有知识：并入问法，可以同时替换答案。"""
    candidate = await _get(session, candidate_id, lock=True)
    _pending(candidate)
    if candidate.kind == CandidateKind.PHRASE:
        raise Unprocessable("话术候选不能合并到知识，请直接通过或驳回")
    item = await session.get(KbItem, payload.item_id, with_for_update=True)
    if item is None:
        raise NotFound(service.ITEM_NOT_FOUND)
    await revise(
        ctx,
        session,
        item,
        staff_id=principal.staff_id,
        questions=variants(candidate),
        content=payload.answer,
        note=f"合并候选：{candidate.question}",
    )
    _reviewed(session, principal, candidate, CandidateStatus.MERGED, item=item)
    await session.commit()
    return await _out(session, candidate)


async def reject(
    session: AsyncSession, principal: Principal, candidate_id: uuid.UUID, reason: str
) -> KbCandidateOut:
    candidate = await _get(session, candidate_id, lock=True)
    _pending(candidate)
    _reviewed(
        session, principal, candidate, CandidateStatus.REJECTED, item=None, note=reason.strip()
    )
    await session.commit()
    return await _out(session, candidate)


async def auto_merge(
    ctx: AppContext, session: AsyncSession, candidate: KbCandidate, item: KbItem, *, now: datetime
) -> None:
    """自动通过（默认关闭）：相似问法证据足够多时直接并入原问答。"""
    await revise(
        ctx,
        session,
        item,
        staff_id=None,
        questions=variants(candidate),
        note=f"自动合并问法：{candidate.question}",
    )
    _reviewed(
        session,
        None,
        candidate,
        CandidateStatus.MERGED,
        item=item,
        note=f"自动合并（证据 {candidate.occurrences} 条）",
        now=now,
    )


async def _out(session: AsyncSession, candidate: KbCandidate) -> KbCandidateOut:
    await session.refresh(candidate)
    titles, names = await _names(session, [candidate])
    return candidate_out(candidate, target_titles=titles, reviewers=names, now=datetime.now(UTC))
