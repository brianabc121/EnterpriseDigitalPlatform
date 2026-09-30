"""推送事件的发件箱（设计文档 §25.8）：订单、待办变化时在同一个事务里写入 webhook_events，
调度进程再按各推送地址订阅的事件生成推送记录并投递（见 delivery）。这样业务提交了，推送一定
会发出；业务回滚了，推送也不会发出。

订单只在进入正式流程（提交审核或直接确认）之后推送：AI 采集中的草稿不推送。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_id
from app.modules.integration.models import WebhookEvent, WebhookEventType

# 订单动态 → 推送事件。
ORDER_EVENTS: dict[str, WebhookEventType] = {
    "submitted": WebhookEventType.ORDER_CREATED,
    "api_created": WebhookEventType.ORDER_CREATED,
    "updated": WebhookEventType.ORDER_UPDATED,
    "confirmed": WebhookEventType.ORDER_CONFIRMED,
    "started": WebhookEventType.ORDER_STATUS_CHANGED,
    "shipped": WebhookEventType.ORDER_STATUS_CHANGED,
    "completed": WebhookEventType.ORDER_STATUS_CHANGED,
    "cancelled": WebhookEventType.ORDER_CANCELLED,
    "paid": WebhookEventType.ORDER_PAYMENT,
    "refunded": WebhookEventType.ORDER_PAYMENT,
    "payment_voided": WebhookEventType.ORDER_PAYMENT,
    # 加工（§25.11）：加工完成、商品缺货和到货都作为订单的更新推送（data.change 区分）。
    "processed": WebhookEventType.ORDER_UPDATED,
    "shortage": WebhookEventType.ORDER_UPDATED,
    "restocked": WebhookEventType.ORDER_UPDATED,
}


def emit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event: WebhookEventType,
    resource_type: str,
    resource_id: uuid.UUID,
    data: dict[str, Any] | None = None,
    actor_type: str | None = None,
) -> None:
    """写入一条推送事件（由调用方提交）。"""
    session.add(
        WebhookEvent(
            id=new_id(),
            tenant_id=tenant_id,
            event=event,
            resource_type=resource_type,
            resource_id=resource_id,
            data=data or {},
            actor_type=actor_type,
            created_at=datetime.now(UTC),
        )
    )


def order_event(
    session: AsyncSession,
    order: Any,
    type_: str,
    *,
    actor_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """订单动态对应的推送事件（orders.service.event 调用）。"""
    event = ORDER_EVENTS.get(type_)
    if event is None or (order.submitted_at is None and order.confirmed_at is None):
        return
    emit(
        session,
        tenant_id=order.tenant_id,
        event=event,
        resource_type="order",
        resource_id=order.id,
        data={"change": type_, "status": order.status, **(payload or {})},
        actor_type=actor_type,
    )


def todo_event(session: AsyncSession, todo: Any, type_: str, *, actor_type: str) -> None:
    """待办动态对应的推送事件（todos.events.record 调用）：目前只推送"待办完成"。订单流程里系统
    生成的待办（订单审核、催收）随订单推送，不单独推送。"""
    if type_ != "done" or (todo.source == "rule" and todo.order_id is not None):
        return
    emit(
        session,
        tenant_id=todo.tenant_id,
        event=WebhookEventType.TODO_DONE,
        resource_type="todo",
        resource_id=todo.id,
        data={"change": type_},
        actor_type=actor_type,
    )
