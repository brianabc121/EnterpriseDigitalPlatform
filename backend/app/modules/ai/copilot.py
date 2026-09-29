"""坐席助手的实时提醒（设计文档 §11.4）：人工接待期间，只给坐席看。

- 客户情绪负面：提醒安抚（同一会话 10 分钟内只提醒一次）；最近几条消息里多次出现负面情绪时
  提醒"情绪持续激动"（每个会话一次）。
- 客户发来身份证号、银行卡号：提醒保护隐私。
- 坐席的回复里有承诺类用语（保证、赔偿、全额退款……）：提醒确认是否符合公司政策。

提醒写入 copilot_alerts 留痕（质检），并经在线信令推给接待坐席（协助者发的消息推给本人）。
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai import decision, pii
from app.modules.ai.models import AlertKind, CopilotAlert
from app.modules.conversation import outbox
from app.modules.conversation.models import (
    ChatSession,
    Message,
    SenderType,
    SessionStatus,
)

SIGNAL = "copilot.alert"
RECENT = 3
ESCALATION_COUNT = 2
NEGATIVE_GAP = timedelta(minutes=10)
# 只提醒身份证号、银行卡号：手机号、邮箱是客户常规留下的联系方式。
PRIVATE_INFO = ("身份证号", "银行卡号")

TEXTS = {
    AlertKind.NEGATIVE: "客户情绪不太好，注意安抚，先回应客户的感受。",
    AlertKind.ESCALATION: "客户情绪持续激动，建议尽快给出解决方案，必要时请主管协助。",
    AlertKind.SENSITIVE_INFO: "客户发来了{label}，请注意保护，不要转发或复制到其他地方。",
    AlertKind.PROMISE: "回复里有承诺类用语「{word}」，请确认符合公司政策。",
}
SERVING = (SessionStatus.HUMAN_SERVING, SessionStatus.TRANSFERRING)


async def _last_alert(session: AsyncSession, session_id: uuid.UUID, kind: str) -> datetime | None:
    return await session.scalar(
        select(CopilotAlert.created_at)
        .where(CopilotAlert.session_id == session_id, CopilotAlert.kind == kind)
        .order_by(CopilotAlert.created_at.desc())
        .limit(1)
    )


def _alert(
    session: AsyncSession,
    chat: ChatSession,
    message: Message,
    kind: AlertKind,
    staff_id: uuid.UUID,
    **detail: str,
) -> None:
    text = TEXTS[kind].format(**detail)
    session.add(
        CopilotAlert(
            tenant_id=chat.tenant_id,
            session_id=chat.id,
            staff_id=staff_id,
            message_id=message.id,
            kind=kind.value,
            detail={**detail, "text": text},
        )
    )
    outbox.enqueue_signal(
        session,
        chat.room_id,
        staff_id,
        {
            "type": SIGNAL,
            "session_id": str(chat.id),
            "room_id": str(chat.room_id),
            "kind": kind.value,
            "text": text,
        },
    )


async def inspect_customer(
    session: AsyncSession, chat: ChatSession, message: Message, now: datetime
) -> bool:
    """客户的新消息（已归入会话）。返回是否发出了提醒。"""
    if chat.status not in SERVING or chat.assignee_id is None:
        return False
    text = message.text_plain or ""
    sent = False
    for label in pii.detect(text):
        if label in PRIVATE_INFO:
            _alert(session, chat, message, AlertKind.SENSITIVE_INFO, chat.assignee_id, label=label)
            sent = True
    if not decision.is_negative(text):
        return sent
    recent = (
        await session.scalars(
            select(Message.text_plain)
            .where(
                Message.session_id == chat.id,
                Message.sender_type == SenderType.CUSTOMER,
                Message.id != message.id,
            )
            .order_by(Message.sent_at.desc())
            .limit(RECENT - 1)
        )
    ).all()
    negatives = 1 + sum(1 for t in recent if t and decision.is_negative(t))
    if (
        negatives >= ESCALATION_COUNT
        and await _last_alert(session, chat.id, AlertKind.ESCALATION) is None
    ):
        _alert(session, chat, message, AlertKind.ESCALATION, chat.assignee_id)
        return True
    last = await _last_alert(session, chat.id, AlertKind.NEGATIVE)
    if last is None or last <= now - NEGATIVE_GAP:
        _alert(session, chat, message, AlertKind.NEGATIVE, chat.assignee_id)
        return True
    return sent


def inspect_agent(session: AsyncSession, chat: ChatSession, message: Message) -> bool:
    """坐席（或协助者）刚发出的消息。返回是否发出了提醒。"""
    word = decision.promise_word(message.text_plain or "")
    if word is None or message.sender_id is None:
        return False
    _alert(session, chat, message, AlertKind.PROMISE, message.sender_id, word=word)
    return True
