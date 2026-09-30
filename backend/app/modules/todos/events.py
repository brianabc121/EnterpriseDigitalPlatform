"""待办动态：所有操作都写入，完整可追溯（设计文档 §24.5）。"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.history import service as history
from app.modules.history.models import RecordType
from app.modules.integration import outbox as webhook_outbox
from app.modules.todos.history import EVENT_ACTIONS
from app.modules.todos.models import REJECT_LABELS, ActorType, Todo, TodoEvent


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
    # 修改历史（§25.14）：改变待办的动态登记一个版本（评论、提醒、通知客户等不登记）。
    action = EVENT_ACTIONS.get(type_)
    if action is not None:
        reason = (payload or {}).get("reason")
        if type_ == "rejected" and isinstance(reason, str):
            reason = REJECT_LABELS.get(reason, reason)
        history.track(
            session,
            RecordType.TODO,
            todo,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason if isinstance(reason, str) else None,
        )
