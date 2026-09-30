"""待办导出（CSV，设计文档 §24）。

需要 todo:export 权限并再次输入密码，只导出数据范围内、符合待办中心筛选条件的待办。敏感字段
（电话、地址、证件号等）在员工有 customer:view_sensitive 权限时导出明文，否则导出掩码。
每次导出记审计（条数、是否含明文）。
"""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.csvfile import BOM, line
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.routing.models import SkillGroup
from app.modules.todos import fields as todo_fields
from app.modules.todos import sla
from app.modules.todos.models import Todo, TodoType

BATCH = 200
HEADER = [
    "编号",
    "类型",
    "标题",
    "描述",
    "状态",
    "优先级",
    "来源",
    "客户",
    "处理人",
    "技能组",
    "截止时间",
    "客户期望时间",
    "创建时间",
    "完成时间",
    "处理结果",
    "字段",
]
STATUS = {
    "pending": "待确认",
    "open": "待处理",
    "in_progress": "处理中",
    "waiting": "等待客户",
    "done": "已完成",
    "cancelled": "已取消",
    "rejected": "已驳回",
}
PRIORITY = {"urgent": "紧急", "high": "高", "normal": "普通", "low": "低"}
SOURCE = {
    "ai_chat": "AI 接待",
    "ai_summary": "会话后解析",
    "zone": "专区",
    "copilot": "工作台",
    "sidebar": "侧边栏",
    "staff": "员工新建",
    "visitor": "访客留言",
    "rule": "系统规则",
    "api": "企业系统",
}


async def count(session: AsyncSession, where: ColumnElement[bool]) -> int:
    return int(await session.scalar(select(func.count()).select_from(Todo).where(where)) or 0)


def _time(value: datetime | None, tz: Any) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M") if value else ""


async def _names(
    session: AsyncSession, model: Any, column: Any, ids: set[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.execute(select(model.id, column).where(model.id.in_(wanted)))
    return {row_id: name for row_id, name in rows}


async def rows(
    ctx: AppContext, principal: Principal, where: ColumnElement[bool], *, plaintext: bool
) -> AsyncIterator[bytes]:
    """逐批生成 CSV（按创建时间）。使用自己的数据库会话。"""
    yield (BOM + line(HEADER)).encode()
    last: tuple[datetime, uuid.UUID] | None = None
    async with ctx.db.tenant_session(principal.tenant_id) as session:
        tz = sla.tz_of(await sla.business_hours(session))
        types = {t.id: t for t in await session.scalars(select(TodoType))}
        while True:
            query = select(Todo).where(where).order_by(Todo.created_at, Todo.id).limit(BATCH)
            if last is not None:
                query = query.where(tuple_(Todo.created_at, Todo.id) > last)
            batch = (await session.scalars(query)).all()
            if not batch:
                return
            customers = await _names(
                session, Customer, Customer.display_name, {t.customer_id for t in batch}
            )
            staff = await _names(session, Staff, Staff.display_name, {t.assignee_id for t in batch})
            groups = await _names(
                session, SkillGroup, SkillGroup.name, {t.skill_group_id for t in batch}
            )
            lines: list[str] = []
            for todo in batch:
                type_ = types.get(todo.type_id)
                stored = todo.fields or {}
                shown = todo_fields.labelled(type_, stored)
                if plaintext and stored:
                    plain = await todo_fields.reveal(ctx.keys, principal.tenant_id, stored)
                    shown = [(key, label, plain.get(key, value)) for key, label, value in shown]
                lines.append(
                    line(
                        [
                            todo.no,
                            type_.name if type_ else "",
                            todo.title,
                            todo.detail,
                            STATUS.get(todo.status, todo.status),
                            PRIORITY.get(todo.priority, todo.priority),
                            SOURCE.get(todo.source, todo.source),
                            customers.get(todo.customer_id) if todo.customer_id else "",
                            staff.get(todo.assignee_id) if todo.assignee_id else "",
                            groups.get(todo.skill_group_id) if todo.skill_group_id else "",
                            _time(todo.due_at, tz),
                            _time(todo.expected_at, tz),
                            _time(todo.created_at, tz),
                            _time(todo.closed_at, tz),
                            todo.result or "",
                            "；".join(f"{label}：{value}" for _, label, value in shown),
                        ]
                    )
                )
            yield "".join(lines).encode()
            last = (batch[-1].created_at, batch[-1].id)
