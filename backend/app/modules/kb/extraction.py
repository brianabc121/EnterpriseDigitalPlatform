"""从已结束的会话提炼知识候选（设计文档 §12.4）。

调度进程每小时处理最近 7 天结束、还没提炼过的会话（`python -m app.cli kb-extract` 可以立即运行）：
1. 筛选：有人工参与、因知识缺失转人工，或客户评价满意（4 分及以上）的会话；
2. 预处理：去掉系统提示和寒暄，合并同一方连续的消息，脱敏（先脱敏再提炼）；
3. 大模型提炼问答对和没有得到解答的问题；只保留可泛化、有把握、不含个人信息的问答；
4. 与已有知识比对：同一问题且答案一致 → 相似问法；答案不一致 → 冲突待审（可能是政策变化）；
   新问题 → 候选；没有解答的问题 → 知识缺口。同类候选按问题相似度聚类并计数。
比对和聚类只在同一租户内进行，知识不会流入其他租户。每条候选记录提炼用的模型和提示词版本。
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
from app.modules.conversation.models import ChatSession, Message, SenderType, SessionStatus
from app.modules.kb import review
from app.modules.kb.models import (
    CandidateKind,
    CandidateStatus,
    ItemStatus,
    KbCandidate,
    KbExtraction,
    KbItem,
    Visibility,
)
from app.modules.kb.search import search
from app.modules.kb.text import normalize, similarity, terms
from app.modules.tenancy.models import Tenant, TenantStatus

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

ROLE: dict[str, str] = {
    SenderType.CUSTOMER: "客户",
    SenderType.AGENT: "坐席",
    SenderType.BOT: "智能客服",
}
CUSTOMER = ROLE[SenderType.CUSTOMER]
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
    if ctx.llm.can_embed:
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
        prompt_version=prompts.EXTRACT_PROMPT_VERSION,
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
    )


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
    async with ctx.db.tenant_session(tenant_id) as session:
        messages = list(
            (
                await session.scalars(
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(Message.sent_at, Message.id)
                )
            ).all()
        )
    lines = transcript(messages)
    # 只有客户说话的会话也提炼：没有得到解答的问题是知识缺口。
    if all(role != CUSTOMER for role, _ in lines):
        await _mark(ctx, tenant_id, session_id, "skipped")
        return "skipped", 0, 0
    try:
        result = await gateway.chat(
            ctx,
            tenant_id,
            prompts.extract_messages(transcript=lines),
            scene="extract",
            json_mode=True,
            max_tokens=1500,
            session_id=session_id,
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
            ):
                recorded_pairs += 1
        for question in gaps:
            asked = [i for i, (role, text) in enumerate(lines, 1) if role == CUSTOMER][-3:]
            evidence = _evidence(session_id, question, lines, asked, now)
            if await record_gap(
                ctx, session, tenant_id, question, evidence, now=now, model=result.model
            ):
                recorded_gaps += 1
        await session.commit()
    await _mark(ctx, tenant_id, session_id, "done", pairs=recorded_pairs, gaps=recorded_gaps)
    return "done", recorded_pairs, recorded_gaps


async def run_extraction(
    ctx: AppContext,
    *,
    now: datetime | None = None,
    tenant_code: str | None = None,
    limit: int = BATCH,
) -> ExtractionReport:
    """调度任务：逐个租户提炼最近结束的会话。没有配置大模型或租户关闭了自动提炼时跳过。"""
    report = ExtractionReport()
    if not ctx.llm.enabled:
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
            if not settings.extraction_enabled:
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
    return report
