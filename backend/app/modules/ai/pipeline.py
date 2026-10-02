"""AI 回复流水线（设计文档 §11.1）：前置规则 → 问题改写 → 知识检索（混合检索、重排）→ 语义缓存 →
大模型生成（可调用工具）→ 后置护栏 → 转人工决策。

租户开通了订单功能时（设计文档 §25.2）：前置规则之后识别套价（固定话术答复，不调用大模型），
检索时一并查商品（只有对客可见的字段，作为【商品信息】），后置护栏之后检查回复里有没有内部价格
口径或成本价金额（拦截并改用固定话术）；连续两次找不到客户要的商品时转人工。

线上接待（responder.py）、管理后台的"试一试"和评测共用这一套逻辑。流水线不发消息，只返回判定
结果；调用大模型的记账由 gateway 完成。工具里只有登记线索和留言会写库（试一试时不写）。

有意图判断（设计文档 §32.6）时：判断模型认为客户在要求人工时转人工；设置了"高意向客户转人工"且到了
那个阶段时转人工（排队中不转）；系统提示里加上【客户意图判断】；"生气激动"计入负面情绪的软信号。
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.context import AppContext
from app.integrations.llm import ChatResult, LLMUnavailable
from app.modules.ai import answer_cache, decision, gateway, pii, price_guard, prompts, reasons
from app.modules.ai.intent import HUMAN_THRESHOLD, NEGATIVE_EMOTION, Judgment, prompt_block
from app.modules.ai.models import AiSettings
from app.modules.ai.prompts import Passage, Turn
from app.modules.ai.tools import ToolBox, specs
from app.modules.kb.search import Hit, search
from app.modules.orders import ai as order_ai
from app.modules.platform.content import ai_words
from app.modules.todos import ai as todo_ai

logger = logging.getLogger(__name__)

SAFE_FALLBACK = (
    "抱歉，我没有找到准确的答案。您可以换个说法描述问题，或者回复「转人工」联系人工客服。"
)
KNOWLEDGE_LIMIT = 4
HISTORY_LIMIT = 10
REWRITE_HISTORY = 6
MAX_QUERIES = 3
MAX_TOOL_ROUNDS = 3
# 把握不低于这个值、依据了知识的第一轮回答才进缓存。
CACHE_MIN_CONFIDENCE = 0.7
_SEPARATORS = re.compile(r"[？?；;\n]")


@dataclass(frozen=True)
class ChannelAi:
    """渠道对 AI 参数的覆盖（设计文档 §11.2：阈值可以按租户和渠道分别配置）与知识空间范围。"""

    handoff_threshold: float | None = None
    relevance_threshold: float | None = None
    max_turns: int | None = None
    space_ids: list[uuid.UUID] = field(default_factory=list)


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
    # 这一轮客户消息的 ID（AI 登记待办、提交订单时作为依据）。
    message_ids: list[uuid.UUID] = field(default_factory=list)
    # 之前连续几次没找到客户要的商品。
    product_misses: int = 0
    # 意图判断（设计文档 §32.6）；为空表示没有判断或不参考。
    intent: Judgment | None = None
    # 排队中（策略允许排队期间 AI 继续回答）：不再因为高意向转人工。
    queued: bool = False


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
    # 模型调用 request_human_handoff 时给出的交接摘要（不再单独生成摘要）。
    summary: str | None = None
    # 正在向客户追问待办或订单的信息：这一轮不计入 AI 接待轮次（设计文档 §24.4、§25.3）。
    collecting: bool = False
    # 更新后的"连续没找到商品"次数；为空时不变。
    product_misses: int | None = None

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


def _multi(question: str) -> bool:
    """一条消息里可能有多个问题（需要拆开检索）。"""
    return bool(_SEPARATORS.search(question.strip().rstrip("？?。.!！")))


async def rewrite_queries(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    settings: AiSettings,
    question: str,
    history: list[Turn],
    *,
    session_id: uuid.UUID | None = None,
) -> list[str]:
    """问题改写（设计文档 §11.1）：补全指代、拆分多问，得到用于检索的独立问题。

    只在有上文或一条消息里有多个问题时改写；失败时用原问题。输入已脱敏。
    """
    if not settings.rewrite_enabled or not (history or _multi(question)):
        return [question]
    prompt = await ctx.prompts.get("rewrite")
    messages = prompts.rewrite_messages(
        history=history[-REWRITE_HISTORY:], question=question, template=prompt.content
    )
    try:
        result = await gateway.chat(
            ctx,
            tenant_id,
            messages,
            scene="rewrite",
            fast=True,
            json_mode=True,
            max_tokens=300,
            session_id=session_id,
            prompt_version=prompt.version,
        )
    except LLMUnavailable:
        return [question]
    start, end = result.content.find("{"), result.content.rfind("}")
    try:
        data = json.loads(result.content[start : end + 1]) if start >= 0 else {}
    except json.JSONDecodeError:
        data = {}
    queries = [
        str(q).strip()[:200]
        for q in (data.get("queries") if isinstance(data, dict) else None) or []
        if str(q).strip()
    ]
    return queries[:MAX_QUERIES] or [question]


async def _retrieve(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    queries: list[str],
    vector: list[float] | None,
    space_ids: list[uuid.UUID],
) -> list[Hit]:
    """每个改写后的问题分别检索，同一条目取最高的相关度。"""
    best: dict[uuid.UUID, Hit] = {}
    async with ctx.db.tenant_session(tenant_id) as db:
        for index, query in enumerate(queries):
            hits = await search(
                ctx,
                db,
                tenant_id,
                query,
                visibilities=("public",),
                limit=KNOWLEDGE_LIMIT,
                space_ids=space_ids or None,
                vector=vector if index == 0 else None,
            )
            for hit in hits:
                if hit.item_id not in best or hit.score > best[hit.item_id].score:
                    best[hit.item_id] = hit
    return sorted(best.values(), key=lambda h: h.score, reverse=True)[:KNOWLEDGE_LIMIT]


async def _embed_query(ctx: AppContext, tenant_id: uuid.UUID, query: str) -> list[float] | None:
    if not await ctx.llms.embed_enabled():
        return None
    try:
        [vector] = await gateway.embed(ctx, tenant_id, [query], scene="search")
    except LLMUnavailable as exc:
        logger.warning("embedding failed, keyword search only: %s", exc)
        return None
    return vector


def _passage(hit: Hit) -> Passage:
    return Passage(
        item_id=str(hit.item_id), kind=hit.kind, title=hit.title, text=hit.text, score=hit.score
    )


def _knowledge(passages: list[Passage]) -> list[dict[str, Any]]:
    return [
        {"item_id": p.item_id, "title": p.title, "score": round(p.score, 4), "used": True}
        for p in passages
    ]


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
    channel: ChannelAi | None = None,
    customer_id: uuid.UUID | None = None,
) -> Outcome:
    channel = channel or ChannelAi()
    handoff_threshold = channel.handoff_threshold or settings.handoff_threshold
    relevance_threshold = (
        channel.relevance_threshold
        if channel.relevance_threshold is not None
        else settings.relevance_threshold
    )
    max_turns = channel.max_turns or settings.max_turns
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
    judged = context.intent
    if judged is not None and (judged.human or 0) >= HUMAN_THRESHOLD:
        # 关键词没有列出的说法，如"能不能让你们的人跟我说"。
        return Outcome(
            action="handoff",
            reason="customer_request",
            signals={"intent_human": round(judged.human or 0, 2)},
            repeats=context.repeats,
        )
    stage = settings.intent_handoff_stage
    if judged is not None and stage and not context.queued and judged.reached(stage):
        return Outcome(
            action="handoff",
            reason="purchase_intent",
            signals={
                "intent_stage": judged.stage,
                "intent_purchase": round(judged.purchase_probability, 2),
            },
            repeats=context.repeats,
        )
    negative_hint = (
        judged is not None and judged.emotion is not None and judged.emotion >= NEGATIVE_EMOTION
    )

    # 套价（设计文档 §25.2）：固定话术答复，不交给模型自由发挥。
    orders = await order_ai.config(ctx, tenant_id)
    probe = price_guard.probe(question) if orders is not None else None
    if probe is not None:
        if scene == "reply" and session_id is not None:
            await price_guard.record_probe(
                ctx,
                tenant_id,
                session_id=session_id,
                customer_id=customer_id,
                question=question,
                category=probe,
                message_id=context.message_ids[-1] if context.message_ids else None,
            )
        return Outcome(
            action="reply",
            reason="price_probe",
            reply=price_guard.FIXED_REPLY,
            guard="price_probe",
            signals={"probe": probe},
            repeats=context.repeats,
        )

    masked_question, mapping = pii.mask(question)
    history = [Turn(t.role, pii.mask(t.text, mapping)[0]) for t in context.history]
    queries = await rewrite_queries(
        ctx, tenant_id, settings, masked_question, history, session_id=session_id
    )
    rewritten = queries != [masked_question]
    vector = await _embed_query(ctx, tenant_id, queries[0])
    hits = await _retrieve(ctx, tenant_id, queries, vector, channel.space_ids)
    passages = [_passage(h) for h in hits]
    best = max((h.score for h in hits), default=0.0)
    extra: dict[str, Any] = {"rewritten": queries} if rewritten else {}
    # 商品库里相关的商品（只有对客可见的字段）。
    products = (
        await order_ai.candidates(ctx, tenant_id, queries, price=orders.price, vector=vector)
        if orders is not None
        else []
    )
    best = max([best, *(p.score for p in products)])

    # 语义缓存：独立、不含个人信息的问题直接用之前的回答（仍然计算软信号）。
    cacheable = (
        scene == "reply"
        and settings.answer_cache
        and vector is not None
        and len(queries) == 1
        and not mapping
        # 限定了知识空间的渠道不共用缓存（缓存的回答可能来自别的空间）。
        and not channel.space_ids
        # 涉及商品和价格的回答不进缓存（价格会变）。
        and not products
    )
    if cacheable:
        assert vector is not None
        cached = await answer_cache.lookup(ctx, tenant_id, vector)
        if cached is not None:
            cached_best = max((float(k.get("score", 0)) for k in cached.knowledge), default=0.0)
            found, repeats = decision.signals(
                question,
                best_relevance=cached_best,
                relevance_threshold=relevance_threshold,
                confidence=cached.confidence,
                previous_question=context.previous_question,
                repeats=context.repeats,
                turns=context.turns + 1,
                max_turns=max_turns,
                negative_hint=negative_hint,
            )
            values = {**found.active(), **found.details, **extra}
            values["cache"] = round(cached.similarity, 4)
            if found.score >= handoff_threshold:
                return Outcome(
                    action="handoff",
                    reason="score",
                    score=found.score,
                    signals=values,
                    knowledge=cached.knowledge,
                    repeats=repeats,
                )
            return Outcome(
                action="reply",
                reply=cached.answer,
                score=found.score,
                signals=values,
                knowledge=cached.knowledge,
                repeats=repeats,
            )

    prompt = await ctx.prompts.get("reply")
    use_tools = (
        settings.tools_enabled
        and scene in ("reply", "test")
        and await ctx.llms.tools_supported(tenant_id, scene)
    )
    order_tools = orders if use_tools else None
    toolbox = ToolBox(
        ctx=ctx,
        tenant_id=tenant_id,
        session_id=session_id if scene == "reply" else None,
        customer_id=customer_id if scene == "reply" else None,
        mapping=mapping,
        space_ids=channel.space_ids or None,
        todo_types=await todo_ai.ai_types(ctx, tenant_id) if use_tools else [],
        evidence_ids=list(context.message_ids),
        orders=order_tools,
        question=question,
        product_misses=context.product_misses,
        involved={p.product_id for p in products},
    )
    messages: list[dict[str, Any]] = list(
        prompts.reply_messages(
            company=company,
            bot_name=settings.bot_name,
            persona=settings.persona,
            passages=passages,
            history=history[-HISTORY_LIMIT:],
            question=masked_question,
            intents=intents,
            template=prompt.content,
            tools=use_tools,
            products=[p.line for p in products],
            rules=(
                prompts.order_rules(tools=use_tools, ordering=orders.ordering, price=orders.price)
                if orders is not None
                else None
            ),
            judgment=prompt_block(judged) if judged is not None else None,
        )
    )
    try:
        result: ChatResult | None = None
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            result = await gateway.chat(
                ctx,
                tenant_id,
                messages,
                scene=scene,
                json_mode=True,
                session_id=session_id,
                tools=(
                    specs(toolbox.todo_types, order_tools)
                    if use_tools and round_ < MAX_TOOL_ROUNDS
                    else None
                ),
                prompt_version=prompt.version,
            )
            if not result.tool_calls or result.message is None:
                break
            messages.append(result.message)
            for call in result.tool_calls:
                output = await toolbox.run(call)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
            if toolbox.handoff is not None:
                break
        assert result is not None
    except LLMUnavailable as exc:
        logger.warning("AI reply unavailable for tenant %s: %s", tenant_id, exc)
        return Outcome(
            action="handoff",
            reason="ai_unavailable",
            knowledge=_knowledge(passages),
            product_misses=toolbox.product_misses,
        )

    known = {p.item_id for p in passages}
    passages += [p for p in toolbox.passages if p.item_id not in known]
    best = max([best, *(p.score for p in toolbox.passages)])
    knowledge = _knowledge(passages)
    if toolbox.log:
        extra["tools"] = [call["name"] for call in toolbox.log]
    if toolbox.handoff is not None:
        request = toolbox.handoff
        return Outcome(
            action="handoff",
            reason="model_request",
            signals={
                **extra,
                "model_reason": request["reason"],
                "category": request["category"],
                "urgency": request["urgency"],
            },
            knowledge=knowledge,
            summary=request["summary"] or None,
            product_misses=toolbox.product_misses,
        )
    if toolbox.product_misses >= order_ai.MISSES_TO_HANDOFF:
        # 连续两次找不到客户要的商品：转人工（已采集的订单信息在草稿里，坐席接着处理）。
        wanted = "、".join(dict.fromkeys(toolbox.missed)) or question[:100]
        return Outcome(
            action="handoff",
            reason="product_not_found",
            signals={**extra, "missed": toolbox.missed},
            knowledge=knowledge,
            summary=f"客户要的商品在商品库里没有找到：{wanted}",
            product_misses=0,
        )

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
                signals=extra,
                knowledge=knowledge,
                guard_failures=failures,
                product_misses=toolbox.product_misses,
            )
        return Outcome(
            action="reply",
            reason="guardrail_retry",
            reply=SAFE_FALLBACK,
            guard=violation,
            signals=extra,
            knowledge=[{**k, "used": False} for k in knowledge],
            repeats=context.repeats,
            guard_failures=failures,
            product_misses=toolbox.product_misses,
        )
    # 回复检查（设计文档 §25.2 第 3 条）：内部价格口径、成本价金额 → 拦截，改用固定话术。
    blocked = (
        await order_ai.price_check(
            ctx,
            tenant_id,
            parsed["reply"],
            product_ids=toolbox.involved,
            session_id=session_id if scene == "reply" else None,
        )
        if orders is not None
        else None
    )
    if blocked is not None:
        if scene == "reply":
            await price_guard.record_block(
                ctx,
                tenant_id,
                session_id=session_id,
                customer_id=customer_id,
                reason=blocked,
                question=question,
            )
        return Outcome(
            action="reply",
            reason="reply_blocked",
            reply=price_guard.FIXED_REPLY,
            guard=f"price_{blocked}",
            signals=extra,
            knowledge=[{**k, "used": False} for k in knowledge],
            repeats=context.repeats,
            guard_failures=context.guard_failures,
            collecting=toolbox.collecting,
            product_misses=toolbox.product_misses,
        )

    intent = parsed["intent"] if intents and parsed["intent"] in intents else None
    if parsed["handoff"]:
        return Outcome(
            action="handoff",
            reason="model_request",
            reply=parsed["reply"] or None,
            signals={**extra, "model_reason": parsed["reason"]},
            knowledge=knowledge,
            intent=intent,
            product_misses=toolbox.product_misses,
        )

    # 正在采集订单信息（这个会话有 AI 的订单草稿）的追问不计入 AI 接待轮次。
    collecting = toolbox.collecting or (
        orders is not None
        and orders.ordering
        and toolbox.session_id is not None
        and await order_ai.collecting(ctx, tenant_id, toolbox.session_id)
    )
    found, repeats = decision.signals(
        question,
        best_relevance=best,
        relevance_threshold=relevance_threshold,
        confidence=parsed["confidence"],
        previous_question=context.previous_question,
        repeats=context.repeats,
        turns=context.turns + (0 if collecting else 1),
        max_turns=max_turns,
        negative_hint=negative_hint,
    )
    signal_values = {**found.active(), **found.details, **extra}
    if found.score >= handoff_threshold:
        return Outcome(
            action="handoff",
            reason="score",
            score=found.score,
            signals=signal_values,
            knowledge=knowledge,
            repeats=repeats,
            intent=intent,
            product_misses=toolbox.product_misses,
        )
    if (
        cacheable
        and vector is not None
        and not context.history
        and not toolbox.log
        and hits
        and parsed["confidence"] >= CACHE_MIN_CONFIDENCE
    ):
        await answer_cache.store(
            ctx,
            tenant_id,
            question=queries[0],
            vector=vector,
            answer=parsed["reply"],
            confidence=parsed["confidence"],
            knowledge=knowledge,
        )
    return Outcome(
        action="reply",
        reply=parsed["reply"],
        score=found.score,
        signals=signal_values,
        knowledge=knowledge,
        repeats=repeats,
        intent=intent,
        collecting=collecting,
        product_misses=toolbox.product_misses,
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
    prompt = await ctx.prompts.get("summary")
    try:
        result = await gateway.chat(
            ctx,
            tenant_id,
            prompts.summary_messages(history=masked, reason=label, template=prompt.content),
            scene="summary",
            fast=True,
            max_tokens=300,
            session_id=session_id,
            prompt_version=prompt.version,
        )
    except LLMUnavailable:
        return fallback
    text = pii.unmask(result.content.strip(), mapping)
    return text[:500] or fallback
