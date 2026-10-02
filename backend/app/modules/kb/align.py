"""知识库整理：让知识库和企业现行的规章制度保持一致（设计文档 §33.7）。

由 AI 唤醒（wake/runner.py）在租户会话里调用，每周一次、规章制度变化后、或者手动"立即整理"：

1. 与制度冲突：每条已发布的问答（不是制度的）在现行制度里检索最相关的几段；知识或相关制度的版本变了
   才交给大模型核对，冲突时生成审核台的"冲突"建议（按制度改写的答案、依据的制度原文）。
2. 制度里有、知识库里没有：现行制度逐段检查，问答里没有讲到的让大模型写成问答 → 审核台"新问题"。
3. 重复的知识：标准问几乎一样的两条问答 → 审核台"重复"（通过时合并、另一条下线）。
4. 整理清单：长期没用到、即将到期、没有负责人、评价差的知识数量。

核对过的对象记在 kb_align_marks（签名是核对时的版本），签名不变时不再核对；每次核对的条数有上限，
超出的 30 分钟后继续。AI 不直接改知识：建议都由人在审核台确认（§33.2 原则 4）。

增量更新索引（§33.9）：先比对 tenant_data_index 里知识库的变化编号和现行制度的指纹，都和上次整理
完时一样就跳过 1–3；有变化时只核对 change_seq 和上次核对时不同的知识（只读编号和增量字段，不读
正文），没变的不再检索、不再调用大模型。
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import Float, and_, cast, delete, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.context import AppContext
from app.core.permissions import Permission
from app.db.types import vector_literal
from app.integrations.llm import LLMUnavailable
from app.modules.ai import answer_cache, gateway, pii, prompts
from app.modules.changes import service as changes
from app.modules.iam.models import Staff, StaffStatus
from app.modules.kb.models import (
    CandidateKind,
    CandidateSource,
    CandidateStatus,
    ChunkKind,
    ItemKind,
    ItemStatus,
    KbAlignMark,
    KbCandidate,
    KbChunk,
    KbItem,
    Visibility,
)
from app.modules.kb.search import Hit, search
from app.modules.kb.service import EXPIRING_WITHIN, STALE_AFTER
from app.modules.kb.text import normalize, terms
from app.modules.notifications import service as notifications
from app.modules.todos import assign
from app.modules.wake import queue
from app.modules.wake import state as check_state
from app.modules.wake.models import RunKind, RunStatus, RunTrigger, WakeCheckState, WakeRun
from app.modules.wake.settings import WakeSettings

logger = logging.getLogger(__name__)

ALL_VISIBILITIES = (Visibility.PUBLIC.value, Visibility.AGENT.value, Visibility.ADMIN.value)
# 和一条知识相关的制度段落：最多几段、相关度至少多少。
RELATED = 3
RELATED_SCORE = 0.5
# 问答已经讲到这段制度（或者这个问题）的相关度。
COVERED_SCORE = 0.75
# 重复的问答：标准问的向量相似度。
DUPLICATE_SIMILARITY = 0.93
# 每次最多核对多少条知识（大模型）、检索多少条知识、核对多少段制度、多少条问答找重复。
MAX_ITEMS = 100
MAX_SEARCHES = 500
MAX_SECTIONS = 40
MAX_DUPLICATES = 200
KNOWLEDGE_CHARS = 1500
POLICY_CHARS = 800
SECTION_CHARS = 1500
EVIDENCE_CHARS = 300
KIND = "kb_align"
REVIEW_PATH = "/knowledge?tab=review"
# 检查状态（wake_check_state）里知识库整理的那一行；整理只读知识库（kb_items）。
STATE = "kb_align"
DOMAINS = ("kb_items",)
# 整理口径的版本：核对的方法改了时加 1，之前的核对状态作废。
VERSION = 1


@dataclass
class Notice:
    staff_ids: list[uuid.UUID]
    title: str
    body: str
    path: str


@dataclass
class Report:
    stats: dict[str, Any] = field(default_factory=dict)
    notices: list[Notice] = field(default_factory=list)


@dataclass
class _Budget:
    items: int = MAX_ITEMS
    searches: int = MAX_SEARCHES
    sections: int = MAX_SECTIONS
    exhausted: bool = False


def signature(*parts: object) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:40]


def policy_fingerprint(policies: list[KbItem]) -> str:
    """现行制度的指纹：哪些制度、各自的版本（制度改了、生效或者到期了，指纹就变）。"""
    return signature(VERSION, *sorted(f"{p.id}:{p.version}" for p in policies))


def _faq() -> ColumnElement[bool]:
    """参与整理的知识：已发布的问答（不是制度）。"""
    return and_(
        KbItem.status == ItemStatus.PUBLISHED,
        KbItem.kind == ItemKind.FAQ,
        KbItem.policy.is_(False),
    )


def _json(content: str) -> dict[str, Any] | None:
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def current_policies(session: AsyncSession, now: datetime) -> list[KbItem]:
    """现行制度：已发布、在有效期内的规章制度。"""
    rows = await session.scalars(
        select(KbItem)
        .where(
            KbItem.policy.is_(True),
            KbItem.status == ItemStatus.PUBLISHED,
            or_(KbItem.valid_from.is_(None), KbItem.valid_from <= now),
            or_(KbItem.valid_to.is_(None), KbItem.valid_to > now),
        )
        .order_by(KbItem.title)
    )
    return list(rows.all())


async def _marks(session: AsyncSession, keys: list[str]) -> dict[str, KbAlignMark]:
    if not keys:
        return {}
    rows = await session.scalars(select(KbAlignMark).where(KbAlignMark.key.in_(keys)))
    return {m.key: m for m in rows.all()}


async def _mark(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    key: str,
    sig: str,
    verdict: str,
    now: datetime,
    candidate_id: uuid.UUID | None = None,
    *,
    item_seq: int | None = None,
    policy_sig: str | None = None,
) -> None:
    values = {
        "signature": sig,
        "verdict": verdict,
        "candidate_id": candidate_id,
        "item_seq": item_seq,
        "policy_sig": policy_sig,
        "checked_at": now,
    }
    await session.execute(
        insert(KbAlignMark)
        .values(tenant_id=tenant_id, key=key[:160], **values)
        .on_conflict_do_update(index_elements=["tenant_id", "key"], set_=values)
    )


async def _vectors(
    session: AsyncSession, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[float] | None]:
    """每条知识第一个检索单元的向量（问答是标准问），用来检索相关的制度，不必再调用向量接口。"""
    if not item_ids:
        return {}
    rows = await session.execute(
        select(KbChunk.item_id, KbChunk.embedding)
        .where(KbChunk.item_id.in_(item_ids))
        .order_by(KbChunk.item_id, KbChunk.created_at, KbChunk.id)
        .ext(distinct_on(KbChunk.item_id))
    )
    return {item_id: (list(v) if v is not None else None) for item_id, v in rows}


async def _chat(
    ctx: AppContext, tenant_id: uuid.UUID, messages: list[dict[str, str]], usage: dict[str, Any]
) -> dict[str, Any] | None:
    try:
        result = await gateway.chat(
            ctx, tenant_id, messages, scene="kb_align", fast=True, json_mode=True, max_tokens=900
        )
    except LLMUnavailable as exc:
        logger.warning("kb alignment call for tenant %s failed: %s", tenant_id, exc)
        usage["llm_errors"] = usage.get("llm_errors", 0) + 1
        return None
    usage["llm_calls"] = usage.get("llm_calls", 0) + 1
    usage["llm_cost"] = round(usage.get("llm_cost", 0.0) + result.cost, 4)
    return _json(result.content)


async def _pending_policy_candidate(
    session: AsyncSession, kind: str, target_item_id: uuid.UUID
) -> KbCandidate | None:
    return await session.scalar(
        select(KbCandidate)
        .where(
            KbCandidate.status == CandidateStatus.PENDING,
            KbCandidate.source == CandidateSource.POLICY,
            KbCandidate.kind == kind,
            KbCandidate.target_item_id == target_item_id,
        )
        .limit(1)
        .with_for_update()
    )


# ---- 1. 与制度冲突 ----


def _knowledge_text(item: KbItem) -> str:
    return f"问题：{item.title}\n答案：{item.content}"[:KNOWLEDGE_CHARS]


async def _check_conflicts(
    ctx: AppContext,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    policies: list[KbItem],
    fingerprint: str,
    budget: _Budget,
    stats: dict[str, Any],
    now: datetime,
) -> list[KbCandidate]:
    versions = {p.id: p.version for p in policies}
    titles = {p.id: p.title for p in policies}
    # 先只读编号和增量字段：change_seq 和上次核对时一样、现行制度也没变的知识不用再检索。
    rows = (
        await session.execute(
            select(KbItem.id, KbItem.change_seq)
            .where(_faq())
            .order_by(KbItem.hits.desc(), KbItem.updated_at.desc(), KbItem.id)
        )
    ).all()
    marks = await _marks(session, [f"item:{item_id}" for item_id, _ in rows])
    changed: list[uuid.UUID] = []
    for item_id, seq in rows:
        mark = marks.get(f"item:{item_id}")
        if mark is not None and mark.item_seq == seq and mark.policy_sig == fingerprint:
            stats["unchanged"] += 1
        else:
            changed.append(item_id)
    stats["items"] = len(rows)
    batch = changed[: budget.searches]
    if len(changed) > len(batch):
        budget.exhausted = True
    loaded = {
        i.id: i for i in (await session.scalars(select(KbItem).where(KbItem.id.in_(batch)))).all()
    }
    vectors = await _vectors(session, batch)
    found: list[KbCandidate] = []
    for item_id in batch:
        item = loaded.get(item_id)
        if item is None:
            continue
        key = f"item:{item.id}"
        budget.searches -= 1
        stats["searched"] += 1
        hits: list[Hit] = await search(
            ctx,
            session,
            tenant_id,
            f"{item.title}\n{item.content[:300]}",
            visibilities=ALL_VISIBILITIES,
            limit=RELATED,
            min_score=RELATED_SCORE,
            vector=vectors.get(item.id),
            rerank=False,
            policy=True,
            now=now,
        )
        hits = [h for h in hits if h.item_id in versions]
        sig = signature(item.version, *sorted(f"{h.item_id}:{versions[h.item_id]}" for h in hits))
        mark = marks.get(key)
        if mark is not None and mark.signature == sig:
            # 内容和相关制度的版本都没变（例如只改了分类）：不用再问大模型。
            await _mark(
                session,
                tenant_id,
                key,
                sig,
                mark.verdict,
                now,
                mark.candidate_id,
                item_seq=item.change_seq,
                policy_sig=fingerprint,
            )
            stats["unchanged"] += 1
            continue
        if not hits:
            await _mark(
                session,
                tenant_id,
                key,
                sig,
                "unrelated",
                now,
                item_seq=item.change_seq,
                policy_sig=fingerprint,
            )
            stats["unrelated"] += 1
            continue
        if budget.items <= 0:
            budget.exhausted = True
            break
        budget.items -= 1
        stats["checked"] += 1
        passages = "\n\n".join(f"《{h.title}》\n{h.text[:POLICY_CHARS]}" for h in hits)
        knowledge, mapping = pii.mask(_knowledge_text(item))
        passages, _ = pii.mask(passages, mapping)
        data = await _chat(
            ctx,
            tenant_id,
            prompts.kb_align_messages(knowledge=knowledge, policies=passages),
            stats,
        )
        if data is None:
            stats["errors"] += 1
            continue
        verdict = str(data.get("verdict") or "").strip().lower()
        answer = pii.unmask(str(data.get("answer") or "").strip(), mapping)
        if verdict != "conflict" or not answer or answer == item.content.strip():
            label = verdict if verdict in ("consistent", "unrelated") else "consistent"
            await _mark(
                session,
                tenant_id,
                key,
                sig,
                label,
                now,
                item_seq=item.change_seq,
                policy_sig=fingerprint,
            )
            stats[label] += 1
            continue
        reason = pii.unmask(str(data.get("reason") or "").strip(), mapping)[:200]
        clause = pii.unmask(str(data.get("clause") or "").strip(), mapping)[:EVIDENCE_CHARS]
        evidence = [
            {
                "kind": "policy",
                "policy_item_id": str(hits[0].item_id),
                "policy_title": titles.get(hits[0].item_id, hits[0].title),
                "excerpt": clause or hits[0].text[:EVIDENCE_CHARS],
                "reason": reason,
                "seen_at": now.isoformat(),
            }
        ]
        candidate = await _pending_policy_candidate(session, CandidateKind.CONFLICT, item.id)
        if candidate is None:
            candidate = KbCandidate(
                tenant_id=tenant_id,
                kind=CandidateKind.CONFLICT,
                source=CandidateSource.POLICY,
                question=item.title,
                answer=answer,
                category=item.category,
                target_item_id=item.id,
                similarity=round(hits[0].score, 4),
                evidence=evidence,
                terms=terms(item.title),
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(candidate)
            found.append(candidate)
        else:
            candidate.answer = answer
            candidate.evidence = evidence
            candidate.similarity = round(hits[0].score, 4)
            candidate.last_seen_at = now
            candidate.occurrences += 1
        await session.flush()
        await _mark(
            session,
            tenant_id,
            key,
            sig,
            "conflict",
            now,
            candidate.id,
            item_seq=item.change_seq,
            policy_sig=fingerprint,
        )
        stats["conflict"] += 1
    return found


# ---- 2. 制度里有、知识库里没有 ----


async def _covered(
    ctx: AppContext,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    query: str,
    vector: list[float] | None,
    now: datetime,
) -> bool:
    hits = await search(
        ctx,
        session,
        tenant_id,
        query,
        visibilities=ALL_VISIBILITIES,
        limit=1,
        min_score=COVERED_SCORE,
        vector=vector,
        rerank=False,
        policy=False,
        kinds=(ItemKind.FAQ.value,),
        now=now,
    )
    return bool(hits)


async def _pending_question(session: AsyncSession, question: str) -> bool:
    """审核台已经有同样问题的待处理建议。"""
    target = normalize(question)
    rows = await session.scalars(
        select(KbCandidate.question).where(
            KbCandidate.status == CandidateStatus.PENDING,
            KbCandidate.terms.overlap(terms(question) or [""]),
        )
    )
    return any(normalize(q) == target for q in rows.all())


async def _check_gaps(
    ctx: AppContext,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    policies: list[KbItem],
    budget: _Budget,
    stats: dict[str, Any],
    now: datetime,
) -> list[KbCandidate]:
    by_id = {p.id: p for p in policies}
    rows = (
        await session.execute(
            select(KbChunk.id, KbChunk.item_id, KbChunk.text)
            .where(KbChunk.item_id.in_(list(by_id)), KbChunk.kind == ChunkKind.PASSAGE)
            .order_by(KbChunk.item_id, KbChunk.created_at, KbChunk.id)
        )
    ).all()
    sections = [
        (f"section:{item_id}:{signature(passage)[:16]}", chunk_id, by_id[item_id], passage)
        for chunk_id, item_id, passage in rows
    ]
    marks = await _marks(session, [key for key, *_ in sections])
    todo = []
    for key, chunk_id, policy, passage in sections:
        if key in marks:
            stats["sections_unchanged"] += 1
        else:
            todo.append((key, chunk_id, policy, passage))
    # 只读没核对过的段落的向量。
    vectors: dict[uuid.UUID, list[float] | None] = {}
    if todo:
        vectors = {
            chunk_id: (list(v) if v is not None else None)
            for chunk_id, v in await session.execute(
                select(KbChunk.id, KbChunk.embedding).where(
                    KbChunk.id.in_([chunk_id for _, chunk_id, _, _ in todo])
                )
            )
        }
    found: list[KbCandidate] = []
    for key, chunk_id, policy, passage in todo:
        if await _covered(ctx, session, tenant_id, passage[:300], vectors.get(chunk_id), now):
            await _mark(session, tenant_id, key, "covered", "covered", now)
            stats["sections_covered"] += 1
            continue
        if budget.sections <= 0:
            budget.exhausted = True
            break
        budget.sections -= 1
        stats["sections_checked"] += 1
        section, mapping = pii.mask(passage[:SECTION_CHARS])
        audience = "客户" if policy.visibility == Visibility.PUBLIC else "员工"
        data = await _chat(
            ctx,
            tenant_id,
            prompts.kb_gap_messages(title=policy.title, section=section, audience=audience),
            stats,
        )
        if data is None:
            stats["errors"] += 1
            continue
        raw = data.get("qa_pairs")
        pairs: list[Any] = raw if isinstance(raw, list) else []
        created: uuid.UUID | None = None
        for pair in pairs[:3]:
            if not isinstance(pair, dict):
                continue
            question = pii.unmask(str(pair.get("question") or "").strip(), mapping)[:500]
            answer = pii.unmask(str(pair.get("answer") or "").strip(), mapping)
            if not question or not answer:
                continue
            if await _covered(ctx, session, tenant_id, question, None, now):
                stats["gap_known"] += 1
                continue
            if await _pending_question(session, question):
                continue
            candidate = KbCandidate(
                tenant_id=tenant_id,
                kind=CandidateKind.NEW,
                source=CandidateSource.POLICY,
                question=question,
                answer=answer,
                category=policy.category,
                evidence=[
                    {
                        "kind": "policy",
                        "policy_item_id": str(policy.id),
                        "policy_title": policy.title,
                        "excerpt": passage[:EVIDENCE_CHARS],
                        "reason": f"《{policy.title}》里的规定，知识库里还没有对应的问答",
                        "visibility": policy.visibility,
                        "seen_at": now.isoformat(),
                    }
                ],
                terms=terms(question),
                first_seen_at=now,
                last_seen_at=now,
            )
            session.add(candidate)
            await session.flush()
            created = created or candidate.id
            found.append(candidate)
            stats["gap"] += 1
        await _mark(
            session, tenant_id, key, "drafted", "drafted" if created else "none", now, created
        )
    return found


# ---- 3. 重复的知识 ----


async def _check_duplicates(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    budget: _Budget,
    stats: dict[str, Any],
    now: datetime,
) -> list[KbCandidate]:
    rows = (
        await session.execute(
            select(KbItem.id, KbItem.title, KbItem.change_seq)
            .where(_faq())
            .order_by(KbItem.updated_at.desc(), KbItem.id)
        )
    ).all()
    pairs: dict[tuple[uuid.UUID, uuid.UUID], float] = {}
    # 标准问去掉标点和空格后相同（只比标题，全部比较）。
    seen: dict[str, uuid.UUID] = {}
    for item_id, title, _ in rows:
        normalized = normalize(title)
        twin = seen.get(normalized)
        if twin is not None and normalized:
            a, b = sorted((item_id, twin))
            pairs[(a, b)] = 1.0
        else:
            seen[normalized] = item_id
    # 标准问的向量几乎一样：只查上次找过之后有变化的问答（change_seq 和上次不同）。
    scans = await _marks(session, [f"scan:{item_id}" for item_id, _, _ in rows])
    changed = [
        (item_id, seq)
        for item_id, _, seq in rows
        if (mark := scans.get(f"scan:{item_id}")) is None or mark.item_seq != seq
    ]
    batch = changed[:MAX_DUPLICATES]
    if len(changed) > len(batch):
        budget.exhausted = True
    stats["scanned"] = len(batch)
    vectors = await _vectors(session, [item_id for item_id, _ in batch])
    if any(v is not None for v in vectors.values()):
        await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    published = {item_id for item_id, _, _ in rows}
    for item_id, vector in vectors.items():
        if vector is None:
            continue
        v = cast(literal(vector_literal(vector)), KbChunk.embedding.type)
        distance = KbChunk.embedding.op("<=>", return_type=Float)(v)
        row = (
            await session.execute(
                select(KbChunk.item_id, distance.label("d"))
                .join(
                    KbItem,
                    and_(KbItem.tenant_id == KbChunk.tenant_id, KbItem.id == KbChunk.item_id),
                )
                .where(
                    KbChunk.kind == ChunkKind.QUESTION,
                    KbChunk.embedding.is_not(None),
                    KbChunk.item_id != item_id,
                    _faq(),
                )
                .order_by(distance)
                .limit(1)
            )
        ).first()
        if row is None or row.item_id not in published:
            continue
        similarity = 1.0 - float(row.d)
        if similarity >= DUPLICATE_SIMILARITY:
            a, b = sorted((item_id, row.item_id))
            pairs[(a, b)] = max(pairs.get((a, b), 0.0), round(similarity, 4))
    if batch:
        statement = insert(KbAlignMark).values(
            [
                {
                    "tenant_id": tenant_id,
                    "key": f"scan:{item_id}",
                    "signature": "",
                    "verdict": "scanned",
                    "item_seq": seq,
                    "checked_at": now,
                }
                for item_id, seq in batch
            ]
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["tenant_id", "key"],
                set_={
                    "item_seq": statement.excluded.item_seq,
                    "checked_at": statement.excluded.checked_at,
                },
            )
        )
    marks = await _marks(session, [f"dup:{a}:{b}" for a, b in pairs])
    new_pairs = {
        pair: sim for pair, sim in pairs.items() if f"dup:{pair[0]}:{pair[1]}" not in marks
    }
    if not new_pairs:
        return []
    ids = {i for pair in new_pairs for i in pair}
    by_id = {i.id: i for i in (await session.scalars(select(KbItem).where(KbItem.id.in_(ids))))}
    found: list[KbCandidate] = []
    for (a, b), similarity in new_pairs.items():
        first, second = by_id.get(a), by_id.get(b)
        if first is None or second is None:
            continue
        keep, other = sorted(
            (first, second), key=lambda i: (-i.hits, i.created_at or now, str(i.id))
        )
        candidate = KbCandidate(
            tenant_id=tenant_id,
            kind=CandidateKind.DUPLICATE,
            source=CandidateSource.POLICY,
            question=keep.title,
            answer=None,
            category=keep.category,
            target_item_id=keep.id,
            similarity=similarity,
            evidence=[
                {
                    "kind": "duplicate",
                    "item_id": str(other.id),
                    "title": other.title,
                    "answer": other.content[:EVIDENCE_CHARS],
                    "hits": other.hits,
                    "same_answer": normalize(other.content) == normalize(keep.content),
                    "seen_at": now.isoformat(),
                }
            ],
            terms=terms(keep.title),
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(candidate)
        await session.flush()
        await _mark(session, tenant_id, f"dup:{a}:{b}", "duplicate", "duplicate", now, candidate.id)
        found.append(candidate)
        stats["duplicate"] += 1
    return found


# ---- 4. 整理清单 ----


async def housekeeping(session: AsyncSession, now: datetime) -> dict[str, int]:
    """长期没用到、即将到期、没有负责人、员工评价差的已发布知识数量（§33.7.2 第 4 项）。"""
    published = KbItem.status == ItemStatus.PUBLISHED
    stale = and_(
        published,
        KbItem.published_at <= now - STALE_AFTER,
        or_(KbItem.last_hit_at.is_(None), KbItem.last_hit_at <= now - STALE_AFTER),
    )
    expiring = and_(
        published, KbItem.valid_to.is_not(None), KbItem.valid_to <= now + EXPIRING_WITHIN
    )
    disliked = and_(published, KbItem.dislikes > KbItem.likes, KbItem.dislikes >= 3)
    row = (
        await session.execute(
            select(
                func.count().filter(stale),
                func.count().filter(expiring),
                func.count().filter(and_(published, KbItem.owner_id.is_(None))),
                func.count().filter(disliked),
            ).select_from(KbItem)
        )
    ).one()
    return {"stale": row[0], "expiring": row[1], "no_owner": row[2], "disliked": row[3]}


# ---- 整理 ----


async def run(
    ctx: AppContext,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    now: datetime,
    *,
    settings: WakeSettings,
    force: bool = False,
) -> Report:
    """整理一次（在调用方的事务里，由调用方提交）。返回整理报告和要发的通知。force（立即整理）时
    不看增量更新索引，但没变的知识仍然按核对记录跳过。"""
    stats: dict[str, Any] = dict.fromkeys(
        (
            "items",
            "searched",
            "checked",
            "unchanged",
            "consistent",
            "conflict",
            "unrelated",
            "errors",
            "sections_checked",
            "sections_unchanged",
            "sections_covered",
            "gap",
            "gap_known",
            "scanned",
            "duplicate",
            "llm_calls",
        ),
        0,
    )
    stats["llm_cost"] = 0.0
    policies = await current_policies(session, now)
    stats["policies"] = len(policies)
    fingerprint = policy_fingerprint(policies)
    # 增量更新索引：知识库最近一次变化的编号。和上次整理完时一样、现行制度也没变，就不用再核对。
    seq = changes.latest(await changes.snapshot(session), DOMAINS)
    previous = await session.get(WakeCheckState, (tenant_id, STATE))
    budget = _Budget()
    conflicts: list[KbCandidate] = []
    gaps: list[KbCandidate] = []
    duplicates: list[KbCandidate] = []
    stats["skipped"] = not force and check_state.unchanged(previous, seq, fingerprint, now)
    if not stats["skipped"]:
        if policies:
            conflicts = await _check_conflicts(
                ctx, session, tenant_id, policies, fingerprint, budget, stats, now
            )
            if not budget.exhausted:
                gaps = await _check_gaps(ctx, session, tenant_id, policies, budget, stats, now)
        duplicates = await _check_duplicates(session, tenant_id, budget, stats, now)
        if budget.exhausted or stats["errors"]:
            # 没有核对完：下次不能跳过。
            await session.execute(delete(WakeCheckState).where(WakeCheckState.check_code == STATE))
        else:
            await check_state.save(
                session, tenant_id, STATE, seq=seq, digest=fingerprint, next_due=None, now=now
            )
    stats.update(await housekeeping(session, now))
    stats["pending"] = await pending_count(session)
    stats["continued"] = budget.exhausted
    if budget.exhausted:
        await queue.enqueue(
            session,
            tenant_id,
            RunKind.KB,
            RunTrigger.CONTINUE,
            now=now,
            not_before=now + queue.CONTINUE_DELAY,
        )
    if conflicts and settings.kb_hold_conflicts:
        # 暂停冲突的知识用于回复客户：之前缓存的、依据它们的回答不再使用。
        await answer_cache.clear(session, tenant_id)
    report = Report(stats=stats)
    report.notices = await _notices(session, tenant_id, conflicts, gaps, duplicates)
    return report


async def _notices(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conflicts: list[KbCandidate],
    gaps: list[KbCandidate],
    duplicates: list[KbCandidate],
) -> list[Notice]:
    """有新建议时通知知识管理员；冲突的知识有负责人时另外通知负责人（§33.7.3）。"""
    if not (conflicts or gaps or duplicates):
        return []
    parts = []
    if conflicts:
        parts.append(f"{len(conflicts)} 条和现行制度冲突")
    if gaps:
        parts.append(f"建议新增 {len(gaps)} 条问答")
    if duplicates:
        parts.append(f"{len(duplicates)} 组重复")
    title = "知识库整理：" + "、".join(parts)
    body = "AI 对照现行的规章制度整理了知识库，修改建议在审核台（来源“制度对齐”），确认后才生效。"
    managers = await assign.staff_with(session, Permission.KB_MANAGE)
    notices: list[Notice] = []
    if managers:
        notifications.add(
            session, tenant_id, managers, kind=KIND, title=title, body=body, link=REVIEW_PATH
        )
        notices.append(Notice(managers, title, body, REVIEW_PATH))
    owners: dict[uuid.UUID, int] = {}
    if conflicts:
        targets = [c.target_item_id for c in conflicts if c.target_item_id]
        rows = await session.execute(
            select(KbItem.owner_id)
            .join(Staff, and_(Staff.tenant_id == KbItem.tenant_id, Staff.id == KbItem.owner_id))
            .where(KbItem.id.in_(targets), Staff.status == StaffStatus.ACTIVE)
        )
        for (owner_id,) in rows:
            if owner_id is not None and owner_id not in managers:
                owners[owner_id] = owners.get(owner_id, 0) + 1
    for owner_id, count in owners.items():
        owner_title = f"你负责的 {count} 条知识和现行制度不一致"
        owner_body = "AI 按现行制度起草了修改，请到审核台确认。"
        notifications.add(
            session,
            tenant_id,
            [owner_id],
            kind=KIND,
            title=owner_title,
            body=owner_body,
            link=REVIEW_PATH,
        )
        notices.append(Notice([owner_id], owner_title, owner_body, REVIEW_PATH))
    return notices


async def pending_count(session: AsyncSession) -> int:
    """审核台待处理的"制度对齐"建议。"""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(KbCandidate)
            .where(
                KbCandidate.status == CandidateStatus.PENDING,
                KbCandidate.source == CandidateSource.POLICY,
            )
        )
        or 0
    )


async def latest_report(session: AsyncSession) -> tuple[datetime | None, dict[str, Any]]:
    """最近一次完成的知识库整理（时间、报告）。"""
    row = (
        await session.execute(
            select(WakeRun.finished_at, WakeRun.stats)
            .where(WakeRun.kind == RunKind.KB, WakeRun.status == RunStatus.DONE)
            .order_by(WakeRun.finished_at.desc().nulls_last())
            .limit(1)
        )
    ).first()
    if row is None:
        return None, {}
    return row[0], dict(row[1] or {})
