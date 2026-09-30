"""客户侧（设计文档 §25.6）：订单跟踪页（凭跟踪链接查看，不需要登录）和 Widget 的"我的订单"。

跟踪页只显示对客户可见的内容：进度、商品和金额、收款方式与收款状态、物流、掩码后的收货信息、
客户可见的动态；不显示内部备注、修改记录的细节、员工信息和成本价。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.deps import client_ip, get_context, get_rate_limiter
from app.core.errors import ERROR_RESPONSES, NotFound
from app.core.ratelimit import Limit, RateLimiter
from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.conversation.models import ChatSession, Room
from app.modules.customer.models import CustomerIdentity
from app.modules.orders import service
from app.modules.orders import settings as order_settings
from app.modules.orders.models import (
    PAYMENT_METHOD_LABELS,
    PAYMENT_STATUS_LABELS,
    STATUS_LABELS,
    Order,
    OrderEvent,
    OrderStatus,
)
from app.modules.orders.schemas import (
    OrderTracking,
    TrackingEvent,
    TrackingItem,
    TrackingStep,
    VisitorOrder,
    VisitorOrderList,
)
from app.modules.visitor.deps import VisitorContext

TRACK_PER_IP = Limit("order-track-ip", 60, 60)
TRACK_PER_TOKEN = Limit("order-track-token", 30, 60)
VISITOR_LIMIT = 20
EXPIRED = "订单链接已失效，请联系客服"

router = APIRouter(prefix="/api/v1/public", tags=["public"], responses=ERROR_RESPONSES)

# 客户可见的动态。
EVENT_TEXT: dict[str, str] = {
    "submitted": "订单已提交，等待客服确认",
    "confirmed": "订单已确认",
    "started": "开始处理",
    "shipped": "已发货",
    "completed": "订单已完成",
    "cancelled": "订单已取消",
    "updated": "订单内容已更新",
    "paid": "已登记收款",
    "refunded": "已登记退款",
}


def _event_text(event: OrderEvent) -> str:
    text = EVENT_TEXT.get(event.type, "订单有更新")
    payload = event.payload or {}
    if event.type == "shipped" and payload.get("shipping_company"):
        text += f"：{payload['shipping_company']} {payload.get('tracking_no') or ''}".rstrip()
    elif event.type in ("paid", "refunded") and payload.get("amount"):
        text += f" {payload['amount']} 元"
    elif event.type == "cancelled" and payload.get("reason"):
        text += f"：{payload['reason']}"
    return text


def _steps(order: Order, shipping: bool) -> list[TrackingStep]:
    status = order.status
    order_of = [
        OrderStatus.PENDING_REVIEW,
        OrderStatus.CONFIRMED,
        OrderStatus.FULFILLING,
        OrderStatus.SHIPPED,
        OrderStatus.COMPLETED,
    ]
    stages: list[tuple[str, str, datetime | None, OrderStatus]] = [
        ("submitted", "已提交", order.submitted_at, OrderStatus.PENDING_REVIEW),
        ("confirmed", "已确认", order.confirmed_at, OrderStatus.CONFIRMED),
        ("fulfilling", "处理中", order.started_at, OrderStatus.FULFILLING),
    ]
    if shipping or order.shipped_at is not None:
        stages.append(("shipped", "已发货", order.shipped_at, OrderStatus.SHIPPED))
    stages.append(("completed", "已完成", order.completed_at, OrderStatus.COMPLETED))
    reached = order_of.index(OrderStatus(status)) if status in order_of else -1
    return [
        TrackingStep(
            key=key,
            label=label,
            done=at is not None or (reached >= 0 and order_of.index(stage) <= reached),
            at=at,
        )
        for key, label, at, stage in stages
    ]


async def build(session: AsyncSession, order: Order) -> OrderTracking:
    settings = await order_settings.load(session, order.tenant_id)
    items = await service.load_items(session, order.id)
    events = (
        await session.scalars(
            select(OrderEvent)
            .where(OrderEvent.order_id == order.id, OrderEvent.public)
            .order_by(OrderEvent.created_at, OrderEvent.id)
        )
    ).all()
    return OrderTracking(
        no=order.no,
        status=order.status,
        status_label=STATUS_LABELS.get(order.status, order.status),
        steps=_steps(order, settings.shipping_enabled),
        items=[
            TrackingItem(
                name=i.name,
                spec=i.spec,
                image_url=i.image_url,
                quantity=i.quantity,
                unit_price=i.unit_price,
                amount=i.amount,
            )
            for i in items
        ],
        items_amount=order.items_amount,
        discount=order.discount,
        total=order.total,
        payment_method=PAYMENT_METHOD_LABELS.get(order.payment_method or ""),
        payment_status=PAYMENT_STATUS_LABELS.get(order.payment_status, order.payment_status),
        paid_amount=order.paid_amount - order.refunded_amount,
        outstanding=service.outstanding(order),
        shipping_company=order.shipping_company,
        tracking_no=order.tracking_no,
        receiver=service.masked_receiver(order.receiver),
        customer_note=order.customer_note,
        events=[
            TrackingEvent(type=e.type, text=_event_text(e), created_at=e.created_at) for e in events
        ],
        created_at=order.created_at,
    )


@router.get("/orders/{token}", response_model=OrderTracking)
async def track_order(
    token: str,
    request: Request,
    ctx: AppContext = Depends(get_context),  # noqa: B008  FastAPI 依赖
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> OrderTracking:
    """订单跟踪页的数据：凭跟踪链接里的令牌查看，不需要登录（按令牌和 IP 限流）。"""
    await limiter.check(TRACK_PER_IP, client_ip(request) or "unknown")
    await limiter.check(TRACK_PER_TOKEN, token[:64])
    if len(token) < 16 or len(token) > 64:
        raise NotFound(EXPIRED)
    async with ctx.db.platform_sessionmaker() as session:
        found = (
            await session.execute(
                select(Order.tenant_id, Order.id).where(Order.tracking_token == token)
            )
        ).first()
    if found is None:
        raise NotFound(EXPIRED)
    tenant_id, order_id = found
    async with ctx.db.tenant_session(tenant_id) as session:
        order = await session.get(Order, order_id)
        if (
            order is None
            or order.status == OrderStatus.DRAFT
            or not service.tracking_active(order, datetime.now(UTC))
        ):
            raise NotFound(EXPIRED)
        return await build(session, order)


# ---- Widget"我的订单" ----


def visitor_scope(customer_id: uuid.UUID, room_id: uuid.UUID | None) -> list[Any]:
    """访客能看到的订单：自己的、已提交的；匿名访客只含在当前访客身份的对话里提交的。"""
    conditions: list[Any] = [Order.customer_id == customer_id, Order.status != OrderStatus.DRAFT]
    if room_id is not None:
        conditions.append(
            Order.session_id.in_(select(ChatSession.id).where(ChatSession.room_id == room_id))
        )
    return conditions


async def anonymous_room(session: AsyncSession, identity_id: uuid.UUID) -> tuple[Room, bool]:
    room = await session.scalar(select(Room).where(Room.identity_id == identity_id))
    if room is None:
        raise NotFound("会话不存在")
    identity = await session.get(CustomerIdentity, room.identity_id)
    channel = await session.get(ChannelAccount, room.channel_account_id)
    anonymous = (
        channel is not None
        and channel.type == ChannelType.WEB
        and not (identity is not None and identity.verified)
    )
    return room, anonymous


async def visitor_orders(ctx: AppContext, visitor: VisitorContext) -> VisitorOrderList:
    session = visitor.session
    room, anonymous = await anonymous_room(session, visitor.claims.identity_id)
    if room.customer_id is None:
        return VisitorOrderList(items=[])
    rows = (
        await session.scalars(
            select(Order)
            .where(*visitor_scope(room.customer_id, room.id if anonymous else None))
            .order_by(Order.created_at.desc())
            .limit(VISITOR_LIMIT)
        )
    ).all()
    now = datetime.now(UTC)
    result: list[VisitorOrder] = []
    for order in rows:
        items = await service.load_items(session, order.id)
        result.append(
            VisitorOrder(
                no=order.no,
                status=order.status,
                status_label=STATUS_LABELS.get(order.status, order.status),
                summary=service.summary(items),
                total=order.total,
                tracking_url=(
                    service.tracking_url(ctx.settings, order.tracking_token)
                    if service.tracking_active(order, now)
                    else None
                ),
                created_at=order.created_at,
            )
        )
    return VisitorOrderList(items=result)
