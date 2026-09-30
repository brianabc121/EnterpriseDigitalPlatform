"""登记待回复：客户在 AI 接待中的会话里发消息后，等一小段时间（合并连续消息）再回复。

启用链路追踪时，触发回复的链路记在 Redis 里（10 分钟），回复时接着这条链路。
"""

import json
import uuid
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.models import AiSessionState
from app.modules.conversation.models import ChatSession
from app.observability import tracing

_TRACE_KEY = "edp:ai:trace:{}"
_TRACE_TTL_SECONDS = 600


async def schedule_reply(session: AsyncSession, chat: ChatSession, due_at: datetime) -> None:
    statement = insert(AiSessionState).values(
        session_id=chat.id, tenant_id=chat.tenant_id, due_at=due_at
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[AiSessionState.session_id],
            set_={"due_at": statement.excluded.due_at, "updated_at": func.now()},
        )
    )


async def remember_trace(redis: Redis, session_id: uuid.UUID) -> None:
    carrier = tracing.inject()
    if carrier:
        await redis.set(_TRACE_KEY.format(session_id), json.dumps(carrier), ex=_TRACE_TTL_SECONDS)


async def recall_trace(redis: Redis, session_id: uuid.UUID) -> dict[str, str] | None:
    if not tracing.enabled():
        return None
    raw = await redis.get(_TRACE_KEY.format(session_id))
    if not raw:
        return None
    data = json.loads(raw)
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else None
