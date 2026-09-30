"""待办动态：所有操作都写入，完整可追溯（设计文档 §24.5）。"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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
