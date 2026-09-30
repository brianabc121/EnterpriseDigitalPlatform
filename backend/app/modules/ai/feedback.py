"""访客评价 AI 回答（设计文档 §12.7：单条知识的满意度）。

访客在 Widget 里对智能客服的一条回答点"有用"或"没用"；评价记在这条回答所依据的知识上
（kb_items.visitor_likes / visitor_dislikes）。同一条回答可以改评价，计数随之调整。
回答与判定的对应：同一会话里、这条消息发出之前最近的一次回复判定。
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.modules.ai.models import AiDecision, AiMessageFeedback, DecisionAction
from app.modules.conversation.models import Message, SenderType
from app.modules.kb.models import KbItem


async def rate_answer(
    session: AsyncSession, room_id: UUID, server_msg_id: str, value: int
) -> AiMessageFeedback:
    message = await session.scalar(
        select(Message).where(
            Message.room_id == room_id,
            Message.channel_msg_id == server_msg_id,
            Message.sender_type == SenderType.BOT,
        )
    )
    if message is None or message.session_id is None:
        raise NotFound("没有找到这条回答")
    feedback = await session.get(AiMessageFeedback, message.id, with_for_update=True)
    if feedback is None:
        decision = await session.scalar(
            select(AiDecision)
            .where(
                AiDecision.session_id == message.session_id,
                AiDecision.action == DecisionAction.REPLY,
                AiDecision.created_at <= message.sent_at,
            )
            .order_by(AiDecision.created_at.desc())
            .limit(1)
        )
        item_ids = [
            UUID(k["item_id"])
            for k in (decision.knowledge if decision else [])
            if k.get("used") and k.get("item_id")
        ]
        feedback = AiMessageFeedback(
            tenant_id=message.tenant_id,
            message_id=message.id,
            session_id=message.session_id,
            value=value,
            item_ids=item_ids,
        )
        session.add(feedback)
        await _count(session, item_ids, value, 1)
    elif feedback.value != value:
        await _count(session, feedback.item_ids, feedback.value, -1)
        await _count(session, feedback.item_ids, value, 1)
        feedback.value = value
    await session.commit()
    return feedback


async def _count(session: AsyncSession, item_ids: list[UUID], value: int, delta: int) -> None:
    if not item_ids:
        return
    column = KbItem.visitor_likes if value > 0 else KbItem.visitor_dislikes
    await session.execute(
        update(KbItem).where(KbItem.id.in_(item_ids)).values({column: column + delta})
    )
