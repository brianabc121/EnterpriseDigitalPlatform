"""订单的调度任务：暂欠到期未收清时生成"催收"待办（设计文档 §25.5）。"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.context import AppContext
from app.core.dates import today
from app.modules.orders import service
from app.modules.orders.models import IN_PROGRESS, Order, OrderStatus, PaymentMethod, PaymentStatus
from app.modules.todos import notify as todo_notify
from app.modules.todos import presets, sla
from app.modules.todos import service as todo_service
from app.modules.todos.models import ActorType, TodoSource

logger = logging.getLogger(__name__)

BATCH = 500
UNPAID = (PaymentStatus.UNPAID, PaymentStatus.DEPOSIT, PaymentStatus.PARTIAL)


async def run_collections(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：暂欠订单过了约定付款日期仍未收清，生成一条"催收"待办交给订单的处理人
    （没有处理人时按催收类型的分派规则）。每个订单只生成一次；收清后自动完成。"""
    now = now or datetime.now(UTC)
    async with ctx.db.platform_sessionmaker() as session:
        candidates = (
            await session.execute(
                select(Order.tenant_id, Order.id)
                .where(
                    Order.payment_method == PaymentMethod.CREDIT,
                    Order.collection_todo_id.is_(None),
                    # 按最早的时区粗筛，逐个租户再按租户的时区判断。
                    Order.credit_due_date < (now + timedelta(days=1)).date(),
                    Order.status.in_((*IN_PROGRESS, OrderStatus.COMPLETED)),
                    Order.payment_status.in_(UNPAID),
                )
                .order_by(Order.credit_due_date)
                .limit(BATCH)
            )
        ).all()
    by_tenant: dict[uuid.UUID, list[uuid.UUID]] = {}
    for tenant_id, order_id in candidates:
        by_tenant.setdefault(tenant_id, []).append(order_id)
    created = 0
    for tenant_id, order_ids in by_tenant.items():
        todo_ids: list[uuid.UUID] = []
        async with ctx.db.tenant_session(tenant_id) as session:
            day = today(sla.tz_of(await sla.business_hours(session)), now)
            type_ = await presets.type_by_code(session, tenant_id, presets.COLLECTION)
            for order_id in order_ids:
                order = await session.scalar(
                    select(Order).where(Order.id == order_id).with_for_update(skip_locked=True)
                )
                if (
                    order is None
                    or order.collection_todo_id is not None
                    or order.credit_due_date is None
                    or order.credit_due_date >= day
                ):
                    continue
                due = service.outstanding(order)
                todo = await todo_service.create(
                    session,
                    ctx.keys,
                    todo_service.Draft(
                        type=type_,
                        title=f"催收：订单 {order.no}",
                        detail=f"约定 {order.credit_due_date:%Y-%m-%d} 付款，"
                        f"还有 {service.text_money(due)} 元未收",
                        source=TodoSource.RULE,
                        created_by_type=ActorType.SYSTEM,
                        customer_id=order.customer_id,
                        session_id=order.session_id,
                        order_id=order.id,
                        explicit=order.assignee_id is not None,
                        assignee_id=order.assignee_id,
                    ),
                    now=now,
                )
                order.collection_todo_id = todo.id
                service.event(
                    session,
                    order,
                    "collection_due",
                    actor_type=ActorType.SYSTEM,
                    actor_id=None,
                    payload={"todo_id": str(todo.id), "outstanding": service.text_money(due)},
                )
                todo_ids.append(todo.id)
            await session.commit()
        if todo_ids:
            await todo_notify.dispatch(ctx, tenant_id=tenant_id, ids=todo_ids)
            created += len(todo_ids)
    return created
