"""访客端：Widget 的"服务进度"（设计文档 §24.9，可选，默认关闭）。

列出访客自己的待办：类型、状态、预计完成时间和员工标记为客户可见的进度说明，不显示内部评论。
匿名访客只能看到在当前访客身份下登记的。
"""

from datetime import UTC, datetime

from sqlalchemy import select

from app.core.errors import NotFound
from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.conversation.models import Room
from app.modules.customer.models import CustomerIdentity
from app.modules.todos import settings as todo_settings
from app.modules.todos.ai import CUSTOMER_STATUS, customer_scope
from app.modules.todos.models import Todo, TodoStatus, TodoType
from app.modules.todos.schemas import VisitorTodoList, VisitorTodoOut
from app.modules.visitor.deps import VisitorContext

LIMIT = 20


async def progress(visitor: VisitorContext) -> VisitorTodoList:
    session = visitor.session
    settings = await todo_settings.load(session, visitor.claims.tenant_id)
    if not settings.visitor_progress:
        return VisitorTodoList(enabled=False, items=[])
    room = await session.scalar(select(Room).where(Room.identity_id == visitor.claims.identity_id))
    if room is None:
        raise NotFound("会话不存在")
    identity = await session.get(CustomerIdentity, room.identity_id)
    channel = await session.get(ChannelAccount, room.channel_account_id)
    anonymous = (
        channel is not None
        and channel.type == ChannelType.WEB
        and not (identity is not None and identity.verified)
    )
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(Todo, TodoType.name)
            .join(TodoType, TodoType.id == Todo.type_id)
            .where(*customer_scope(room.customer_id, room.id if anonymous else None, now))
            .order_by(Todo.created_at.desc())
            .limit(LIMIT)
        )
    ).all()
    return VisitorTodoList(
        enabled=True,
        items=[
            VisitorTodoOut(
                no=todo.no,
                type_name=type_name,
                title=todo.title,
                status=todo.status,
                status_label=CUSTOMER_STATUS.get(todo.status, "处理中"),
                due_at=(
                    todo.due_at
                    if todo.status in (TodoStatus.OPEN, TodoStatus.IN_PROGRESS)
                    else None
                ),
                progress_note=todo.progress_note,
                created_at=todo.created_at,
                closed_at=todo.closed_at,
            )
            for todo, type_name in rows
        ],
    )
