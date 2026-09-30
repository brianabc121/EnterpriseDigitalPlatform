"""查看历史时把快照里的引用（员工、客户、技能组、待办类型、订单）换成名称，按类型批量查询。"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class Refs:
    staff: set[uuid.UUID] = field(default_factory=set)
    customers: set[uuid.UUID] = field(default_factory=set)
    groups: set[uuid.UUID] = field(default_factory=set)
    todo_types: set[uuid.UUID] = field(default_factory=set)
    orders: set[uuid.UUID] = field(default_factory=set)

    def add(self, kind: str, value: Any) -> None:
        """value 是快照里的字符串形式的 id（可以为空）。"""
        if not value:
            return
        try:
            getattr(self, kind).add(uuid.UUID(str(value)))
        except ValueError:
            return


@dataclass
class Names:
    staff: dict[uuid.UUID, str] = field(default_factory=dict)
    customers: dict[uuid.UUID, str] = field(default_factory=dict)
    groups: dict[uuid.UUID, str] = field(default_factory=dict)
    # 待办类型：名称和字段定义（字段的标签按现在的定义显示）。
    todo_types: dict[uuid.UUID, tuple[str, list[dict[str, Any]]]] = field(default_factory=dict)
    orders: dict[uuid.UUID, str] = field(default_factory=dict)

    def of(self, kind: str, value: Any, missing: str = "") -> str:
        if not value:
            return ""
        try:
            key = uuid.UUID(str(value))
        except ValueError:
            return missing
        found = getattr(self, kind).get(key)
        if kind == "todo_types" and found is not None:
            return str(found[0])
        return str(found) if found is not None else missing

    def type_fields(self, value: Any) -> list[dict[str, Any]]:
        """待办类型现在的字段定义。"""
        try:
            found = self.todo_types.get(uuid.UUID(str(value))) if value else None
        except ValueError:
            return []
        return found[1] if found is not None else []


async def load(session: AsyncSession, refs: Refs) -> Names:
    from app.modules.customer.models import Customer
    from app.modules.iam.models import Staff
    from app.modules.orders.models import Order
    from app.modules.routing.models import SkillGroup
    from app.modules.todos.models import TodoType

    names = Names()
    if refs.staff:
        rows = await session.execute(
            select(Staff.id, Staff.display_name).where(Staff.id.in_(refs.staff))
        )
        names.staff = {r.id: r.display_name for r in rows}
    if refs.customers:
        rows = await session.execute(
            select(Customer.id, Customer.display_name).where(Customer.id.in_(refs.customers))
        )
        names.customers = {r.id: r.display_name for r in rows}
    if refs.groups:
        rows = await session.execute(
            select(SkillGroup.id, SkillGroup.name).where(SkillGroup.id.in_(refs.groups))
        )
        names.groups = {r.id: r.name for r in rows}
    if refs.todo_types:
        rows = await session.execute(
            select(TodoType.id, TodoType.name, TodoType.fields).where(
                TodoType.id.in_(refs.todo_types)
            )
        )
        names.todo_types = {r.id: (r.name, list(r.fields or [])) for r in rows}
    if refs.orders:
        rows = await session.execute(select(Order.id, Order.no).where(Order.id.in_(refs.orders)))
        names.orders = {r.id: r.no for r in rows}
    return names
