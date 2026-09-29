"""登记待回复：客户在 AI 接待中的会话里发消息后，等一小段时间（合并连续消息）再回复。"""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.models import AiSessionState
from app.modules.conversation.models import ChatSession


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
