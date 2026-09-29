"""从已结束的会话提炼知识候选（设计文档 §12.4）。

调度进程每小时处理最近 7 天结束、还没提炼过的会话（`python -m app.cli kb-extract` 可以立即运行）：
1. 筛选：有人工参与、因知识缺失转人工，或客户评价满意（4 分及以上）的会话；
2. 预处理：去掉系统提示和寒暄，合并同一方连续的消息，脱敏（先脱敏再提炼）；
3. 大模型提炼问答对和没有得到解答的问题；只保留可泛化、有把握、不含个人信息的问答；
4. 与已有知识比对：同一问题且答案一致 → 相似问法；答案不一致 → 冲突待审（可能是政策变化）；
   新问题 → 候选；没有解答的问题 → 知识缺口。同类候选按问题相似度聚类并计数。
比对和聚类只在同一租户内进行，知识不会流入其他租户。每条候选记录提炼用的模型和提示词版本。

客户评价满意的人工会话还会挖掘坐席的优秀回复（话术候选），审核通过后成为共享快捷话术；
客户在提炼之后才评价的会话，下次运行时补充挖掘。
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway, pii, prompts
from app.modules.ai import service as ai_service
from app.modules.billing.entitlements import has_feature
from app.modules.conversation.models import ChatSession, Message, SenderType, SessionStatus
from app.modules.kb import review
from app.modules.kb.models import (
    CandidateKind,
    CandidateSource,
    CandidateStatus,
    ItemStatus,
    KbCandidate,
    KbExtraction,
    KbItem,
    Visibility,
)
from app.modules.kb.search import search
from app.modules.kb.text import normalize, similarity, terms
from app.modules.quickreply.models import QuickReply
from app.modules.tenancy.models import Tenant, TenantStatus
from app.modules.wecom.models import WecomSidebarMessage

logger = logging.getLogger(__name__)

LOOKBACK = timedelta(days=7)
BATCH = 50
MAX_ATTEMPTS = 3
MIN_CONFIDENCE = 0.6
# 检索相关度达到这个值视为"已有知识里的同一个问题"。
SAME_QUESTION = 0.8
# 候选之间问题的相似度（词项 Jaccard）达到这个值归为同一类。
CLUSTER_SIMILARITY = 0.6
MAX_EVIDENCE = 10
AUTO_MERGE_EVIDENCE = 3
AUTO_MERGE_SIMILARITY = 0.85
SATISFIED = 4
GAP_REASONS = ("model_request", "score")
PHRASE_MIN = 10
PHRASE_MAX = 500
MAX_PHRASES = 5
# 与已有的共享话术相似度达到这个值时不再作为候选。
SAME_PHRASE = 0.8

ROLE: dict[str, str] = {
    SenderType.CUSTOMER: "客户",
    SenderType.AGENT: "坐席",
    SenderType.BOT: "智能客服",
}
CUSTOMER = ROLE[SenderType.CUSTOMER]
AGENT = ROLE[SenderType.AGENT]
_GREETING = re.compile(
    r"^(你好|您好|hi|hello|在吗|在不在|有人吗|谢谢|多谢|谢谢你|好的|好|嗯|嗯嗯|哦|ok|收到|明白了|"
    r"知道了|再见|拜拜)[\s!！。.~～？?]*$",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"\[(手机号|邮箱|身份证号|银行卡号)\d+\]")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_NEGATIONS = ("不支持", "不能", "无法", "不可以", "不提供", "不包邮", "不退")


@dataclass
class ExtractionReport:
    tenants: int = 0
    sessions: int = 0
    pairs: int = 0
    gaps: int = 0
    skipped: int = 0
    failed: int = 0
    phrases: int = 0


@dataclass
class Pair:
    question: str
    answer: str
    category: str = ""
    time_sensitive: bool = False
    confidence: float = 0.0
    evidence: list[int] = field(default_factory=list)


# ---- 预处理与解析 ----


def transcript(messages: list[Message]) -> list[tuple[str, str]]:
    """（角色, 脱敏后的内容）：去掉系统提示、寒暄和空消息，同一方连续的消息合并为一行。"""
    mapping: dict[str, str] = {}
    lines: list[tuple[str, str]] = []
    for m in messages:
        role = ROLE.get(m.sender_type)
        text = (m.text_plain or "").strip()
        if role is None or not text or _GREETING.match(text):
            continue
        masked, mapping = pii.mask(text, mapping)
        if lines and lines[-1][0] == role:
            lines[-1] = (role, f"{lines[-1][1]}\n{masked}")
        else:
            lines.append((role, masked))
    return lines


def _clean(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def parse(content: str, lines: int) -> tuple[list[Pair], list[str]] | None:
    """解析模型输出；只保留可泛化、有把握、不含个人信息占位符的问答。无法解析时返回 None。"""
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    pairs: list[Pair] = []
    for raw in data.get("qa_pairs") or []:
        if not isinstance(raw, dict) or raw.get("generalizable") is False:
            continue
        question, answer = _clean(raw.get("question"), 500), _clean(raw.get("answer"), 2000)
        try:
            confidence = float(raw.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        if not question or not answer or confidence < MIN_CONFIDENCE:
            continue
        if _PLACEHOLDER.search(question) or _PLACEHOLDER.search(answer):
            continue
        evidence = [
            int(i) for i in raw.get("evidence") or [] if isinstance(i, int) and 1 <= i <= lines
        ]
        pairs.append(
            Pair(
                question=question,
                answer=answer,
                category=_clean(raw.get("category"), 64),
                time_sensitive=bool(raw.get("time_sensitive")),
                confidence=round(min(1.0, confidence), 4),
                evidence=evidence,
            )
        )
    gaps = [
        q
        for q in (_clean(g, 500) for g in data.get("unresolved_questions") or [])
        if q and not _PLACEHOLDER.search(q)
    ]
    return pairs, gaps


def parse_phrases(content: str) -> list[tuple[str, str]]:
    """解析优秀话术：（标题, 话术），去掉过短、过长和含个人信息占位符的。"""
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    found: list[tuple[str, str]] = []
    for raw in data.get("phrases") or []:
        if not isinstance(raw, dict):
            continue
        text = _clean(raw.get("content"), PHRASE_MAX + 1)
        if not PHRASE_MIN <= len(text) <= PHRASE_MAX or _PLACEHOLDER.search(text):
            continue
        found.append((_clean(raw.get("title"), 32) or text[:12], text))
    return found[:MAX_PHRASES]


def consistent(new: str, old: str) -> bool:
    """同一问题的两个答案是否一致：新答案没有出现旧答案里没有的数字，也没有相反的否定说法。"""
    if set(_NUMBER.findall(new)) - set(_NUMBER.findall(old)):
        return False
    return all((word in new) == (word in old) for word in _NEGATIONS)


def known_question(item: KbItem, question: str) -> bool:
    target = normalize(question)
    return any(normalize(q) == target for q in [item.title, *item.questions])


# ---- 记录候选 ----


async def _cluster(
    session: AsyncSession,
    kind: str,
    question: str,
    target_item_id: uuid.UUID | None,
) -> KbCandidate | None:
    """同类（同种类、同目标知识）待审候选里问题最相似的一条。"""
    query_terms = terms(question)
    if not query_terms:
        return None
    rows = (
        await session.scalars(
            select(KbCandidate)
            .where(
                KbCandidate.status == CandidateStatus.PENDING,
                KbCandidate.kind == kind,
                KbCandidate.target_item_id.is_(None)
                if target_item_id is None
                else KbCandidate.target_item_id == target_item_id,
                KbCandidate.terms.overlap(query_terms),
            )
            .limit(50)
            .with_for_update()
        )
    ).all()
    scored = [(similarity(question, c.question), c) for c in rows]
    best = max(scored, key=lambda pair: pair[0], default=None)
    return best[1] if best and best[0] >= CLUSTER_SIMILARITY else None


def _evidence(
    session_id: uuid.UUID,
    question: str,
    lines: list[tuple[str, str]],
    indices: list[int],
    now: datetime,
) -> dict[str, Any]:
    picked = [lines[i - 1] for i in sorted(set(indices))] if indices else lines[:4]
    return {
        "session_id": str(session_id),
        "seen_at": now.isoformat(),
        "question": question,
        "lines": [{"role": role, "text": text[:500]} for role, text in picked],
    }


async def _upsert(
    ctx: AppContext,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    kind: str,
    question: str,
    answer: str | None,
    evidence: dict[str, Any],
    now: datetime,
    model: str,
    target: KbItem | None = None,
    score: float | None = None,
    pair: Pair | None = None,
    source: str = CandidateSource.SESSION,
    prompt_version: str | None = None,
) -> KbCandidate:
    """聚类到已有的待审候选（出现次数加一、补充证据），或新建候选。"""
    target_id = target.id if target else None
    existing = await _cluster(session, kind, question, target_id)
    if existing is not None:
        existing.occurrences += 1
        existing.last_seen_at = now
        existing.evidence = [*existing.evidence, evidence][-MAX_EVIDENCE:]
        if pair and answer and pair.confidence > (existing.confidence or 0):
            existing.answer, existing.confidence = answer, pair.confidence
            existing.category = pair.category or existing.category
        return existing
    embedding = None
    if await ctx.llms.embed_enabled():
        try:
            [embedding] = await gateway.embed(ctx, tenant_id, [question], scene="extract")
        except LLMUnavailable as exc:
            logger.warning("embedding failed for candidate: %s", exc)
    candidate = KbCandidate(
        tenant_id=tenant_id,
        kind=kind,
        question=question,
        answer=answer,
        category=pair.category if pair else "",
        target_item_id=target_id,
        similarity=round(score, 4) if score is not None else None,
        confidence=pair.confidence if pair else None,
        time_sensitive=pair.time_sensitive if pair else False,
        terms=terms(question),
        embedding=embedding,
        evidence=[evidence],
        first_seen_at=now,
        last_seen_at=now,
        model=model,
        prompt_version=prompt_version,
        source=source,
    )
    session.add(candidate)
    await session.flush()
    return candidate


async def _best_existing(
    ctx: AppContext, session: AsyncSession, tenant_id: uuid.UUID, question: str
) -> tuple[KbItem, float] | None:
    hits = await search(
        ctx,
        session,
        tenant_id,
        question,
        visibilities=(Visibility.PUBLIC, Visibility.AGENT, Visibility.ADMIN),
        limit=1,
    )
    if not hits or hits[0].score < SAME_QUESTION:
        return None
    item = await session.get(KbItem, hits[0].item_id)
    if item is None or item.status != ItemStatus.PUBLISHED:
        return None
    return item, hits[0].score


async def record_pair(
    ctx: AppContext,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    pair: Pair,
    evidence: dict[str, Any],
    *,
    now: datetime,
    model: str,
    auto_merge: bool,
    source: str = CandidateSource.SESSION,
    prompt_version: str | None = None,
) -> KbCandidate | None:
    """一个问答：已有知识里的同一问题 → 相似问法或冲突；否则 → 新问题。问法已存在时不记录。"""
    existing = await _best_existing(ctx, session, tenant_id, pair.question)
    if existing is None:
        return await _upsert(
            ctx,
            session,
            tenant_id,
            kind=CandidateKind.NEW,
            question=pair.question,
            answer=pair.answer,
            evidence=evidence,
            now=now,
            model=model,
            pair=pair,
            source=source,
            prompt_version=prompt_version,
        )
    item, score = existing
    if consistent(pair.answer, item.content):
        if known_question(item, pair.question):
            return None
        candidate = await _upsert(
            ctx,
            session,
            tenant_id,
            kind=CandidateKind.SIMILAR,
            question=pair.question,
            answer=pair.answer,
            evidence=evidence,
            now=now,
            model=model,
            target=item,
            score=score,
            pair=pair,
            source=source,
            prompt_version=prompt_version,
        )
        if (
            auto_merge
            and candidate.occurrences >= AUTO_MERGE_EVIDENCE
            and (candidate.similarity or 0) >= AUTO_MERGE_SIMILARITY
        ):
            await review.auto_merge(ctx, session, candidate, item, now=now)
        return candidate
    return await _upsert(
        ctx,
        session,
        tenant_id,
        kind=CandidateKind.CONFLICT,
        question=pair.question,
        answer=pair.answer,
        evidence=evidence,
        now=now,
        model=model,
        target=item,
        score=score,
        pair=pair,
        source=source,
        prompt_version=prompt_version,
    )


async def record_gap(
    ctx: AppContext,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    question: str,
    evidence: dict[str, Any],
    *,
    now: datetime,
    model: str,
    prompt_version: str | None = None,
) -> KbCandidate | None:
    """没有得到解答的问题。已有知识能回答时不算缺口。"""
    if await _best_existing(ctx, session, tenant_id, question) is not None:
        return None
    return await _upsert(
        ctx,
        session,
        tenant_id,
        kind=CandidateKind.GAP,
        question=question,
        answer=None,
        evidence=evidence,
        now=now,
        model=model,
        prompt_version=prompt_version,
    )


async def record_phrase(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    title: str,
    content: str,
    evidence: dict[str, Any],
    *,
    now: datetime,
    model: str,
    prompt_version: str | None = None,
) -> KbCandidate | None:
    """优秀话术候选：已经是共享话术的不记录；与待审的话术相似时归为一类（出现次数加一）。"""
    shared = (
        await session.scalars(select(QuickReply.content).where(QuickReply.owner_id.is_(None)))
    ).all()
    if any(similarity(content, s) >= SAME_PHRASE for s in shared):
        return None
    content_terms = terms(content)
    rows = (
        await session.scalars(
            select(KbCandidate)
            .where(
                KbCandidate.status == CandidateStatus.PENDING,
                KbCandidate.kind == CandidateKind.PHRASE,
                KbCandidate.terms.overlap(content_terms),
            )
            .limit(50)
            .with_for_update()
        )
    ).all()
    best = max(
        ((similarity(content, c.answer or ""), c) for c in rows),
        key=lambda pair: pair[0],
        default=None,
    )
    if best is not None and best[0] >= CLUSTER_SIMILARITY:
        existing = best[1]
        existing.occurrences += 1
        existing.last_seen_at = now
        existing.evidence = [*existing.evidence, evidence][-MAX_EVIDENCE:]
        return existing
    candidate = KbCandidate(
        tenant_id=tenant_id,
        kind=CandidateKind.PHRASE,
        question=title,
        answer=content,
        category="优秀话术",
        terms=content_terms,
        evidence=[evidence],
        first_seen_at=now,
        last_seen_at=now,
        model=model,
        prompt_version=prompt_version,
        source=CandidateSource.SESSION,
    )
    session.add(candidate)
    await session.flush()
    return candidate


async def mine_phrases(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    lines: list[tuple[str, str]],
    *,
    now: datetime,
) -> int | None:
    """从客户评价满意的会话里挑选坐席的优秀回复，返回记录的候选数；模型不可用时返回 None
    （下次再试）。处理过的会话记下时间，不再重复挖掘。"""
    recorded = 0
    if any(role == AGENT for role, _ in lines):
        prompt = await ctx.prompts.get("phrase")
        try:
            result = await gateway.chat(
                ctx,
                tenant_id,
                prompts.phrase_messages(transcript=lines, template=prompt.content),
                scene="phrase",
                json_mode=True,
                max_tokens=800,
                session_id=session_id,
                prompt_version=prompt.version,
            )
        except LLMUnavailable as exc:
            logger.warning("phrase mining failed for session %s: %s", session_id, exc)
            return None
        async with ctx.db.tenant_session(tenant_id) as session:
            for title, content in parse_phrases(result.content):
                said = [
                    i
                    for i, (role, text) in enumerate(lines, 1)
                    if role == AGENT and (content in text or similarity(content, text) >= 0.5)
                ]
                evidence = _evidence(session_id, title, lines, said[:3], now)
                if await record_phrase(
                    session,
                    tenant_id,
                    title,
                    content,
                    evidence,
                    now=now,
                    model=result.model,
                    prompt_version=prompt.version,
                ):
                    recorded += 1
            await session.commit()
    async with ctx.db.tenant_session(tenant_id) as session:
        row = await session.get(KbExtraction, session_id)
        if row is not None:
            row.phrases_at = now
        await session.commit()
    return recorded


async def _messages(ctx: AppContext, tenant_id: uuid.UUID, session_id: uuid.UUID) -> list[Message]:
    async with ctx.db.tenant_session(tenant_id) as session:
        return list(
            (
                await session.scalars(
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(Message.sent_at, Message.id)
                )
            ).all()
        )


async def mine_late_phrases(
    ctx: AppContext, tenant_id: uuid.UUID, *, now: datetime, limit: int = BATCH
) -> int:
    """提炼之后客户才评价满意的人工会话：补充挖掘优秀话术。"""
    async with ctx.db.tenant_session(tenant_id) as session:
        session_ids = list(
            (
                await session.scalars(
                    select(ChatSession.id)
                    .join(KbExtraction, KbExtraction.session_id == ChatSession.id)
                    .where(
                        ChatSession.status == SessionStatus.CLOSED,
                        ChatSession.closed_at >= now - LOOKBACK,
                        ChatSession.csat >= SATISFIED,
                        ChatSession.assigned_at.is_not(None),
                        KbExtraction.status.in_(("done", "skipped")),
                        KbExtraction.phrases_at.is_(None),
                    )
                    .order_by(ChatSession.closed_at)
                    .limit(limit)
                )
            ).all()
        )
    recorded = 0
    for session_id in session_ids:
        lines = transcript(await _messages(ctx, tenant_id, session_id))
        recorded += await mine_phrases(ctx, tenant_id, session_id, lines, now=now) or 0
    return recorded


# ---- 运行 ----


def eligible(now: datetime, limit: int) -> Any:
    """最近结束、还没提炼过（或失败次数未满）且值得提炼的会话。"""
    finished = select(KbExtraction.session_id).where(
        or_(KbExtraction.status != "failed", KbExtraction.attempts >= MAX_ATTEMPTS)
    )
    return (
        select(ChatSession.id)
        .where(
            ChatSession.status == SessionStatus.CLOSED,
            ChatSession.closed_at >= now - LOOKBACK,
            ChatSession.id.not_in(finished),
            or_(
                ChatSession.assigned_at.is_not(None),
                ChatSession.csat >= SATISFIED,
                ChatSession.handoff_reason.in_(GAP_REASONS),
            ),
        )
        .order_by(ChatSession.closed_at, ChatSession.id)
        .limit(limit)
    )


async def _mark(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    status: str,
    *,
    pairs: int = 0,
    gaps: int = 0,
    error: str | None = None,
) -> None:
    async with ctx.db.tenant_session(tenant_id) as session:
        row = await session.get(KbExtraction, session_id)
        if row is None:
            session.add(
                KbExtraction(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    status=status,
                    pairs=pairs,
                    gaps=gaps,
                    error=error,
                )
            )
        else:
            row.status, row.pairs, row.gaps, row.error = status, pairs, gaps, error
            row.attempts += 1
        await session.commit()


async def extract_session(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    auto_merge: bool,
    now: datetime,
) -> tuple[str, int, int]:
    """提炼一个会话，返回（状态, 问答数, 缺口数）。调用模型时不占数据库事务。"""
    lines = transcript(await _messages(ctx, tenant_id, session_id))
    # 只有客户说话的会话也提炼：没有得到解答的问题是知识缺口。
    if all(role != CUSTOMER for role, _ in lines):
        await _mark(ctx, tenant_id, session_id, "skipped")
        return "skipped", 0, 0
    prompt = await ctx.prompts.get("extract")
    try:
        result = await gateway.chat(
            ctx,
            tenant_id,
            prompts.extract_messages(transcript=lines, template=prompt.content),
            scene="extract",
            json_mode=True,
            max_tokens=1500,
            session_id=session_id,
            prompt_version=prompt.version,
        )
    except LLMUnavailable as exc:
        await _mark(ctx, tenant_id, session_id, "failed", error=str(exc)[:500])
        return "failed", 0, 0
    parsed = parse(result.content, len(lines))
    if parsed is None:
        await _mark(ctx, tenant_id, session_id, "failed", error="模型输出无法解析")
        return "failed", 0, 0
    pairs, gaps = parsed
    recorded_pairs = recorded_gaps = 0
    async with ctx.db.tenant_session(tenant_id) as session:
        for pair in pairs:
            evidence = _evidence(session_id, pair.question, lines, pair.evidence, now)
            if await record_pair(
                ctx,
                session,
                tenant_id,
                pair,
                evidence,
                now=now,
                model=result.model,
                auto_merge=auto_merge,
                prompt_version=prompt.version,
            ):
                recorded_pairs += 1
        for question in gaps:
            asked = [i for i, (role, text) in enumerate(lines, 1) if role == CUSTOMER][-3:]
            evidence = _evidence(session_id, question, lines, asked, now)
            if await record_gap(
                ctx,
                session,
                tenant_id,
                question,
                evidence,
                now=now,
                model=result.model,
                prompt_version=prompt.version,
            ):
                recorded_gaps += 1
        await session.commit()
    await _mark(ctx, tenant_id, session_id, "done", pairs=recorded_pairs, gaps=recorded_gaps)
    return "done", recorded_pairs, recorded_gaps


async def extract_sidebar(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    *,
    auto_merge: bool,
    now: datetime,
    limit: int = BATCH,
) -> int:
    """企业微信侧边栏里的一问一答（员工粘贴的客户问题 + 发出的回复）也进入沉淀流水线
    （设计 §12.3）。与会话一样先脱敏再提炼，返回记录的候选数。"""
    async with ctx.db.tenant_session(tenant_id) as session:
        rows = (
            await session.scalars(
                select(WecomSidebarMessage)
                .where(
                    WecomSidebarMessage.question.is_not(None),
                    WecomSidebarMessage.extracted_at.is_(None),
                    WecomSidebarMessage.created_at >= now - LOOKBACK,
                )
                .order_by(WecomSidebarMessage.created_at)
                .limit(limit)
            )
        ).all()
        items = [(r.id, r.question or "", r.content) for r in rows]
    recorded = 0
    prompt = await ctx.prompts.get("extract")
    for message_id, question, answer in items:
        mapping: dict[str, str] = {}
        masked_question, mapping = pii.mask(question.strip(), mapping)
        masked_answer, mapping = pii.mask(answer.strip(), mapping)
        lines = [(CUSTOMER, masked_question), (ROLE[SenderType.AGENT], masked_answer)]
        parsed = None
        model = ""
        if masked_question and masked_answer and not _GREETING.match(masked_question):
            try:
                result = await gateway.chat(
                    ctx,
                    tenant_id,
                    prompts.extract_messages(transcript=lines, template=prompt.content),
                    scene="extract",
                    json_mode=True,
                    max_tokens=800,
                    prompt_version=prompt.version,
                )
            except LLMUnavailable as exc:
                logger.warning("sidebar extraction failed: %s", exc)
                continue
            parsed = parse(result.content, len(lines))
            model = result.model
        async with ctx.db.tenant_session(tenant_id) as session:
            for pair in (parsed or ([], []))[0]:
                evidence = {
                    "session_id": None,
                    "sidebar_message_id": str(message_id),
                    "seen_at": now.isoformat(),
                    "question": pair.question,
                    "lines": [{"role": role, "text": text[:500]} for role, text in lines],
                }
                if await record_pair(
                    ctx,
                    session,
                    tenant_id,
                    pair,
                    evidence,
                    now=now,
                    model=model,
                    auto_merge=auto_merge,
                    source=CandidateSource.SIDEBAR,
                    prompt_version=prompt.version,
                ):
                    recorded += 1
            row = await session.get(WecomSidebarMessage, message_id)
            if row is not None:
                row.extracted_at = now
            await session.commit()
    return recorded


async def run_extraction(
    ctx: AppContext,
    *,
    now: datetime | None = None,
    tenant_code: str | None = None,
    limit: int = BATCH,
) -> ExtractionReport:
    """调度任务：逐个租户提炼最近结束的会话。没有配置大模型或租户关闭了自动提炼时跳过。"""
    report = ExtractionReport()
    if not await ctx.llms.any_enabled():
        return report
    now = now or datetime.now(UTC)
    async with ctx.db.platform_sessionmaker() as session:
        query = select(Tenant.id).where(Tenant.status == TenantStatus.ACTIVE)
        if tenant_code:
            query = query.where(Tenant.code == tenant_code)
        tenant_ids = list((await session.scalars(query.order_by(Tenant.created_at))).all())
    for tenant_id in tenant_ids:
        async with ctx.db.tenant_session(tenant_id) as session:
            settings = await ai_service.load(session, tenant_id)
            if not settings.extraction_enabled or not await has_feature(
                session, tenant_id, "extraction"
            ):
                continue
            if not await ctx.llms.chat_enabled(tenant_id, "extract"):
                continue
            session_ids = list((await session.scalars(eligible(now, limit))).all())
        report.tenants += 1
        for session_id in session_ids:
            try:
                status, pairs, gaps = await extract_session(
                    ctx, tenant_id, session_id, auto_merge=settings.auto_merge_similar, now=now
                )
            except Exception:
                logger.exception("knowledge extraction failed for session %s", session_id)
                await _mark(ctx, tenant_id, session_id, "failed", error="处理出错")
                status, pairs, gaps = "failed", 0, 0
            report.sessions += 1
            report.pairs += pairs
            report.gaps += gaps
            report.skipped += status == "skipped"
            report.failed += status == "failed"
        if await ctx.llms.chat_enabled(tenant_id, "phrase"):
            try:
                report.phrases += await mine_late_phrases(ctx, tenant_id, now=now, limit=limit)
            except Exception:
                logger.exception("phrase mining failed for tenant %s", tenant_id)
        try:
            report.pairs += await extract_sidebar(
                ctx, tenant_id, auto_merge=settings.auto_merge_similar, now=now, limit=limit
            )
        except Exception:
            logger.exception("sidebar knowledge extraction failed for tenant %s", tenant_id)
    return report
