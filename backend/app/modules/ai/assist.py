"""坐席助手（Copilot，设计文档 §11.4）：人工接待时给坐席的建议回复。

只对坐席可见，不会自动发给客户。
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
from app.modules.conversation.models import Message, SenderType
from app.modules.iam.principal import Principal
from app.modules.kb.search import search
from app.modules.kb.service import visibilities_for
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
    question = history[asked[-1]].text
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
    fallback = [h.text for h in hits][:MAX_SUGGESTIONS]
    if not ctx.llm.enabled:
        return await _logged(session, principal, chat.id, fallback, knowledge)
    mapping: dict[str, str] = {}
    masked = [Turn(t.role, pii.mask(t.text, mapping)[0]) for t in history[: asked[-1]]]
    passages = [
        Passage(item_id=str(h.item_id), kind=h.kind, title=h.title, text=h.text, score=h.score)
        for h in hits
    ]
    try:
        result = await gateway.chat(
            ctx,
            principal.tenant_id,
            prompts.suggest_messages(
                passages=passages, history=masked, question=pii.mask(question, mapping)[0]
            ),
            scene="suggest",
            fast=True,
            json_mode=True,
            session_id=chat.id,
        )
    except LLMUnavailable:
        return await _logged(session, principal, chat.id, fallback, knowledge)
    suggestions = [pii.unmask(s, mapping) for s in _parse(result.content)] or fallback
    return await _logged(session, principal, chat.id, suggestions, knowledge)


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
