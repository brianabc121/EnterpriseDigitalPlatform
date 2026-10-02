"""通过机器人发文字：解密密钥、调用适配器、记下结果（最近发送时间、错误、连续失败次数）。"""

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.integrations.imbots import SendError, adapter_for
from app.integrations.imbots.base import TargetKind
from app.modules.assistant import service
from app.modules.assistant.models import AssistantBot, AssistantIdentity

logger = logging.getLogger(__name__)

TEXT_LIMIT = 2000


async def send(
    ctx: AppContext,
    session: AsyncSession,
    bot: AssistantBot,
    *,
    kind: TargetKind,
    target: str,
    text: str,
    reply_to: dict[str, str] | None = None,
) -> None:
    """发送失败抛出 SendError；结果记在机器人上（由调用方提交）。"""
    context = await service.bot_context(ctx, bot)
    now = datetime.now(UTC)
    try:
        await adapter_for(bot.provider).send_text(
            ctx.bots,
            ctx.settings,
            context,
            kind=kind,
            target=target,
            text=text[:TEXT_LIMIT],
            reply_to=reply_to,
        )
    except SendError as exc:
        bot.failures += 1
        bot.last_error = str(exc)[:500]
        logger.warning("assistant bot %s send failed: %s", bot.id, exc)
        raise
    bot.last_sent_at = now
    bot.failures = 0
    bot.last_error = None


async def send_to_identity(
    ctx: AppContext,
    session: AsyncSession,
    bot: AssistantBot,
    identity: AssistantIdentity,
    text: str,
) -> None:
    """给一位员工发：有私聊会话时按会话发，否则按用户 ID 发。"""
    if identity.chat_id:
        await send(ctx, session, bot, kind="chat", target=identity.chat_id, text=text)
    else:
        await send(ctx, session, bot, kind="user", target=identity.external_user_id, text=text)
