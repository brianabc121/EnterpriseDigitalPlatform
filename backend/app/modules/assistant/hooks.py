"""IM 机器人的回调入口（设计文档 §27.6）：/hooks/assistant/{bot_id}/{token}。

按机器人 ID 跨租户找到机器人，核对地址里的随机令牌，再交给平台的适配器验签、解析；收到的消息写入
事件流后立即返回（飞书的 url_verification、WhatsApp 和企业微信的地址验证直接应答）。
"""

import hmac
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select

from app.context import AppContext
from app.core.deps import get_context
from app.core.errors import Forbidden, NotFound, Unprocessable
from app.events.bus import Event, EventType
from app.integrations.imbots import Rejected, WebhookRequest, adapter_for
from app.modules.assistant import inbound, service
from app.modules.assistant.models import AssistantBot, BotStatus
from app.observability import metrics
from app.observability.context import note_tenant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hooks/assistant", include_in_schema=False)
Context = Annotated[AppContext, Depends(get_context)]
_MAX_BODY = 256 * 1024


async def _bot(ctx: AppContext, bot_id: uuid.UUID, token: str) -> AssistantBot:
    async with ctx.db.platform_sessionmaker() as session:
        bot = await session.scalar(select(AssistantBot).where(AssistantBot.id == bot_id))
        if bot is None:
            raise NotFound("回调地址不存在")
        session.expunge(bot)
    if not hmac.compare_digest(bot.webhook_token, token):
        raise NotFound("回调地址不存在")
    if bot.status != BotStatus.ACTIVE:
        raise Forbidden("机器人已停用")
    return bot


@router.api_route("/{bot_id}/{token}", methods=["GET", "POST"])
async def receive(bot_id: uuid.UUID, token: str, request: Request, ctx: Context) -> Response:
    bot = await _bot(ctx, bot_id, token)
    note_tenant(bot.tenant_id)
    body = await request.body() if request.method == "POST" else b""
    if len(body) > _MAX_BODY:
        raise Unprocessable("回调内容过大")
    context = await service.bot_context(ctx, bot)
    try:
        result = await adapter_for(bot.provider).receive(
            WebhookRequest(
                method=request.method,
                query={k: v for k, v in request.query_params.items()},
                headers={k.lower(): v for k, v in request.headers.items()},
                body=body,
            ),
            context,
        )
    except Rejected as exc:
        metrics.WEBHOOKS.labels("assistant", metrics.tenant_label(bot.tenant_id), "rejected").inc()
        logger.info("assistant bot %s callback rejected: %s", bot.id, exc)
        raise Forbidden("回调验签失败") from exc
    for message in result.messages:
        await ctx.bus.publish(
            Event(
                type=EventType.ASSISTANT_INBOUND,
                tenant_id=bot.tenant_id,
                key=f"assistant:{bot.id}:{message.chat_id}",
                data={"bot_id": str(bot.id), "message": inbound.encode(message)},
            )
        )
    metrics.WEBHOOKS.labels("assistant", metrics.tenant_label(bot.tenant_id), "queued").inc()
    if result.response is not None:
        return Response(
            content=result.response.body,
            media_type=result.response.media_type,
            status_code=result.response.status_code,
        )
    return PlainTextResponse("ok")
