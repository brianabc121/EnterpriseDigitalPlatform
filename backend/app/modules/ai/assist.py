"""坐席助手（Copilot，设计文档 §11.4）：人工接待时给坐席的建议回复。

只对坐席可见，不会自动发给客户。租户开通了订单功能时，一并给出商品库里相关的商品（只有对客可见的
字段和建议零售价，不带成本价），建议回复同样经过价格保护的回复检查（设计文档 §25.2）。
"""

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway, pii, prompts
from app.modules.ai.models import AiSuggestion
from app.modules.ai.prompts import Passage, Turn
from app.modules.ai.schemas import KnowledgeRef, SuggestionList
from app.modules.billing.entitlements import has_feature
from app.modules.conversation.models import Message, SenderType
from app.modules.iam.principal import Principal
from app.modules.kb.search import search
from app.modules.kb.service import visibilities_for
from app.modules.orders import ai as order_ai
from app.modules.sessions.service import visible_session

HISTORY = 12
MAX_SUGGESTIONS = 3
_ROLES: dict[str, str] = {
    SenderType.CUSTOMER: "customer",
    SenderType.AGENT: "agent",
    SenderType.BOT: "bot",
}


def _parse(content: str) -> list[str]:
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return []
    items = data.get("suggestions") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [s.strip()[:300] for s in items if isinstance(s, str) and s.strip()][:MAX_SUGGESTIONS]


async def suggest(
    ctx: AppContext, session: AsyncSession, principal: Principal, session_id: uuid.UUID
) -> SuggestionList:
    chat, *_ = await visible_session(session, principal, session_id)
    rows = (
        await session.scalars(
            select(Message)
            .where(Message.session_id == chat.id, Message.text_plain.is_not(None))
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(HISTORY)
        )
    ).all()
    history = [
        Turn(_ROLES[m.sender_type], m.text_plain or "")
        for m in reversed(rows)
        if m.sender_type in _ROLES
    ]
    asked = [i for i, t in enumerate(history) if t.role == "customer"]
    if not asked:
        return SuggestionList(suggestions=[], knowledge=[])
    # 针对客户最近的一个问题给建议；之前的对话作为上下文。
    suggestions, knowledge = await draft(
        ctx,
        session,
        principal,
        history[: asked[-1]],
        history[asked[-1]].text,
        session_id=chat.id,
    )
    return await _logged(session, principal, chat.id, suggestions, knowledge)


async def draft(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    history: list[Turn],
    question: str,
    *,
    session_id: uuid.UUID | None = None,
) -> tuple[list[str], list[KnowledgeRef]]:
    """按员工的可见范围检索知识，结合对话上下文生成 1–3 条建议回复。

    没有大模型或模型不可用时，直接用检索到的知识答案。工作台和企业微信侧边栏共用。
    """
    hits = await search(
        ctx,
        session,
        principal.tenant_id,
        question,
        visibilities=visibilities_for(principal),
        limit=3,
    )
    knowledge = [
        KnowledgeRef(item_id=h.item_id, title=h.title, score=round(h.score, 4)) for h in hits
    ]
    orders = await order_ai.config(ctx, principal.tenant_id)
    products = (
        await order_ai.candidates(ctx, principal.tenant_id, [question], price=True)
        if orders is not None
        else []
    )
    involved = {p.product_id for p in products}

    async def safe(texts: list[str]) -> list[str]:
        """去掉出现内部价格口径或成本价金额的建议。"""
        if orders is None:
            return texts
        return [
            text
            for text in texts
            if await order_ai.price_check(
                ctx, principal.tenant_id, text, product_ids=involved, session_id=session_id
            )
            is None
        ]

    fallback = await safe([h.text for h in hits][:MAX_SUGGESTIONS])
    if not await ctx.llms.chat_enabled(principal.tenant_id, "suggest") or not await has_feature(
        session, principal.tenant_id, "ai"
    ):
        return fallback, knowledge
    mapping: dict[str, str] = {}
    masked = [Turn(t.role, pii.mask(t.text, mapping)[0]) for t in history]
    passages = [
        Passage(item_id=str(h.item_id), kind=h.kind, title=h.title, text=h.text, score=h.score)
        for h in hits
    ]
    prompt = await ctx.prompts.get("suggest")
    try:
        result = await gateway.chat(
            ctx,
            principal.tenant_id,
            prompts.suggest_messages(
                passages=passages,
                history=masked,
                question=pii.mask(question, mapping)[0],
                template=prompt.content,
                products=[p.line for p in products],
            ),
            scene="suggest",
            fast=True,
            json_mode=True,
            session_id=session_id,
            prompt_version=prompt.version,
        )
    except LLMUnavailable:
        return fallback, knowledge
    suggestions = await safe([pii.unmask(s, mapping) for s in _parse(result.content)])
    return suggestions or fallback, knowledge


async def _logged(
    session: AsyncSession,
    principal: Principal,
    session_id: uuid.UUID,
    suggestions: list[str],
    knowledge: list[KnowledgeRef],
) -> SuggestionList:
    """记下这次建议（统计采纳率：坐席发送时标明来源为 AI 建议）。"""
    if suggestions:
        session.add(
            AiSuggestion(
                tenant_id=principal.tenant_id,
                session_id=session_id,
                staff_id=principal.staff_id,
                suggestions=suggestions,
            )
        )
        await session.commit()
    return SuggestionList(suggestions=suggestions, knowledge=knowledge)
