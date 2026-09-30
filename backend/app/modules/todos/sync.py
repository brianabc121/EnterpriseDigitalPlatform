"""企业系统通过开放接口创建待办（设计文档 §24.3、§25.8）：直接进入待办列表，按类型的规则分派；
带上企业系统自己的单号（external_ref）时，重复创建返回已有的待办。"""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import NotFound, Unprocessable
from app.modules.customer.models import Customer
from app.modules.integration.schemas import OpenTodoCreate
from app.modules.orders.models import Order, OrderStatus
from app.modules.todos import notify
from app.modules.todos import service as todos
from app.modules.todos.actions import _clean_or_raise
from app.modules.todos.models import ActorType, Todo, TodoSource, TodoType
from app.modules.todos.service import utcnow


async def create(
    ctx: AppContext, session: AsyncSession, key_id: uuid.UUID, payload: OpenTodoCreate
) -> tuple[Todo, bool]:
    """返回（待办，是否新建）。"""
    ref = (payload.external_ref or "").strip() or None
    if ref is not None:
        existing = await session.scalar(select(Todo).where(Todo.external_ref == ref))
        if existing is not None:
            return existing, False
    type_ = await session.scalar(select(TodoType).where(TodoType.code == payload.type.strip()))
    if type_ is None or not type_.enabled or type_.system:
        raise Unprocessable(f"待办类型 {payload.type} 不存在、已停用或不能通过接口创建")
    customer_id = payload.customer_id
    if customer_id is not None and await session.get(Customer, customer_id) is None:
        raise NotFound("客户不存在")
    order_id: uuid.UUID | None = None
    if payload.order_no:
        order = await session.scalar(
            select(Order)
            .where((Order.no == payload.order_no) | (Order.external_no == payload.order_no))
            .limit(1)
        )
        if order is None or order.status == OrderStatus.DRAFT:
            raise NotFound("订单不存在")
        if customer_id is not None and order.customer_id not in (None, customer_id):
            raise Unprocessable("订单不属于这位客户")
        order_id = order.id
        customer_id = customer_id or order.customer_id
    now = utcnow()
    if payload.due_at is not None and payload.due_at <= now:
        raise Unprocessable("截止时间必须晚于现在")
    todo = await todos.create(
        session,
        ctx.keys,
        todos.Draft(
            type=type_,
            title=payload.title.strip(),
            detail=payload.detail.strip(),
            fields=_clean_or_raise(type_, payload.fields),
            source=TodoSource.API,
            created_by_type=ActorType.API,
            created_by=key_id,
            customer_id=customer_id,
            order_id=order_id,
            due_at=payload.due_at,
            priority=payload.priority,
        ),
        now=now,
    )
    todo.external_ref = ref
    try:
        await session.commit()
    except IntegrityError:
        # 同一个单号的两个请求同时到达：后写入的一个返回先建好的待办。
        await session.rollback()
        if ref is None:
            raise
        existing = await session.scalar(select(Todo).where(Todo.external_ref == ref))
        if existing is None:
            raise
        return existing, False
    await notify.dispatch(ctx, tenant_id=todo.tenant_id, ids=[todo.id])
    return todo, True
