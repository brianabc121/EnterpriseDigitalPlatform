"""订单中心的查询（设计文档 §25.9）：视图（全部、待审核、处理中、应收、修改过的）、筛选、
详情、数量。"""

import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.dates import today
from app.core.permissions import Permission
from app.modules.conversation.models import Message
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.orders import service
from app.modules.orders.models import (
    EDITABLE,
    IN_PROGRESS,
    Order,
    OrderEvent,
    OrderItem,
    OrderPayment,
    OrderRevision,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.modules.orders.schemas import (
    EvidenceOut,
    LinkedTodo,
    OrderAllowed,
    OrderCounts,
    OrderDetail,
    OrderEventOut,
    OrderItemOut,
    OrderOut,
    OrderPage,
    OrderPaymentOut,
    OrderRevisionOut,
    View,
)
from app.modules.orders.settings import OrderSettings
from app.modules.products import stock
from app.modules.routing.models import SkillGroup
from app.modules.todos import sla
from app.modules.todos.models import Todo, TodoType

EVIDENCE_LIMIT = 20
RECEIVABLE = (*IN_PROGRESS, OrderStatus.COMPLETED)
UNPAID = (PaymentStatus.UNPAID, PaymentStatus.DEPOSIT, PaymentStatus.PARTIAL)


def _receivable() -> ColumnElement[bool]:
    return and_(Order.status.in_(RECEIVABLE), Order.payment_status.in_(UNPAID), Order.total > 0)


def awaiting_shipment() -> ColumnElement[bool]:
    """待发货：工人加工完成、还没有发货（没有发货环节的是还没有完成）的订单。"""
    return and_(Order.status == OrderStatus.FULFILLING, Order.processed_at.is_not(None))


def out_of_stock() -> ColumnElement[bool]:
    """缺货：有商品缺货、还没有发货或取消的订单。"""
    return and_(
        Order.shortage_at.is_not(None),
        Order.status.in_((OrderStatus.CONFIRMED, OrderStatus.FULFILLING)),
    )


def _view(view: View) -> ColumnElement[bool] | None:
    match view:
        case "pending_review":
            return Order.status == OrderStatus.PENDING_REVIEW
        case "processing":
            return Order.status.in_(IN_PROGRESS)
        case "awaiting_shipment":
            return awaiting_shipment()
        case "out_of_stock":
            return out_of_stock()
        case "receivable":
            return _receivable()
        case "modified":
            return Order.modified.is_(True)
    return None


def conditions(
    principal: Principal,
    *,
    view: View = "all",
    status: str | None = None,
    source: str | None = None,
    assignee_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    q: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    min_total: Decimal | None = None,
    max_total: Decimal | None = None,
) -> ColumnElement[bool]:
    """订单中心的筛选条件（数据范围内），列表和导出共用。"""
    conditions: list[ColumnElement[bool]] = [service.visible_to(principal)]
    by_view = _view(view)
    if by_view is not None:
        conditions.append(by_view)
    if status:
        conditions.append(Order.status == status)
    if source:
        conditions.append(Order.source == source)
    if assignee_id:
        conditions.append(Order.assignee_id == assignee_id)
    if customer_id:
        conditions.append(Order.customer_id == customer_id)
    if session_id:
        conditions.append(Order.session_id == session_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        conditions.append(
            or_(
                Order.no.ilike(like),
                Order.external_no.ilike(like),
                Order.customer_id.in_(select(Customer.id).where(Customer.display_name.ilike(like))),
            )
        )
    if created_from:
        conditions.append(Order.created_at >= created_from)
    if created_to:
        conditions.append(Order.created_at < created_to)
    if min_total is not None:
        conditions.append(Order.total >= min_total)
    if max_total is not None:
        conditions.append(Order.total <= max_total)
    return and_(*conditions)


async def list_orders(
    session: AsyncSession,
    principal: Principal,
    *,
    view: View = "all",
    status: str | None = None,
    source: str | None = None,
    assignee_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    q: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    min_total: Decimal | None = None,
    max_total: Decimal | None = None,
    limit: int = 20,
    offset: int = 0,
) -> OrderPage:
    where = conditions(
        principal,
        view=view,
        status=status,
        source=source,
        assignee_id=assignee_id,
        customer_id=customer_id,
        session_id=session_id,
        q=q,
        created_from=created_from,
        created_to=created_to,
        min_total=min_total,
        max_total=max_total,
    )
    total = int(await session.scalar(select(func.count()).select_from(Order).where(where)) or 0)
    ordering: list[Any] = [Order.created_at.desc()]
    if view == "receivable":
        ordering = [Order.credit_due_date.asc().nulls_last(), Order.created_at]
    elif view == "awaiting_shipment":
        ordering = [Order.processed_at, Order.created_at]
    elif view == "out_of_stock":
        ordering = [Order.shortage_at, Order.created_at]
    rows = (
        await session.scalars(
            select(Order).where(where).order_by(*ordering).limit(limit).offset(offset)
        )
    ).all()
    return OrderPage(items=await outs(session, rows), total=total)


async def names(
    session: AsyncSession, model: Any, column: Any, ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.execute(select(model.id, column).where(model.id.in_(wanted)))
    return {row_id: name for row_id, name in rows}


async def _tenant_today(session: AsyncSession) -> date:
    return today(sla.tz_of(await sla.business_hours(session)))


def _overdue(order: Order, day: date) -> bool:
    return (
        order.payment_method == PaymentMethod.CREDIT
        and order.credit_due_date is not None
        and order.credit_due_date < day
        and order.status in RECEIVABLE
        and order.payment_status in UNPAID
    )


async def outs(session: AsyncSession, orders: Iterable[Order]) -> list[OrderOut]:
    rows = list(orders)
    for row in rows:
        # 提交后由数据库生成的值（updated_at）已过期，先重新读取。
        if sa_inspect(row).expired_attributes:
            await session.refresh(row)
    items: dict[uuid.UUID, list[OrderItem]] = defaultdict(list)
    if rows:
        for item in await session.scalars(
            select(OrderItem)
            .where(OrderItem.order_id.in_([r.id for r in rows]))
            .order_by(OrderItem.sort)
        ):
            items[item.order_id].append(item)
    customers = await names(session, Customer, Customer.display_name, (r.customer_id for r in rows))
    staff = await names(
        session,
        Staff,
        Staff.display_name,
        [*(r.assignee_id for r in rows), *(r.worker_id for r in rows)],
    )
    groups = await names(session, SkillGroup, SkillGroup.name, (r.skill_group_id for r in rows))
    day = await _tenant_today(session)
    return [_out(r, items[r.id], customers, staff, groups, day) for r in rows]


def _out(
    order: Order,
    items: list[OrderItem],
    customers: dict[uuid.UUID, str],
    staff: dict[uuid.UUID, str],
    groups: dict[uuid.UUID, str],
    day: date,
) -> OrderOut:
    return OrderOut(
        id=order.id,
        no=order.no,
        status=order.status,
        source=order.source,
        customer_id=order.customer_id,
        customer_name=customers.get(order.customer_id) if order.customer_id else None,
        session_id=order.session_id,
        assignee_id=order.assignee_id,
        assignee_name=staff.get(order.assignee_id) if order.assignee_id else None,
        skill_group_id=order.skill_group_id,
        skill_group_name=groups.get(order.skill_group_id) if order.skill_group_id else None,
        summary=service.summary(items),
        item_count=len(items),
        total=order.total,
        paid_amount=order.paid_amount,
        refunded_amount=order.refunded_amount,
        outstanding=service.outstanding(order),
        payment_method=order.payment_method,
        payment_status=order.payment_status,
        price_pending=order.price_pending,
        modified=order.modified,
        ai_error=order.ai_error,
        credit_due_date=order.credit_due_date,
        receivable_overdue=_overdue(order, day),
        version=order.version,
        created_at=order.created_at,
        updated_at=order.updated_at,
        confirmed_at=order.confirmed_at,
        worker_id=order.worker_id,
        worker_name=staff.get(order.worker_id) if order.worker_id else None,
        processed_at=order.processed_at,
        shortage=order.shortage_at is not None,
    )


def allowed(principal: Principal, order: Order) -> OrderAllowed:
    review = principal.has(Permission.ORDER_REVIEW)
    creator = order.created_by == principal.staff_id
    draft = order.status == OrderStatus.DRAFT
    edit = review or (draft and creator and principal.has(Permission.ORDER_CREATE))
    return OrderAllowed(
        edit=edit and order.status in EDITABLE,
        price=principal.has(Permission.ORDER_PRICE),
        submit=draft and edit,
        confirm=review and order.status == OrderStatus.PENDING_REVIEW,
        start=review and order.status == OrderStatus.CONFIRMED,
        ship=review and order.status == OrderStatus.FULFILLING,
        complete=review and order.status in (OrderStatus.FULFILLING, OrderStatus.SHIPPED),
        cancel=(review or (draft and edit))
        and order.status
        in (
            OrderStatus.DRAFT,
            OrderStatus.PENDING_REVIEW,
            OrderStatus.CONFIRMED,
            OrderStatus.FULFILLING,
        ),
        payment=principal.has(Permission.ORDER_PAYMENT) and order.status != OrderStatus.DRAFT,
        reveal=principal.has(Permission.CUSTOMER_VIEW_SENSITIVE) and bool(order.receiver),
        assign=review and order.status not in (OrderStatus.COMPLETED, OrderStatus.CANCELLED),
        assign_worker=principal.has(Permission.PRODUCTION_ASSIGN)
        and order.status in (OrderStatus.CONFIRMED, OrderStatus.FULFILLING)
        and order.processed_at is None,
        restock=review
        and order.shortage_at is not None
        and order.status in (OrderStatus.CONFIRMED, OrderStatus.FULFILLING),
    )


def revision_out(row: OrderRevision, staff: dict[uuid.UUID, str]) -> OrderRevisionOut:
    return OrderRevisionOut(
        version=row.version,
        kind=row.kind,
        actor_type=row.actor_type,
        actor_name=staff.get(row.actor_id) if row.actor_id else None,
        reason=row.reason,
        note=row.note,
        changes=row.changes,
        created_at=row.created_at,
    )


async def detail(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order: Order,
    settings: OrderSettings,
) -> OrderDetail:
    [base] = await outs(session, [order])
    items = await service.load_items(session, order.id)
    lines = await stock.line_stock(session, [order], items)
    payments = (
        await session.scalars(
            select(OrderPayment)
            .where(OrderPayment.order_id == order.id)
            .order_by(OrderPayment.created_at, OrderPayment.id)
        )
    ).all()
    events = (
        await session.scalars(
            select(OrderEvent)
            .where(OrderEvent.order_id == order.id)
            .order_by(OrderEvent.created_at, OrderEvent.id)
        )
    ).all()
    revisions = (await session.scalars(service.revisions_query(order.id))).all()
    todos = (
        await session.scalars(
            select(Todo).where(Todo.order_id == order.id).order_by(Todo.created_at)
        )
    ).all()
    types = await names(session, TodoType, TodoType.name, (t.type_id for t in todos))
    staff = await names(
        session,
        Staff,
        Staff.display_name,
        [
            order.created_by,
            order.credit_approved_by,
            *(p.recorded_by for p in payments),
            *(p.voided_by for p in payments),
            *(e.actor_id for e in events),
            *(r.actor_id for r in revisions),
            *(t.assignee_id for t in todos),
            *(i.done_by for i in items),
            order.processed_by,
        ],
    )
    evidence: list[EvidenceOut] = []
    ids = list(order.evidence_message_ids or [])[-EVIDENCE_LIMIT:]
    if order.confirm_message_id is not None and order.confirm_message_id not in ids:
        ids.append(order.confirm_message_id)
    if ids:
        messages = await session.scalars(
            select(Message).where(Message.id.in_(ids)).order_by(Message.sent_at, Message.id)
        )
        evidence = [
            EvidenceOut(
                id=m.id,
                sender_type=m.sender_type,
                text=m.text_plain or f"[{m.content_type}]",
                sent_at=m.sent_at,
            )
            for m in messages
        ]
    cost = principal.has(Permission.PRODUCT_VIEW_COST)
    cost_amount: Decimal | None = None
    if cost and all(i.cost_price is not None for i in items):
        cost_amount = sum(
            (i.cost_price * i.quantity for i in items if i.cost_price is not None), Decimal("0")
        )
    return OrderDetail(
        **base.model_dump(),
        items=[
            OrderItemOut(
                id=i.id,
                product_id=i.product_id,
                matched=i.product_id is not None,
                code=i.code,
                name=i.name,
                model=i.model,
                spec=i.spec,
                image_url=i.image_url,
                raw_text=i.raw_text,
                quantity=i.quantity,
                list_price=i.list_price,
                unit_price=i.unit_price,
                amount=i.amount,
                cost_price=i.cost_price if cost else None,
                work_status=i.work_status,
                done_at=i.done_at,
                done_by_name=staff.get(i.done_by) if i.done_by else None,
                shortage_qty=i.shortage_qty,
                shortage_note=i.shortage_note,
                restock_date=i.restock_date,
                stock_available=lines.get(i.id, stock.LineStock()).available,
                stock_short=lines.get(i.id, stock.LineStock()).short,
            )
            for i in items
        ],
        payments=[
            OrderPaymentOut(
                id=p.id,
                kind=p.kind,
                amount=p.amount,
                channel=p.channel,
                paid_at=p.paid_at,
                reference_no=p.reference_no,
                proof_url=p.proof_url,
                note=p.note,
                recorded_by_name=staff.get(p.recorded_by) if p.recorded_by else None,
                voided_at=p.voided_at,
                voided_by_name=staff.get(p.voided_by) if p.voided_by else None,
                void_reason=p.void_reason,
                created_at=p.created_at,
            )
            for p in payments
        ],
        items_amount=order.items_amount,
        discount=order.discount,
        deposit_amount=order.deposit_amount,
        credit_approved_by_name=(
            staff.get(order.credit_approved_by) if order.credit_approved_by else None
        ),
        payment_hint=order.payment_hint,
        receiver=service.masked_receiver(order.receiver),
        missing=service.missing_required(order, settings),
        expected_at=order.expected_at,
        shipping_company=order.shipping_company,
        tracking_no=order.tracking_no,
        submitted_at=order.submitted_at,
        started_at=order.started_at,
        shipped_at=order.shipped_at,
        completed_at=order.completed_at,
        cancelled_at=order.cancelled_at,
        cancel_reason=order.cancel_reason,
        customer_note=order.customer_note,
        internal_note=order.internal_note,
        external_no=order.external_no,
        created_by_type=order.created_by_type,
        created_by_name=staff.get(order.created_by) if order.created_by else None,
        evidence=evidence,
        events=[
            OrderEventOut(
                id=e.id,
                type=e.type,
                actor_type=e.actor_type,
                actor_name=staff.get(e.actor_id) if e.actor_id else None,
                payload=e.payload,
                public=e.public,
                created_at=e.created_at,
            )
            for e in events
        ],
        revisions=[revision_out(r, staff) for r in revisions],
        todos=[
            LinkedTodo(
                id=t.id,
                no=t.no,
                type_name=types.get(t.type_id, ""),
                title=t.title,
                status=t.status,
                assignee_name=staff.get(t.assignee_id) if t.assignee_id else None,
            )
            for t in todos
        ],
        tracking_url=service.tracking_url(ctx.settings, order.tracking_token),
        tracking_active=service.tracking_active(order),
        allowed=allowed(principal, order),
        cost_amount=cost_amount,
        claimed_at=order.claimed_at,
        processed_by_name=staff.get(order.processed_by) if order.processed_by else None,
    )


async def counts(session: AsyncSession, principal: Principal) -> OrderCounts:
    visible = service.visible_to(principal)
    day = await _tenant_today(session)

    async def count(condition: ColumnElement[bool]) -> int:
        return int(
            await session.scalar(select(func.count()).select_from(Order).where(visible, condition))
            or 0
        )

    return OrderCounts(
        pending_review=await count(Order.status == OrderStatus.PENDING_REVIEW),
        processing=await count(Order.status.in_(IN_PROGRESS)),
        receivable=await count(_receivable()),
        receivable_overdue=await count(
            and_(
                _receivable(),
                Order.payment_method == PaymentMethod.CREDIT,
                Order.credit_due_date < day,
            )
        ),
        awaiting_shipment=await count(awaiting_shipment()),
        out_of_stock=await count(out_of_stock()),
    )
