"""企业系统通过开放接口创建订单、回传状态、物流和收款（设计文档 §25.8）。

- 对接后，确认之后的状态以企业系统回传的为准（2026-09-30 确认）：可以跳过中间状态，不能回退；
  已完成、已取消的订单只能登记收款和退款。平台负责展示和通知客户。
- 企业系统的订单号（external_no）和平台订单号都可以用来查询和回传；按 external_no 重复创建、
  按流水号重复登记收款时不会重复记录，企业系统可以放心重试。
- 每次变化都记版本和动态，操作人为"企业系统"（actor_type=api，actor_id 是接口密钥）。
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.dates import today
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.ids import new_id
from app.modules.audit.service import record_audit
from app.modules.customer import sensitive
from app.modules.customer.models import Customer
from app.modules.integration import outbox as webhook_outbox
from app.modules.integration.schemas import OpenOrderCreate, OpenPaymentIn, OpenStatusUpdate
from app.modules.orders import service
from app.modules.orders import settings as order_settings
from app.modules.orders.actions import _after, _notice, _values, render
from app.modules.orders.models import (
    Order,
    OrderItem,
    OrderPayment,
    OrderStatus,
    PaymentKind,
    PaymentMethod,
    RevisionKind,
)
from app.modules.orders.schemas import OrderNotice
from app.modules.orders.settings import OrderSettings
from app.modules.products.models import Product
from app.modules.todos import sla
from app.modules.todos.models import ActorType

API = ActorType.API.value
FLOW = (
    OrderStatus.PENDING_REVIEW,
    OrderStatus.CONFIRMED,
    OrderStatus.FULFILLING,
    OrderStatus.SHIPPED,
    OrderStatus.COMPLETED,
)
# 回传的状态 → 订单动态。
EVENT_OF = {
    OrderStatus.CONFIRMED: "confirmed",
    OrderStatus.FULFILLING: "started",
    OrderStatus.SHIPPED: "shipped",
    OrderStatus.COMPLETED: "completed",
    OrderStatus.CANCELLED: "cancelled",
}


@dataclass(frozen=True)
class ApiActor:
    tenant_id: uuid.UUID
    key_id: uuid.UUID


async def _customer(
    ctx: AppContext, session: AsyncSession, actor: ApiActor, payload: OpenOrderCreate
) -> uuid.UUID:
    if payload.customer_id is not None:
        if await session.get(Customer, payload.customer_id) is None:
            raise NotFound("客户不存在")
        return payload.customer_id
    if payload.customer is None:
        raise Unprocessable("请提供 customer_id，或者客户的名称和手机号（customer）")
    phone = sensitive.normalize_phone(payload.customer.phone or "")
    if phone:
        if not sensitive.valid_phone(phone):
            raise Unprocessable("客户手机号的格式不正确")
        phone_hash, _ = await sensitive.search_indexes(ctx.keys, actor.tenant_id, phone)
        found = await session.scalar(
            select(Customer.id).where(Customer.phone_hash == phone_hash).limit(1)
        )
        if found is not None:
            return found
    customer = Customer(
        id=new_id(),
        tenant_id=actor.tenant_id,
        display_name=payload.customer.name.strip(),
        source_channel="api",
    )
    await sensitive.set_phone(ctx.keys, customer, phone or None)
    session.add(customer)
    await session.flush()
    record_audit(
        session,
        action="customer.create",
        actor_type=API,
        actor_id=actor.key_id,
        tenant_id=actor.tenant_id,
        resource_type="customer",
        resource_id=str(customer.id),
    )
    return customer.id


async def _lines(session: AsyncSession, payload: OpenOrderCreate) -> list[service.Line]:
    codes = [line.code.strip() for line in payload.items if line.code and line.code.strip()]
    products = {
        p.code: p.id for p in await session.scalars(select(Product).where(Product.code.in_(codes)))
    }
    lines: list[service.Line] = []
    for line in payload.items:
        code = (line.code or "").strip()
        if code:
            product_id = products.get(code)
            if product_id is None:
                raise Unprocessable(f"商品库里没有代码为 {code} 的商品")
            lines.append(
                service.Line(
                    quantity=line.quantity, product_id=product_id, unit_price=line.unit_price
                )
            )
        elif line.name and line.name.strip():
            text = line.name.strip()
            lines.append(
                service.Line(
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    raw_text=text,
                    name=text[:128],
                )
            )
        else:
            raise Unprocessable("每个商品行需要商品代码（code）或商品说明（name）")
    return lines


async def _payment_terms(
    session: AsyncSession,
    order: Order,
    settings: OrderSettings,
    *,
    method: str | None,
    deposit: Decimal | None,
    due: date | None,
) -> None:
    """确认时的收款方式（企业系统已经确认过，平台只检查信息是否完整）。"""
    if method is None:
        raise Unprocessable("确认订单需要收款方式（payment_method）")
    if method not in settings.payment_methods:
        raise Unprocessable("这个收款方式没有启用")
    if method == PaymentMethod.DEPOSIT.value:
        if deposit is None:
            raise Unprocessable("预付定金需要填写定金金额（deposit_amount）")
        if deposit > order.total:
            raise Unprocessable("定金不能超过订单金额")
    if method == PaymentMethod.CREDIT.value:
        if due is None:
            raise Unprocessable("暂欠需要约定付款日期（credit_due_date）")
        if due < today(sla.tz_of(await sla.business_hours(session))):
            raise Unprocessable("约定付款日期不能早于今天")


def _ready(items: list[OrderItem], order: Order) -> None:
    if any(i.product_id is None for i in items) or order.price_pending:
        raise Unprocessable("还有没对应到商品库或待定价的商品，不能确认；请先在平台上修改订单")


async def create(
    ctx: AppContext, session: AsyncSession, actor: ApiActor, payload: OpenOrderCreate
) -> tuple[Order, bool, OrderNotice | None]:
    """企业系统创建订单：进入平台审核（生成"订单审核"待办），或作为已确认的订单记录。
    返回（订单，是否新建，通知结果）；同一个企业系统订单号重复创建时返回已有的订单。"""
    external = (payload.external_no or "").strip() or None
    if external is not None:
        existing = await session.scalar(select(Order).where(Order.external_no == external))
        if existing is not None:
            return existing, False, None
    try:
        return await _create(ctx, session, actor, payload, external)
    except IntegrityError:
        # 同一个单号的两个请求同时到达：后写入的一个返回先建好的订单。
        await session.rollback()
        if external is None:
            raise
        existing = await session.scalar(select(Order).where(Order.external_no == external))
        if existing is None:
            raise
        return existing, False, None


async def _create(
    ctx: AppContext,
    session: AsyncSession,
    actor: ApiActor,
    payload: OpenOrderCreate,
    external: str | None,
) -> tuple[Order, bool, OrderNotice | None]:
    customer_id = await _customer(ctx, session, actor, payload)
    settings = await order_settings.load(session, actor.tenant_id)
    now = service.utcnow()
    order = Order(
        id=new_id(),
        tenant_id=actor.tenant_id,
        no=await service.next_no(session, actor.tenant_id, settings, now),
        status=OrderStatus.DRAFT,
        source="api",
        customer_id=customer_id,
        discount=Decimal("0"),
        expected_at=payload.expected_at,
        customer_note=payload.customer_note.strip(),
        internal_note=payload.internal_note.strip(),
        tracking_token=service.new_token(),
        external_no=external,
        created_by_type=API,
        created_by=actor.key_id,
        created_at=now,
        version=1,
    )
    order.receiver, _ = await service.merge_receiver(
        ctx.keys, actor.tenant_id, {}, payload.receiver.model_dump()
    )
    items = await service.build_items(session, order, await _lines(session, payload), settings)
    order.discount = payload.discount
    service.recompute(order, items, [])
    session.add(order)
    await session.flush()
    session.add_all(items)
    service.event(
        session,
        order,
        "created",
        actor_type=API,
        actor_id=actor.key_id,
        payload={"source": "api", "external_no": external},
    )
    todos: list[uuid.UUID] = []
    notice: OrderNotice | None = None
    room: uuid.UUID | None = None
    if payload.status == OrderStatus.PENDING_REVIEW.value:
        missing = service.missing_required(order, settings)
        if missing:
            raise Unprocessable(f"提交前请补全：{'、'.join(missing)}")
        order.status = OrderStatus.PENDING_REVIEW
        order.submitted_at = now
        todo = await service.open_review_todo(
            session, ctx.keys, order, items, actor_type=ActorType.API, actor_id=None, now=now
        )
        todos.append(todo.id)
        service.event(
            session,
            order,
            "submitted",
            actor_type=API,
            actor_id=actor.key_id,
            payload={"todo_id": str(todo.id)},
            public=True,
        )
    else:
        _ready(items, order)
        await _payment_terms(
            session,
            order,
            settings,
            method=payload.payment_method,
            deposit=payload.deposit_amount,
            due=payload.credit_due_date,
        )
        credit = payload.payment_method == PaymentMethod.CREDIT.value
        order.status = OrderStatus.CONFIRMED
        order.payment_method = payload.payment_method
        order.deposit_amount = (
            payload.deposit_amount
            if payload.payment_method == PaymentMethod.DEPOSIT.value
            else None
        )
        order.credit_due_date = payload.credit_due_date if credit else None
        order.credit_approved_at = now if credit else None
        order.submitted_at = order.confirmed_at = now
        webhook_outbox.order_event(session, order, "api_created", actor_type=API)
        service.event(
            session,
            order,
            "confirmed",
            actor_type=API,
            actor_id=actor.key_id,
            payload={"payment_method": payload.payment_method, "by": "api"},
            public=True,
        )
        if payload.notify_customer:
            text = render(settings.confirm_template, _values(ctx, order, items))
            notice, room = await _notice(
                session, order, text, actor_id=actor.key_id, now=now, actor_type=API
            )
    service.add_revision(
        session,
        order,
        kind=RevisionKind.CREATED,
        actor_type=API,
        actor_id=actor.key_id,
        before=None,
        after=service.snapshot(order, items, []),
    )
    await session.commit()
    await _after(ctx, order, [room], todos)
    return order, True, notice


async def _add_payment(
    session: AsyncSession, actor: ApiActor, order: Order, payload: OpenPaymentIn, now: datetime
) -> bool:
    """登记一笔收款或退款；同一个流水号已经登记过时跳过。返回是否登记了。"""
    reference = (payload.reference_no or "").strip() or None
    if reference is not None:
        duplicate = await session.scalar(
            select(OrderPayment.id).where(
                OrderPayment.order_id == order.id,
                OrderPayment.reference_no == reference,
                OrderPayment.kind == payload.kind,
                OrderPayment.voided_at.is_(None),
            )
        )
        if duplicate is not None:
            return False
    refund = payload.kind == PaymentKind.REFUND.value
    if order.status == OrderStatus.CANCELLED and not refund:
        raise Unprocessable("已取消的订单不能登记收款")
    paid_at = payload.paid_at or now
    if paid_at > now + timedelta(minutes=5):
        raise Unprocessable("收款时间不能晚于现在")
    net = order.paid_amount - order.refunded_amount
    if refund and payload.amount > net:
        raise Unprocessable("退款金额不能超过已收金额")
    if not refund and not order.price_pending and payload.amount > service.outstanding(order):
        raise Unprocessable(
            f"收款金额超过未收金额（{service.text_money(service.outstanding(order))} 元）"
        )
    items = await service.load_items(session, order.id)
    payments = await service.load_payments(session, order.id)
    before = service.snapshot(order, items, payments)
    payment = OrderPayment(
        id=new_id(),
        tenant_id=order.tenant_id,
        order_id=order.id,
        kind=payload.kind,
        amount=payload.amount,
        channel=payload.channel,
        paid_at=paid_at,
        reference_no=reference,
        note=(payload.note or "").strip() or None,
        recorded_by_type=API,
        recorded_by=actor.key_id,
        created_at=now,
    )
    session.add(payment)
    payments.append(payment)
    service.recompute(order, items, payments)
    service.add_revision(
        session,
        order,
        kind=RevisionKind.PAYMENT,
        actor_type=API,
        actor_id=actor.key_id,
        before=before,
        after=service.snapshot(order, items, payments),
    )
    service.event(
        session,
        order,
        "refunded" if refund else "paid",
        actor_type=API,
        actor_id=actor.key_id,
        payload={"amount": service.text_money(payload.amount), "channel": payload.channel},
        public=True,
    )
    if service.outstanding(order) == 0 and order.total > 0:
        await service.close_todo(
            session,
            order.collection_todo_id,
            result="款项已收清（企业系统回传）",
            cancelled=False,
            actor_type=API,
            actor_id=None,
            now=now,
        )
    return True


def _check_target(order: Order, target: OrderStatus) -> None:
    if order.status in (OrderStatus.COMPLETED, OrderStatus.CANCELLED):
        raise Conflict("订单已经完成或取消，不能再改状态")
    if target != OrderStatus.CANCELLED and FLOW.index(target) < FLOW.index(
        OrderStatus(order.status)
    ):
        raise Conflict("订单状态不能回退")


async def update_status(
    ctx: AppContext, session: AsyncSession, actor: ApiActor, ref: str, payload: OpenStatusUpdate
) -> tuple[Order, OrderNotice | None]:
    """企业系统回传状态、物流、收款和它自己的订单号。"""
    order = await session.scalar(
        select(Order)
        .where((Order.no == ref) | (Order.external_no == ref))
        .limit(1)
        .with_for_update()
    )
    if order is None:
        raise NotFound("订单不存在")
    if order.status == OrderStatus.DRAFT:
        raise Conflict("订单还在采集中，没有提交，不能回传")
    settings = await order_settings.load(session, actor.tenant_id)
    now = service.utcnow()

    external = (payload.external_no or "").strip() or None
    if external is not None and external != order.external_no:
        taken = await session.scalar(
            select(Order.id).where(Order.external_no == external, Order.id != order.id)
        )
        if taken is not None:
            raise Conflict("这个企业系统订单号已经对应了另一个订单")
        order.external_no = external
        service.event(
            session,
            order,
            "synced",
            actor_type=API,
            actor_id=actor.key_id,
            payload={"external_no": external},
        )

    for payment in payload.payments:
        await _add_payment(session, actor, order, payment, now)

    notice: OrderNotice | None = None
    room: uuid.UUID | None = None
    company = (payload.shipping_company or "").strip() or None
    number = (payload.tracking_no or "").strip() or None
    target = OrderStatus(payload.status) if payload.status else None
    if target is not None and target != order.status:
        _check_target(order, target)
        items = await service.load_items(session, order.id)
        payments = await service.load_payments(session, order.id)
        before = service.snapshot(order, items, payments)
        confirming = order.status == OrderStatus.PENDING_REVIEW and target != OrderStatus.CANCELLED
        method = payload.payment_method or order.payment_method
        if confirming:
            _ready(items, order)
            await _payment_terms(
                session,
                order,
                settings,
                method=method,
                deposit=payload.deposit_amount or order.deposit_amount,
                due=payload.credit_due_date or order.credit_due_date,
            )
            credit = method == PaymentMethod.CREDIT
            order.payment_method = method
            if method == PaymentMethod.DEPOSIT:
                order.deposit_amount = payload.deposit_amount or order.deposit_amount
            if credit:
                order.credit_due_date = payload.credit_due_date or order.credit_due_date
                order.credit_approved_at = order.credit_approved_at or now
            order.confirmed_at = now
        if target in (OrderStatus.FULFILLING, OrderStatus.SHIPPED, OrderStatus.COMPLETED):
            order.started_at = order.started_at or now
        if target == OrderStatus.SHIPPED:
            order.shipped_at = now
        if target == OrderStatus.COMPLETED:
            order.completed_at = now
            service.expire_tracking(order, settings, now)
        reason = (payload.cancel_reason or "").strip() or "企业系统已取消订单"
        if target == OrderStatus.CANCELLED:
            order.cancelled_at, order.cancel_reason = now, reason
            service.expire_tracking(order, settings, now)
        if company or number:
            order.shipping_company = company or order.shipping_company
            order.tracking_no = number or order.tracking_no
        order.status = target
        service.recompute(order, items, payments)
        service.add_revision(
            session,
            order,
            kind=RevisionKind.STATUS,
            actor_type=API,
            actor_id=actor.key_id,
            before=before,
            after=service.snapshot(order, items, payments),
        )
        if confirming and target != OrderStatus.CONFIRMED:
            service.event(
                session,
                order,
                "confirmed",
                actor_type=API,
                actor_id=actor.key_id,
                payload={"payment_method": method, "by": "api"},
                public=True,
            )
        detail: dict[str, object] = {"by": "api"}
        if target == OrderStatus.CONFIRMED:
            detail["payment_method"] = method
        if target == OrderStatus.SHIPPED:
            detail.update(shipping_company=order.shipping_company, tracking_no=order.tracking_no)
        if target == OrderStatus.CANCELLED:
            detail["reason"] = reason
        service.event(
            session,
            order,
            EVENT_OF[target],
            actor_type=API,
            actor_id=actor.key_id,
            payload=detail,
            public=True,
        )
        if confirming or target == OrderStatus.CANCELLED:
            await service.close_todo(
                session,
                order.review_todo_id,
                result="企业系统已取消订单"
                if target == OrderStatus.CANCELLED
                else "企业系统已确认订单",
                cancelled=target == OrderStatus.CANCELLED,
                actor_type=API,
                actor_id=None,
                now=now,
            )
        if target == OrderStatus.CANCELLED:
            await service.close_todo(
                session,
                order.collection_todo_id,
                result="订单已取消",
                cancelled=True,
                actor_type=API,
                actor_id=None,
                now=now,
            )
        if target in (OrderStatus.SHIPPED, OrderStatus.COMPLETED, OrderStatus.CANCELLED):
            await service.close_production_todos(
                session,
                order,
                result={
                    OrderStatus.SHIPPED: "企业系统已发货",
                    OrderStatus.COMPLETED: "企业系统已完成订单",
                }.get(target, "订单已取消"),
                cancelled=target == OrderStatus.CANCELLED,
                actor_type=API,
                actor_id=None,
                now=now,
            )
        template = {
            OrderStatus.CONFIRMED: settings.confirm_template,
            OrderStatus.SHIPPED: settings.ship_template,
            OrderStatus.COMPLETED: settings.complete_template,
            OrderStatus.CANCELLED: settings.cancel_template,
        }.get(target) or (settings.confirm_template if confirming else None)
        if payload.notify_customer and template:
            text = render(
                template,
                _values(
                    ctx,
                    order,
                    items,
                    company=order.shipping_company or "",
                    tracking_no=order.tracking_no or "",
                    reason=reason,
                ),
            )
            notice, room = await _notice(
                session, order, text, actor_id=actor.key_id, now=now, actor_type=API
            )
    elif company or number:
        # 只更正物流信息（例如补上单号）：记版本，不改状态。
        items = await service.load_items(session, order.id)
        payments = await service.load_payments(session, order.id)
        before = service.snapshot(order, items, payments)
        order.shipping_company = company or order.shipping_company
        order.tracking_no = number or order.tracking_no
        service.add_revision(
            session,
            order,
            kind=RevisionKind.EDIT,
            actor_type=API,
            actor_id=actor.key_id,
            before=before,
            after=service.snapshot(order, items, payments),
        )
    await session.commit()
    await _after(ctx, order, [room], [])
    return order, notice
