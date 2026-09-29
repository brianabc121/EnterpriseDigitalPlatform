"""AI 回复流水线（设计文档 §11.1）：前置规则 → 知识检索 → 大模型生成 → 后置护栏 → 转人工决策。

线上接待（responder.py）、管理后台的"试一试"和评测共用这一套逻辑。流水线本身不写库、不发消息，
只返回判定结果；调用大模型的记账由 gateway 完成。
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.context import AppContext
from app.integrations.llm import LLMUnavailable
from app.modules.ai import decision, gateway, pii, prompts, reasons
from app.modules.ai.models import AiSettings
from app.modules.ai.prompts import Passage, Turn
from app.modules.kb.search import search
from app.modules.platform.content import ai_words

logger = logging.getLogger(__name__)

SAFE_FALLBACK = (
    "抱歉，我没有找到准确的答案。您可以换个说法描述问题，或者回复「转人工」联系人工客服。"
)
KNOWLEDGE_LIMIT = 4
HISTORY_LIMIT = 10


@dataclass
class Context:
    """一次判定需要的会话上下文。"""

    question: str
    history: list[Turn] = field(default_factory=list)
    customer_tags: list[str] = field(default_factory=list)
    previous_question: str | None = None
    repeats: int = 0
    turns: int = 0
    guard_failures: int = 0


@dataclass
class Outcome:
    action: str  # reply、handoff
    reason: str | None = None
    reply: str | None = None
    score: float = 0.0
    signals: dict[str, Any] = field(default_factory=dict)
    guard: str | None = None
    knowledge: list[dict[str, Any]] = field(default_factory=list)
    repeats: int = 0
    guard_failures: int = 0
    # 模型判断的客户意图（策略配置了按意图分配时）。
    intent: str | None = None

    @property
    def used_items(self) -> list[uuid.UUID]:
        return [uuid.UUID(k["item_id"]) for k in self.knowledge if k.get("used")]


def parse_reply(content: str) -> dict[str, Any] | None:
    """解析模型输出的 JSON（容忍代码块和前后的说明文字）。"""
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("reply"), str):
        return None
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "reply": data["reply"].strip(),
        "confidence": min(1.0, max(0.0, confidence)),
        "handoff": bool(data.get("handoff")),
        "reason": str(data.get("reason") or "")[:200],
        "intent": str(data.get("intent") or "").strip()[:32] or None,
    }


async def evaluate(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    settings: AiSettings,
    context: Context,
    *,
    company: str,
    session_id: uuid.UUID | None = None,
    scene: str = "reply",
    intents: list[str] | None = None,
) -> Outcome:
    question = context.question
    async with ctx.db.tenant_session(tenant_id) as db:
        platform_words = await ai_words(db)
    sensitive = [*settings.sensitive_keywords, *platform_words]
    trigger = decision.hard_trigger(
        question,
        extra_handoff=list(settings.handoff_keywords),
        extra_sensitive=sensitive,
        customer_tags=context.customer_tags,
    )
    if trigger:
        return Outcome(action="handoff", reason=trigger, repeats=context.repeats)

    async with ctx.db.tenant_session(tenant_id) as db:
        hits = await search(
            ctx, db, tenant_id, question, visibilities=("public",), limit=KNOWLEDGE_LIMIT
        )
    passages = [
        Passage(item_id=str(h.item_id), kind=h.kind, title=h.title, text=h.text, score=h.score)
        for h in hits
    ]
    best = max((h.score for h in hits), default=0.0)
    knowledge = [
        {"item_id": str(h.item_id), "title": h.title, "score": round(h.score, 4), "used": True}
        for h in hits
    ]

    masked_question, mapping = pii.mask(question)
    history = [Turn(t.role, pii.mask(t.text, mapping)[0]) for t in context.history]
    messages = prompts.reply_messages(
        company=company,
        bot_name=settings.bot_name,
        persona=settings.persona,
        passages=passages,
        history=history[-HISTORY_LIMIT:],
        question=masked_question,
        intents=intents,
    )
    try:
        result = await gateway.chat(
            ctx, tenant_id, messages, scene=scene, json_mode=True, session_id=session_id
        )
    except LLMUnavailable as exc:
        logger.warning("AI reply unavailable for tenant %s: %s", tenant_id, exc)
        return Outcome(action="handoff", reason="ai_unavailable", knowledge=knowledge)

    parsed = parse_reply(result.content)
    violation: str | None
    if parsed is None:
        violation = "bad_output"
    else:
        parsed["reply"] = pii.unmask(parsed["reply"], mapping)
        violation = decision.guard(
            parsed["reply"],
            references=prompts.references(passages),
            sensitive=[*decision.SENSITIVE, *sensitive],
        )
    if violation or parsed is None:
        failures = context.guard_failures + 1
        if failures >= decision.GUARD_FAILURES_TO_HANDOFF:
            return Outcome(
                action="handoff",
                reason="guardrail",
                guard=violation,
                knowledge=knowledge,
                guard_failures=failures,
            )
        return Outcome(
            action="reply",
            reason="guardrail_retry",
            reply=SAFE_FALLBACK,
            guard=violation,
            knowledge=[{**k, "used": False} for k in knowledge],
            repeats=context.repeats,
            guard_failures=failures,
        )

    intent = parsed["intent"] if intents and parsed["intent"] in intents else None
    if parsed["handoff"]:
        return Outcome(
            action="handoff",
            reason="model_request",
            reply=parsed["reply"] or None,
            signals={"model_reason": parsed["reason"]},
            knowledge=knowledge,
            intent=intent,
        )

    found, repeats = decision.signals(
        question,
        best_relevance=best,
        relevance_threshold=settings.relevance_threshold,
        confidence=parsed["confidence"],
        previous_question=context.previous_question,
        repeats=context.repeats,
        turns=context.turns + 1,
        max_turns=settings.max_turns,
    )
    signal_values = {**found.active(), **found.details}
    if found.score >= settings.handoff_threshold:
        return Outcome(
            action="handoff",
            reason="score",
            score=found.score,
            signals=signal_values,
            knowledge=knowledge,
            repeats=repeats,
            intent=intent,
        )
    return Outcome(
        action="reply",
        reply=parsed["reply"],
        score=found.score,
        signals=signal_values,
        knowledge=knowledge,
        repeats=repeats,
        intent=intent,
    )


async def summarize(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    history: list[Turn],
    reason: str,
    *,
    session_id: uuid.UUID | None = None,
) -> str:
    """转人工时给坐席的交接摘要（原因另存在会话上）；大模型不可用时用最近的客户消息拼一段。"""
    label = reasons.label(reason)
    customer = [t.text for t in history if t.role == "customer"]
    fallback = f"客户最近的问题：{'；'.join(customer[-3:])[:200]}"
    if not history or not await ctx.llms.chat_enabled(tenant_id, "summary"):
        return fallback
    mapping: dict[str, str] = {}
    masked = [Turn(t.role, pii.mask(t.text, mapping)[0]) for t in history[-HISTORY_LIMIT:]]
    try:
        result = await gateway.chat(
            ctx,
            tenant_id,
            prompts.summary_messages(history=masked, reason=label),
            scene="summary",
            fast=True,
            max_tokens=300,
            session_id=session_id,
        )
    except LLMUnavailable:
        return fallback
    text = pii.unmask(result.content.strip(), mapping)
    return text[:500] or fallback
