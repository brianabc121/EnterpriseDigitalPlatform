"""订单的操作（设计文档 §25.4、§25.5）：新建、修改（改价、改商品留痕）、提交审核、确认、开始处理、
发货、完成、取消、登记和作废收款、查看收货信息、重新生成跟踪链接、转交、通知客户。

- 改价、改商品、改数量时必须选择原因；每次修改都生成一个版本，记录修改人、前后差异和原因，
  不需要客户再次确认（2026-09-30 确认），可以选择把最新内容告知客户。
- 收款方式决定开始处理和完成的条件：在线收款要先收清；预付定金要先收到定金、尾款在发货前或
  完成前收清；货到付款在完成前登记收款；暂欠需要有 order:credit 权限的员工同意。
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.dates import today
from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.ids import new_id
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.conversation import notices, outbox
from app.modules.conversation.models import ChatSession
from app.modules.customer.models import Customer
from app.modules.customer.service import visible_to as customer_visible_to
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.orders import service
from app.modules.orders import settings as order_settings
from app.modules.orders.models import (
    CLOSED,
    EDITABLE,
    Order,
    OrderPayment,
    OrderStatus,
    PaymentKind,
    PaymentMethod,
    RevisionKind,
    RevisionReason,
)
from app.modules.orders.schemas import (
    LineIn,
    NotifyFlag,
    OrderAssignRequest,
    OrderCancelRequest,
    OrderConfirmRequest,
    OrderCreate,
    OrderNotice,
    OrderUpdate,
    PaymentIn,
    ShipRequest,
    VoidRequest,
)
from app.modules.orders.settings import OrderSettings
from app.modules.products import stock
from app.modules.routing.models import SkillGroup
from app.modules.todos import notify as todo_notify
from app.modules.todos import sla
from app.modules.todos.models import UNFINISHED as TODO_UNFINISHED
from app.modules.todos.models import ActorType, NotifyReason, Todo
from app.modules.warehouse import documents

STAFF = "staff"


def render(template: str, values: dict[str, Any]) -> str:
    """通知模板：{no}、{summary}、{total} 等占位符；模板写错时原样发送。"""
    try:
        return template.format_map(defaultdict(str, values)).strip()
    except (ValueError, IndexError, AttributeError):
        return template.strip()


def lines_of(items: list[LineIn]) -> list[service.Line]:
    return [
        service.Line(
            quantity=i.quantity,
            product_id=i.product_id,
            unit_price=i.unit_price,
            raw_text=(i.raw_text or "").strip() or None,
            name=(i.name or "").strip() or None,
        )
        for i in items
    ]


async def _customer(session: AsyncSession, principal: Principal, customer_id: uuid.UUID) -> None:
    visible = await session.scalar(
        select(Customer.id).where(Customer.id == customer_id, customer_visible_to(principal))
    )
    if visible is None:
        raise NotFound("客户不存在或没有权限查看")


async def _staff(session: AsyncSession, staff_id: uuid.UUID) -> None:
    status = await session.scalar(select(Staff.status).where(Staff.id == staff_id))
    if status != StaffStatus.ACTIVE:
        raise Unprocessable("员工不存在或已停用")


def _prices_changed(before: list[Any], after: list[Any]) -> bool:
    """修改后是否有商品行的单价与原来（或新加商品的建议零售价）不同。"""
    old = {(i.product_id, i.raw_text): i.unit_price for i in before}
    for item in after:
        key = (item.product_id, item.raw_text)
        if key in old:
            if item.unit_price != old[key]:
                return True
        elif item.unit_price != item.list_price:
            return True
    return False


async def _notice(
    session: AsyncSession,
    order: Order,
    text: str,
    *,
    actor_id: uuid.UUID | None,
    now: datetime,
    actor_type: str | None = None,
) -> tuple[OrderNotice, uuid.UUID | None]:
    """按渠道通知客户，结果记入订单动态（由调用方提交并刷新发件箱）。"""
    sent = await notices.send(
        session, session_id=order.session_id, customer_id=order.customer_id, text=text, now=now
    )
    service.event(
        session,
        order,
        "customer_notified",
        actor_type=actor_type or (STAFF if actor_id else ActorType.SYSTEM),
        actor_id=actor_id,
        payload={
            "status": sent.status,
            "channel": sent.channel,
            "reason": sent.reason,
            "text": text,
        },
    )
    room = sent.room_id if sent.status == "sent" else None
    return OrderNotice(status=sent.status, channel=sent.channel, reason=sent.reason), room


def _values(ctx: AppContext, order: Order, items: list[Any], **extra: Any) -> dict[str, Any]:
    return {
        "no": order.no,
        "summary": service.summary(items),
        "total": service.text_money(order.total),
        "payment": service.payment_label(order),
        "link": service.tracking_url(ctx.settings, order.tracking_token),
        **extra,
    }


async def _after(
    ctx: AppContext, order: Order, rooms: list[uuid.UUID | None], todos: list[uuid.UUID]
) -> None:
    """提交之后：立即发送给客户的通知，提醒待办的处理人。"""
    targets = [r for r in rooms if r is not None]
    if targets:
        await outbox.flush_rooms(ctx, order.tenant_id, targets)
    if todos:
        await todo_notify.dispatch(ctx, tenant_id=order.tenant_id, ids=todos)


# ---- 新建与修改 ----


async def create(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: OrderCreate
) -> Order:
    """员工新建（包括 AI 预填后核对保存的）。提交审核时生成"订单审核"待办：自己能审核订单时交给
    自己（不提醒），否则按规则分派。"""
    await _customer(session, principal, payload.customer_id)
    if payload.session_id is not None:
        chat = await session.get(ChatSession, payload.session_id)
        if chat is None or chat.customer_id != payload.customer_id:
            raise Unprocessable("会话不属于这位客户")
    settings = await order_settings.load(session, principal.tenant_id)
    now = service.utcnow()
    me = principal.staff_id
    order = Order(
        id=new_id(),
        tenant_id=principal.tenant_id,
        no=await service.next_no(session, principal.tenant_id, settings, now),
        status=OrderStatus.DRAFT,
        source=payload.source,
        customer_id=payload.customer_id,
        session_id=payload.session_id,
        assignee_id=me,
        payment_method=payload.payment_method,
        discount=Decimal("0"),
        expected_at=payload.expected_at,
        customer_note=payload.customer_note.strip(),
        internal_note=payload.internal_note.strip(),
        evidence_message_ids=list(dict.fromkeys(payload.evidence_message_ids)),
        tracking_token=service.new_token(),
        created_by_type=STAFF,
        created_by=me,
        created_at=now,
        version=1,
    )
    order.receiver, _ = await service.merge_receiver(
        ctx.keys, principal.tenant_id, {}, payload.receiver.model_dump()
    )
    items = await service.build_items(session, order, lines_of(payload.items), settings)
    can_price = principal.has(Permission.ORDER_PRICE)
    if not can_price and (
        payload.discount > 0 or any(i.unit_price != i.list_price for i in items if i.product_id)
    ):
        raise Forbidden("改价和优惠需要 order:price 权限")
    order.discount = payload.discount
    service.recompute(order, items, [])
    service.check_discount(principal, items, order.total, settings)
    session.add(order)
    await session.flush()
    session.add_all(items)
    todos: list[uuid.UUID] = []
    if payload.submit:
        todos.append(await _submit(ctx, session, principal, order, items, settings, now))
    service.add_revision(
        session,
        order,
        kind=RevisionKind.CREATED,
        actor_type=STAFF,
        actor_id=me,
        before=None,
        after=service.snapshot(order, items, []),
    )
    service.event(
        session,
        order,
        "created",
        actor_type=STAFF,
        actor_id=me,
        payload={"source": order.source, "submitted": payload.submit},
    )
    await session.commit()
    await _after(ctx, order, [], todos)
    return order


async def _submit(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal | None,
    order: Order,
    items: list[Any],
    settings: OrderSettings,
    now: datetime,
) -> uuid.UUID:
    """草稿提交审核：检查必填信息，生成"订单审核"待办。"""
    missing = service.missing_required(order, settings)
    if missing:
        raise Unprocessable(f"提交前请补全：{'、'.join(missing)}")
    order.status = OrderStatus.PENDING_REVIEW
    order.submitted_at = now
    reviewer = (
        principal.staff_id
        if principal is not None and principal.has(Permission.ORDER_REVIEW)
        else None
    )
    todo = await service.open_review_todo(
        session,
        ctx.keys,
        order,
        items,
        actor_type=ActorType.STAFF if principal else ActorType.AI,
        actor_id=principal.staff_id if principal else None,
        assignee_id=reviewer,
        now=now,
    )
    service.event(
        session,
        order,
        "submitted",
        actor_type=STAFF if principal else ActorType.AI,
        actor_id=principal.staff_id if principal else None,
        payload={"todo_id": str(todo.id)},
        public=True,
    )
    return todo.id


def _can_edit(principal: Principal, order: Order) -> bool:
    if principal.has(Permission.ORDER_REVIEW):
        return True
    return (
        principal.has(Permission.ORDER_CREATE)
        and order.status == OrderStatus.DRAFT
        and order.created_by == principal.staff_id
    )


async def update(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    payload: OrderUpdate,
    *,
    ip: str | None = None,
) -> tuple[Order, OrderNotice | None]:
    order = await service.get_visible(session, principal, order_id, lock=True)
    if not _can_edit(principal, order):
        raise Forbidden("没有修改这个订单的权限")
    if payload.version != order.version:
        raise Conflict("订单已被其他人修改，请刷新后重试")
    fields = payload.model_fields_set
    settings = await order_settings.load(session, principal.tenant_id)
    now = service.utcnow()
    me = principal.staff_id
    old_items = await service.load_items(session, order.id)
    payments = await service.load_payments(session, order.id)
    before = service.snapshot(order, old_items, payments)
    items = old_items
    content = False  # 改了商品、数量、单价或优惠
    if (payload.items is not None or payload.discount is not None) and order.status not in EDITABLE:
        raise Unprocessable("已发货、已完成或已取消的订单不能修改商品和金额")
    if payload.items is not None:
        items = await service.build_items(session, order, lines_of(payload.items), settings)
        # 没有传单价的新商品行按建议零售价；原有商品行保持原来的单价。
        previous = {(i.product_id, i.raw_text): i.unit_price for i in old_items}
        for item, line in zip(items, payload.items, strict=True):
            if line.unit_price is None and (item.product_id, item.raw_text) in previous:
                item.unit_price = previous[(item.product_id, item.raw_text)]
                item.amount = (
                    service.cents(item.unit_price * item.quantity)
                    if item.unit_price is not None
                    else service.ZERO
                )
        if _prices_changed(old_items, items) and not principal.has(Permission.ORDER_PRICE):
            raise Forbidden("改价需要 order:price 权限")
        content = True
    if payload.discount is not None and payload.discount != order.discount:
        if not principal.has(Permission.ORDER_PRICE):
            raise Forbidden("优惠需要 order:price 权限")
        order.discount = payload.discount
        content = True
    if content and payload.reason is None:
        raise Unprocessable("改价、改商品、改数量时请选择原因")
    receiver_changed: list[str] = []
    if payload.receiver is not None:
        order.receiver, receiver_changed = await service.merge_receiver(
            ctx.keys, principal.tenant_id, order.receiver, payload.receiver.model_dump()
        )
    if "payment_method" in fields and payload.payment_method != order.payment_method:
        if (
            payload.payment_method is not None
            and payload.payment_method not in settings.payment_methods
        ):
            raise Unprocessable("这个收款方式没有启用")
        if order.status not in (
            OrderStatus.DRAFT,
            OrderStatus.PENDING_REVIEW,
            OrderStatus.CONFIRMED,
        ):
            raise Unprocessable("开始处理后不能修改收款方式")
        if (
            payload.payment_method == PaymentMethod.CREDIT.value
            and order.status == OrderStatus.CONFIRMED
        ):
            if not principal.has(Permission.ORDER_CREDIT):
                raise Forbidden("暂欠需要有 order:credit 权限的员工同意")
            order.credit_approved_by, order.credit_approved_at = me, now
        order.payment_method = payload.payment_method
    if "deposit_amount" in fields:
        order.deposit_amount = payload.deposit_amount
    if "credit_due_date" in fields:
        order.credit_due_date = payload.credit_due_date
    if "expected_at" in fields:
        order.expected_at = payload.expected_at
    if payload.customer_note is not None:
        order.customer_note = payload.customer_note.strip()
    if payload.internal_note is not None:
        order.internal_note = payload.internal_note.strip()
    if payload.items is not None:
        service.carry_work(old_items, items)
        for item in old_items:
            await session.delete(item)
        await session.flush()
        session.add_all(items)
    service.recompute(order, items, payments)
    if content:
        service.check_discount(principal, items, order.total, settings)
    after = service.snapshot(order, items, payments)
    changes = service.diff(before, after)
    if receiver_changed:
        changes["receiver"] = receiver_changed
    if not changes:
        return order, None
    if content or receiver_changed or "payment_method" in changes:
        order.modified = True
    if payload.reason == RevisionReason.AI_ERROR.value:
        order.ai_error = True
    service.add_revision(
        session,
        order,
        kind=RevisionKind.EDIT,
        actor_type=STAFF,
        actor_id=me,
        before=before,
        after=after,
        reason=payload.reason,
        note=payload.note,
        extra={"receiver": receiver_changed} if receiver_changed else None,
    )
    service.event(
        session,
        order,
        "updated",
        actor_type=STAFF,
        actor_id=me,
        payload={"reason": payload.reason, "fields": sorted(changes)},
        public=order.status not in (OrderStatus.DRAFT,) and content,
    )
    if content:
        record_audit(
            session,
            action="order.update",
            actor_type="staff",
            actor_id=me,
            tenant_id=principal.tenant_id,
            resource_type="order",
            resource_id=str(order.id),
            detail={"no": order.no, "reason": payload.reason, "changes": sorted(changes)},
            ip=ip,
        )
    todos: list[uuid.UUID] = []
    if payload.items is not None:
        todos = await service.after_edit(session, ctx.keys, order, items, actor_id=me, now=now)
    notice: OrderNotice | None = None
    room: uuid.UUID | None = None
    if payload.notify_customer and order.status in (
        OrderStatus.CONFIRMED,
        OrderStatus.FULFILLING,
    ):
        text = render(settings.update_template, _values(ctx, order, items))
        notice, room = await _notice(session, order, text, actor_id=me, now=now)
    await session.commit()
    await _after(ctx, order, [room], todos)
    return order, notice


async def submit(
    ctx: AppContext, session: AsyncSession, principal: Principal, order_id: uuid.UUID
) -> Order:
    order = await service.get_visible(session, principal, order_id, lock=True)
    service.require_status(order, (OrderStatus.DRAFT,), "只有草稿可以提交审核")
    if not _can_edit(principal, order):
        raise Forbidden("没有提交这个订单的权限")
    settings = await order_settings.load(session, principal.tenant_id)
    now = service.utcnow()
    items = await service.load_items(session, order.id)
    payments = await service.load_payments(session, order.id)
    before = service.snapshot(order, items, payments)
    todo_id = await _submit(ctx, session, principal, order, items, settings, now)
    service.add_revision(
        session,
        order,
        kind=RevisionKind.STATUS,
        actor_type=STAFF,
        actor_id=principal.staff_id,
        before=before,
        after=service.snapshot(order, items, payments),
    )
    await session.commit()
    await _after(ctx, order, [], [todo_id])
    return order


# ---- 审核与跟进 ----


async def _transition(
    session: AsyncSession,
    principal: Principal | None,
    order: Order,
    status: OrderStatus,
    event_type: str,
    *,
    payload: dict[str, Any] | None = None,
    change: Any = None,
) -> tuple[list[Any], list[OrderPayment]]:
    """改状态并记版本和动态（change 在记录前修改订单的其他内容）。"""
    items = await service.load_items(session, order.id)
    payments = await service.load_payments(session, order.id)
    before = service.snapshot(order, items, payments)
    order.status = status
    if change is not None:
        change()
    service.recompute(order, items, payments)
    actor_type = STAFF if principal else ActorType.SYSTEM
    actor_id = principal.staff_id if principal else None
    service.add_revision(
        session,
        order,
        kind=RevisionKind.STATUS,
        actor_type=actor_type,
        actor_id=actor_id,
        before=before,
        after=service.snapshot(order, items, payments),
    )
    service.event(
        session,
        order,
        event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        payload=payload,
        public=True,
    )
    return items, payments


async def confirm(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    payload: OrderConfirmRequest,
) -> tuple[Order, OrderNotice | None]:
    """确认：商品都已对应到商品库并定价，收货信息齐全，确定收款方式；然后告知客户（带跟踪链接）。"""
    order = await service.get_visible(session, principal, order_id, lock=True)
    service.require_status(order, (OrderStatus.PENDING_REVIEW,), "只有待审核的订单可以确认")
    settings = await order_settings.load(session, principal.tenant_id)
    now = service.utcnow()
    me = principal.staff_id
    items = await service.load_items(session, order.id)
    if any(i.product_id is None for i in items):
        raise Unprocessable("还有没对应到商品库的商品，请先修改订单")
    if order.price_pending:
        raise Unprocessable("还有待定价的商品，请先填写单价")
    missing = service.missing_required(order, settings)
    if missing:
        raise Unprocessable(f"请先补全：{'、'.join(missing)}")
    if payload.payment_method not in settings.payment_methods:
        raise Unprocessable("这个收款方式没有启用")
    deposit: Decimal | None = None
    if payload.payment_method == PaymentMethod.DEPOSIT.value:
        if payload.deposit_amount is None:
            raise Unprocessable("预付定金需要填写定金金额")
        if payload.deposit_amount > order.total:
            raise Unprocessable("定金不能超过订单金额")
        deposit = payload.deposit_amount
    if payload.payment_method == PaymentMethod.CREDIT.value:
        if not principal.has(Permission.ORDER_CREDIT):
            raise Forbidden("暂欠需要有 order:credit 权限的员工同意")
        if payload.credit_due_date is None:
            raise Unprocessable("暂欠需要填写约定付款日期")
        if payload.credit_due_date < today(sla.tz_of(await sla.business_hours(session))):
            raise Unprocessable("约定付款日期不能早于今天")

    def change() -> None:
        order.payment_method = payload.payment_method
        order.deposit_amount = deposit
        credit = payload.payment_method == PaymentMethod.CREDIT.value
        order.credit_due_date = payload.credit_due_date if credit else None
        order.credit_approved_by = me if credit else None
        order.credit_approved_at = now if credit else None
        order.confirmed_at, order.confirmed_by = now, me

    await _transition(
        session,
        principal,
        order,
        OrderStatus.CONFIRMED,
        "confirmed",
        payload={"payment_method": payload.payment_method, "note": payload.note},
        change=change,
    )
    await service.close_todo(
        session,
        order.review_todo_id,
        result="订单已确认",
        cancelled=False,
        actor_type=STAFF,
        actor_id=me,
        now=now,
    )
    notice: OrderNotice | None = None
    room: uuid.UUID | None = None
    if payload.notify_customer:
        text = render(settings.confirm_template, _values(ctx, order, items))
        notice, room = await _notice(session, order, text, actor_id=me, now=now)
    await session.commit()
    await _after(ctx, order, [room], [])
    return order, notice


def _net_paid(order: Order) -> Decimal:
    return order.paid_amount - order.refunded_amount


def check_startable(order: Order) -> None:
    """开始处理前的收款条件（加工页的"待领取"与这里一致，见 production.claimable）。"""
    method = order.payment_method
    if method == PaymentMethod.ONLINE and service.outstanding(order) > 0:
        raise Unprocessable("在线收款的订单要先收清全款，才能开始处理")
    if (
        method == PaymentMethod.DEPOSIT
        and order.deposit_amount is not None
        and _net_paid(order) < order.deposit_amount
    ):
        raise Unprocessable("还没有收到定金，不能开始处理")
    if method == PaymentMethod.CREDIT and order.credit_approved_at is None:
        raise Unprocessable("暂欠需要主管同意后才能开始处理")


async def begin(session: AsyncSession, principal: Principal, order: Order) -> None:
    """已确认 → 处理中（员工点"开始处理"，或工人领取时；由调用方提交）。"""
    service.require_status(order, (OrderStatus.CONFIRMED,), "只有已确认的订单可以开始处理")
    check_startable(order)
    now = service.utcnow()

    def change() -> None:
        order.started_at = now

    await _transition(session, principal, order, OrderStatus.FULFILLING, "started", change=change)


async def start(
    ctx: AppContext, session: AsyncSession, principal: Principal, order_id: uuid.UUID
) -> Order:
    order = await service.get_visible(session, principal, order_id, lock=True)
    await begin(session, principal, order)
    await session.commit()
    return order


async def ship(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    payload: ShipRequest,
) -> tuple[Order, OrderNotice | None]:
    order = await service.get_visible(session, principal, order_id, lock=True)
    service.require_status(order, (OrderStatus.FULFILLING,), "只有处理中的订单可以登记发货")
    settings = await order_settings.load(session, principal.tenant_id)
    if not settings.shipping_enabled:
        raise Unprocessable("订单设置里没有发货环节，处理完成后直接完成订单")
    if (
        order.payment_method == PaymentMethod.DEPOSIT
        and settings.deposit_balance == "before_ship"
        and service.outstanding(order) > 0
    ):
        raise Unprocessable("预付定金的订单要在发货前收清尾款")
    now = service.utcnow()
    company, number = payload.shipping_company.strip(), payload.tracking_no.strip()

    def change() -> None:
        order.shipping_company, order.tracking_no, order.shipped_at = company, number, now

    items, _ = await _transition(
        session,
        principal,
        order,
        OrderStatus.SHIPPED,
        "shipped",
        payload={"shipping_company": company, "tracking_no": number},
        change=change,
    )
    await service.close_production_todos(
        session,
        order,
        result="订单已发货",
        cancelled=False,
        actor_type=STAFF,
        actor_id=principal.staff_id,
        now=now,
    )
    # 发货时出库（扣减库存，§25.12）。
    await stock.ship_out(
        session, order, items, actor=stock.Actor(STAFF, principal.staff_id), now=now
    )
    notice: OrderNotice | None = None
    room: uuid.UUID | None = None
    if payload.notify_customer:
        text = render(
            settings.ship_template,
            _values(ctx, order, items, company=company, tracking_no=number),
        )
        notice, room = await _notice(session, order, text, actor_id=principal.staff_id, now=now)
    await session.commit()
    await _after(ctx, order, [room], [])
    return order, notice


async def complete(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    payload: NotifyFlag,
) -> tuple[Order, OrderNotice | None]:
    """完成：已发货的签收后完成；没有发货环节的（服务类）处理中直接完成。货到付款、预付定金要先收清；
    暂欠可以完成，未收金额进入"应收"。"""
    order = await service.get_visible(session, principal, order_id, lock=True)
    service.require_status(
        order, (OrderStatus.FULFILLING, OrderStatus.SHIPPED), "只有处理中或已发货的订单可以完成"
    )
    settings = await order_settings.load(session, principal.tenant_id)
    if order.payment_method != PaymentMethod.CREDIT and service.outstanding(order) > 0:
        raise Unprocessable(
            "货到付款的订单要先登记收款再完成"
            if order.payment_method == PaymentMethod.COD
            else "还有未收清的款项，请先登记收款"
        )
    now = service.utcnow()

    def change() -> None:
        order.completed_at = now
        service.expire_tracking(order, settings, now)

    items, _ = await _transition(
        session, principal, order, OrderStatus.COMPLETED, "completed", change=change
    )
    await service.close_production_todos(
        session,
        order,
        result="订单已完成",
        cancelled=False,
        actor_type=STAFF,
        actor_id=principal.staff_id,
        now=now,
    )
    # 没有发货环节的订单在完成时出库；已发货的订单在发货时已经出库，这里不再扣。
    await stock.ship_out(
        session, order, items, actor=stock.Actor(STAFF, principal.staff_id), now=now
    )
    notice: OrderNotice | None = None
    room: uuid.UUID | None = None
    if payload.notify_customer:
        text = render(settings.complete_template, _values(ctx, order, items))
        notice, room = await _notice(session, order, text, actor_id=principal.staff_id, now=now)
    await session.commit()
    await _after(ctx, order, [room], [])
    return order, notice


async def cancel(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    payload: OrderCancelRequest,
    *,
    ip: str | None = None,
) -> tuple[Order, OrderNotice | None]:
    """取消（已发货、已完成的不能取消）。有收款时在动态里提示登记退款。"""
    order = await service.get_visible(session, principal, order_id, lock=True)
    if order.status in (*CLOSED, OrderStatus.SHIPPED):
        raise Unprocessable("已发货、已完成或已取消的订单不能取消")
    if order.status == OrderStatus.DRAFT:
        if not _can_edit(principal, order):
            raise Forbidden("没有取消这个订单的权限")
    elif not principal.has(Permission.ORDER_REVIEW):
        raise Forbidden("没有执行该操作的权限")
    settings = await order_settings.load(session, principal.tenant_id)
    now = service.utcnow()
    me = principal.staff_id
    reason = payload.reason.strip()
    notify = payload.notify_customer and order.status != OrderStatus.DRAFT

    def change() -> None:
        order.cancelled_at, order.cancel_reason = now, reason
        service.expire_tracking(order, settings, now)

    items, _ = await _transition(
        session,
        principal,
        order,
        OrderStatus.CANCELLED,
        "cancelled",
        payload={"reason": reason, "refund_needed": _net_paid(order) > 0},
        change=change,
    )
    for todo_id in (
        order.review_todo_id,
        order.collection_todo_id,
        order.ship_todo_id,
        order.shortage_todo_id,
    ):
        await service.close_todo(
            session,
            todo_id,
            result="订单已取消",
            cancelled=True,
            actor_type=STAFF,
            actor_id=me,
            now=now,
        )
    await stock.return_order(session, order, actor=stock.Actor(STAFF, me))
    # 还没生效的领料单、入库单随之作废（§25.13）。
    await documents.void_for_order(session, order, staff_id=me, reason="订单已取消")
    record_audit(
        session,
        action="order.cancel",
        actor_type="staff",
        actor_id=me,
        tenant_id=principal.tenant_id,
        resource_type="order",
        resource_id=str(order.id),
        detail={"no": order.no, "reason": reason},
        ip=ip,
    )
    notice: OrderNotice | None = None
    room: uuid.UUID | None = None
    if notify:
        text = render(settings.cancel_template, _values(ctx, order, items, reason=reason))
        notice, room = await _notice(session, order, text, actor_id=me, now=now)
    await session.commit()
    await _after(ctx, order, [room], [])
    return order, notice


# ---- 收款 ----


async def add_payment(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    payload: PaymentIn,
    *,
    ip: str | None = None,
) -> Order:
    order = await service.get_visible(session, principal, order_id, lock=True)
    now = service.utcnow()
    refund = payload.kind == PaymentKind.REFUND.value
    if order.status == OrderStatus.DRAFT or (order.status == OrderStatus.CANCELLED and not refund):
        raise Unprocessable("草稿和已取消的订单不能登记收款")
    paid_at = payload.paid_at or now
    if paid_at > now + timedelta(minutes=5):
        raise Unprocessable("收款时间不能晚于现在")
    if refund and payload.amount > _net_paid(order):
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
        reference_no=(payload.reference_no or "").strip() or None,
        proof_url=payload.proof_url,
        note=(payload.note or "").strip() or None,
        recorded_by_type=STAFF,
        recorded_by=principal.staff_id,
        created_at=now,
    )
    session.add(payment)
    payments.append(payment)
    service.recompute(order, items, payments)
    await _after_payment(session, principal, order, items, payments, before, now)
    service.event(
        session,
        order,
        "refunded" if refund else "paid",
        actor_type=STAFF,
        actor_id=principal.staff_id,
        payload={"amount": service.text_money(payload.amount), "channel": payload.channel},
        public=True,
    )
    record_audit(
        session,
        action="order.refund" if refund else "order.payment",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="order",
        resource_id=str(order.id),
        detail={
            "no": order.no,
            "amount": service.text_money(payload.amount),
            "channel": payload.channel,
        },
        ip=ip,
    )
    await session.commit()
    return order


async def _after_payment(
    session: AsyncSession,
    principal: Principal,
    order: Order,
    items: list[Any],
    payments: list[OrderPayment],
    before: dict[str, Any],
    now: datetime,
) -> None:
    service.add_revision(
        session,
        order,
        kind=RevisionKind.PAYMENT,
        actor_type=STAFF,
        actor_id=principal.staff_id,
        before=before,
        after=service.snapshot(order, items, payments),
    )
    if service.outstanding(order) == 0 and order.total > 0:
        await service.close_todo(
            session,
            order.collection_todo_id,
            result="款项已收清",
            cancelled=False,
            actor_type=STAFF,
            actor_id=principal.staff_id,
            now=now,
        )


async def void_payment(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    payment_id: uuid.UUID,
    payload: VoidRequest,
    *,
    ip: str | None = None,
) -> Order:
    order = await service.get_visible(session, principal, order_id, lock=True)
    payment = await session.scalar(
        select(OrderPayment).where(OrderPayment.id == payment_id, OrderPayment.order_id == order.id)
    )
    if payment is None:
        raise NotFound("收款记录不存在")
    if payment.voided_at is not None:
        raise Conflict("这笔记录已经作废了")
    now = service.utcnow()
    items = await service.load_items(session, order.id)
    payments = await service.load_payments(session, order.id)
    before = service.snapshot(order, items, payments)
    payment.voided_at, payment.voided_by, payment.void_reason = (
        now,
        principal.staff_id,
        payload.reason.strip(),
    )
    service.recompute(order, items, payments)
    await _after_payment(session, principal, order, items, payments, before, now)
    service.event(
        session,
        order,
        "payment_voided",
        actor_type=STAFF,
        actor_id=principal.staff_id,
        payload={"amount": service.text_money(payment.amount), "reason": payment.void_reason},
    )
    record_audit(
        session,
        action="order.payment_void",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="order",
        resource_id=str(order.id),
        detail={
            "no": order.no,
            "amount": service.text_money(payment.amount),
            "reason": payment.void_reason,
        },
        ip=ip,
    )
    await session.commit()
    return order


# ---- 其他 ----


async def reveal(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    *,
    ip: str | None = None,
) -> dict[str, str]:
    """查看完整的收货信息（需要 customer:view_sensitive，记审计日志）。"""
    order = await service.get_visible(session, principal, order_id)
    values = await service.reveal_receiver(ctx.keys, order.tenant_id, order.receiver)
    record_audit(
        session,
        action="order.view_receiver",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="order",
        resource_id=str(order.id),
        detail={"no": order.no, "fields": sorted(values)},
        ip=ip,
    )
    await session.commit()
    return values


async def regenerate_link(
    ctx: AppContext, session: AsyncSession, principal: Principal, order_id: uuid.UUID
) -> Order:
    """重新生成跟踪链接：旧链接立即失效。"""
    order = await service.get_visible(session, principal, order_id, lock=True)
    order.tracking_token = service.new_token()
    if order.status not in CLOSED:
        order.tracking_expires_at = None
    service.event(session, order, "link_regenerated", actor_type=STAFF, actor_id=principal.staff_id)
    await session.commit()
    return order


async def assign(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    payload: OrderAssignRequest,
) -> Order:
    """转交订单（没有结束的"订单审核"待办一起转交并提醒新的处理人）。"""
    order = await service.get_visible(session, principal, order_id, lock=True)
    if order.status in CLOSED:
        raise Unprocessable("已完成或已取消的订单不能转交")
    if payload.assignee_id is None and payload.skill_group_id is None:
        raise Unprocessable("请选择员工或技能组")
    if payload.assignee_id is not None:
        await _staff(session, payload.assignee_id)
    if (
        payload.skill_group_id is not None
        and await session.get(SkillGroup, payload.skill_group_id) is None
    ):
        raise Unprocessable("技能组不存在")
    me = principal.staff_id
    previous = order.assignee_id
    order.assignee_id, order.skill_group_id = payload.assignee_id, payload.skill_group_id
    todos: list[uuid.UUID] = []
    if order.review_todo_id is not None:
        todo = await session.get(Todo, order.review_todo_id, with_for_update=True)
        if todo is not None and todo.status in TODO_UNFINISHED:
            todo.assignee_id, todo.skill_group_id = payload.assignee_id, payload.skill_group_id
            todo.assigned_by = me if payload.assignee_id not in (None, me) else None
            todo.notify_reason = NotifyReason.ASSIGNED
            todo.notified_at = datetime.now(UTC) if payload.assignee_id == me else None
            todos.append(todo.id)
    service.event(
        session,
        order,
        "assigned",
        actor_type=STAFF,
        actor_id=me,
        payload={
            "from": str(previous) if previous else None,
            "to": str(payload.assignee_id) if payload.assignee_id else None,
            "skill_group_id": str(payload.skill_group_id) if payload.skill_group_id else None,
        },
    )
    await session.commit()
    await _after(ctx, order, [], todos)
    return order


async def notify(
    ctx: AppContext, session: AsyncSession, principal: Principal, order_id: uuid.UUID, text: str
) -> OrderNotice:
    order = await service.get_visible(session, principal, order_id)
    notice, room = await _notice(
        session, order, text.strip(), actor_id=principal.staff_id, now=service.utcnow()
    )
    await session.commit()
    await _after(ctx, order, [room], [])
    return notice
