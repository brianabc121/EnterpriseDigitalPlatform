"""待办动态：所有操作都写入，完整可追溯（设计文档 §24.5）。"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.integration import outbox as webhook_outbox
from app.modules.todos.models import ActorType, Todo, TodoEvent


def record(
    session: AsyncSession,
    todo: Todo,
    type_: str,
    *,
    actor_type: str = ActorType.SYSTEM,
    actor_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        TodoEvent(
            tenant_id=todo.tenant_id,
            todo_id=todo.id,
            type=type_,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload or {},
        )
    )
    # 同一个事务里写入推送事件（企业系统对接，§25.8）。
    webhook_outbox.todo_event(session, todo, type_, actor_type=actor_type)
