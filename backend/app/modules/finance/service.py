"""应收账款（设计文档 §28）：应收的定义（到期日按收款方式、逾期天数、账龄分段）、汇总、列表、
按客户汇总、最近收款、跟进、催收、对账单。应收不另外记账，全部从订单和收款记录算出。"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, Date, and_, case, cast, func, literal, null, or_, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.dates import today
from app.core.errors import Conflict, NotFound, Unprocessable
from app.modules.customer.models import Customer
from app.modules.finance.schemas import (
    BUCKET_LABELS,
    BUCKETS,
    AmountCount,
    Bucket,
    BucketOut,
    CollectIn,
    CollectionTodoOut,
    CustomerReceivableOut,
    CustomerReceivablePage,
    CustomerSort,
    CustomerStatement,
    FollowupIn,
    ReceivableFilters,
    ReceivableOut,
    ReceivablePage,
    ReceivableSummary,
    RecentPaymentList,
    RecentPaymentOut,
    Sort,
    StaffOption,
    StaffOptionList,
    StatementLine,
)
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.orders import queries, service
from app.modules.orders import settings as order_settings
from app.modules.orders.models import (
    Order,
    OrderItem,
    OrderPayment,
    PaymentKind,
    PaymentMethod,
)
from app.modules.orders.settings import OrderSettings
from app.modules.todos import notify as todo_notify
from app.modules.todos import presets, sla
from app.modules.todos import service as todo_service
from app.modules.todos.models import UNFINISHED, ActorType, Todo, TodoSource

ZERO = Decimal("0")
DUE_SOON_DAYS = 7
CHANNEL_LABELS: dict[str, str] = {
    "wechat": "微信",
    "alipay": "支付宝",
    "bank": "银行转账",
    "cash": "现金",
    "other": "其他",
}
# 订单动态的类型（订单详情和修改历史里显示）。
FOLLOWUP_EVENT = "collection_followup"
COLLECT_EVENT = "collection_manual"


@dataclass(frozen=True)
class Calendar:
    """租户的"今天"（按租户时区）和影响到期日的订单设置。"""

    tz: ZoneInfo
    today: date
    settings: OrderSettings


async def calendar(
    session: AsyncSession, tenant_id: uuid.UUID, now: datetime | None = None
) -> Calendar:
    tz = sla.tz_of(await sla.business_hours(session))
    settings = await order_settings.load(session, tenant_id)
    return Calendar(tz=tz, today=today(tz, now), settings=settings)


# ---- 应收的定义（§28.3） ----


def receivable() -> ColumnElement[bool]:
    """算应收的订单：已确认及之后、合计大于 0、还没收清。"""
    return and_(
        Order.status.in_(queries.RECEIVABLE),
        Order.payment_status.in_(queries.UNPAID),
        Order.total > 0,
    )


def net() -> ColumnElement[Any]:
    return Order.paid_amount - Order.refunded_amount


def outstanding() -> ColumnElement[Any]:
    return Order.total - net()


def local_date(column: Any, tz: ZoneInfo) -> ColumnElement[Any]:
    return cast(func.timezone(tz.key, column), Date)


def due_date(cal: Calendar) -> ColumnElement[Any]:
    """到期日：暂欠是约定付款日；在线收款、没收到定金的是确认当天；尾款是可以发货那天（发货前收清）
    或发货当天（货到时收取）；货到付款是发货当天；没有发货环节时都是加工完成当天。还没到的为空。"""
    tz = cal.tz
    confirmed = local_date(Order.confirmed_at, tz)
    shipped = local_date(Order.shipped_at, tz)
    # 可以交付的那天：加工完成；不用加工的订单是开始处理当天。
    ready = case(
        (service.needs_production(), local_date(Order.processed_at, tz)),
        else_=local_date(Order.started_at, tz),
    )
    balance: ColumnElement[Any]
    delivery: ColumnElement[Any]
    if not cal.settings.shipping_enabled:
        balance = ready
        delivery = ready
    elif cal.settings.deposit_balance == "before_ship":
        balance = ready
        delivery = shipped
    else:
        balance = shipped
        delivery = shipped
    deposit_pending = and_(
        Order.payment_method == PaymentMethod.DEPOSIT.value,
        or_(Order.deposit_amount.is_(None), net() < Order.deposit_amount),
    )
    return case(
        (Order.payment_method == PaymentMethod.CREDIT.value, Order.credit_due_date),
        (Order.payment_method == PaymentMethod.ONLINE.value, confirmed),
        (deposit_pending, confirmed),
        (Order.payment_method == PaymentMethod.DEPOSIT.value, balance),
        (Order.payment_method == PaymentMethod.COD.value, delivery),
        else_=null(),
    )


def bucket_of(overdue_days: int) -> Bucket:
    if overdue_days <= 0:
        return "current"
    if overdue_days <= 30:
        return "d1_30"
    if overdue_days <= 60:
        return "d31_60"
    if overdue_days <= 90:
        return "d61_90"
    return "d90_plus"


def bucket_expr(due: ColumnElement[Any], day: date) -> ColumnElement[Any]:
    """账龄分段（和 bucket_of 一致）。"""
    return case(
        (or_(due.is_(None), due >= day), literal("current")),
        (due >= day - timedelta(days=30), literal("d1_30")),
        (due >= day - timedelta(days=60), literal("d31_60")),
        (due >= day - timedelta(days=90), literal("d61_90")),
        else_=literal("d90_plus"),
    )


def bucket_condition(which: Bucket, due: ColumnElement[Any], day: date) -> ColumnElement[bool]:
    match which:
        case "current":
            return or_(due.is_(None), due >= day)
        case "d1_30":
            return and_(due < day, due >= day - timedelta(days=30))
        case "d31_60":
            return and_(due < day - timedelta(days=30), due >= day - timedelta(days=60))
        case "d61_90":
            return and_(due < day - timedelta(days=60), due >= day - timedelta(days=90))
    return due < day - timedelta(days=90)


def view_condition(view: str, due: ColumnElement[Any], day: date) -> ColumnElement[bool] | None:
    match view:
        case "overdue":
            return due < day
        case "due_today":
            return due == day
        case "due_soon":
            return and_(due > day, due <= day + timedelta(days=DUE_SOON_DAYS))
        case "not_due":
            return or_(due.is_(None), due >= day)
        case "promised":
            return Order.promise_date.is_not(None)
        case "promise_overdue":
            return Order.promise_date < day
    return None


def conditions(cal: Calendar, filters: ReceivableFilters) -> ColumnElement[bool]:
    """列表、按客户和导出共用的筛选条件。"""
    due = due_date(cal)
    where: list[ColumnElement[bool]] = [receivable()]
    by_view = view_condition(filters.view, due, cal.today)
    if by_view is not None:
        where.append(by_view)
    if filters.bucket:
        where.append(bucket_condition(filters.bucket, due, cal.today))
    if filters.payment_method:
        where.append(Order.payment_method == filters.payment_method)
    if filters.assignee_id:
        where.append(Order.assignee_id == filters.assignee_id)
    if filters.customer_id:
        where.append(Order.customer_id == filters.customer_id)
    if filters.q and filters.q.strip():
        like = f"%{filters.q.strip()}%"
        where.append(
            or_(
                Order.no.ilike(like),
                Order.external_no.ilike(like),
                Order.customer_id.in_(
                    select(Customer.id).where(
                        or_(Customer.display_name.ilike(like), Customer.company.ilike(like))
                    )
                ),
            )
        )
    return and_(*where)


# ---- 汇总与列表 ----


async def count(session: AsyncSession, cal: Calendar, filters: ReceivableFilters) -> int:
    where = conditions(cal, filters)
    return int(await session.scalar(select(func.count()).select_from(Order).where(where)) or 0)


async def list_receivables(
    session: AsyncSession,
    cal: Calendar,
    filters: ReceivableFilters,
    *,
    sort: Sort = "due",
    limit: int = 20,
    offset: int = 0,
) -> ReceivablePage:
    where = conditions(cal, filters)
    due = due_date(cal)
    total = int(await session.scalar(select(func.count()).select_from(Order).where(where)) or 0)
    ordering: list[Any]
    if sort == "outstanding":
        ordering = [outstanding().desc(), Order.created_at]
    elif sort == "age":
        ordering = [Order.confirmed_at.asc().nulls_last(), Order.created_at]
    else:
        ordering = [due.asc().nulls_last(), Order.confirmed_at, Order.created_at]
    rows = (
        await session.execute(
            select(Order, due).where(where).order_by(*ordering).limit(limit).offset(offset)
        )
    ).all()
    return ReceivablePage(
        items=await outs(session, cal, [(row[0], row[1]) for row in rows]), total=total
    )


async def one(session: AsyncSession, cal: Calendar, order_id: uuid.UUID) -> ReceivableOut:
    row = (
        await session.execute(
            select(Order, due_date(cal)).where(Order.id == order_id, receivable())
        )
    ).first()
    if row is None:
        raise NotFound("订单不存在，或者已经收清")
    [out] = await outs(session, cal, [(row[0], row[1])])
    return out


async def outs(
    session: AsyncSession, cal: Calendar, rows: list[tuple[Order, date | None]]
) -> list[ReceivableOut]:
    orders = [order for order, _ in rows]
    if not orders:
        return []
    for order in orders:
        if sa_inspect(order).expired_attributes:
            await session.refresh(order)
    items: dict[uuid.UUID, list[OrderItem]] = defaultdict(list)
    for item in await session.scalars(
        select(OrderItem)
        .where(OrderItem.order_id.in_([o.id for o in orders]))
        .order_by(OrderItem.sort)
    ):
        items[item.order_id].append(item)
    customers = await customer_names(session, [o.customer_id for o in orders])
    todo_ids = [o.collection_todo_id for o in orders if o.collection_todo_id is not None]
    todos: dict[uuid.UUID, Any] = {}
    if todo_ids:
        todos = {
            row.id: row
            for row in await session.execute(
                select(Todo.id, Todo.no, Todo.status, Todo.assignee_id).where(
                    Todo.id.in_(todo_ids), Todo.status.in_(UNFINISHED)
                )
            )
        }
    staff = await queries.names(
        session,
        Staff,
        Staff.display_name,
        [*(o.assignee_id for o in orders), *(t.assignee_id for t in todos.values())],
    )
    return [_out(order, due, cal, items[order.id], customers, staff, todos) for order, due in rows]


async def customer_names(
    session: AsyncSession, ids: list[uuid.UUID | None]
) -> dict[uuid.UUID, tuple[str, str | None]]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.execute(
        select(Customer.id, Customer.display_name, Customer.company).where(Customer.id.in_(wanted))
    )
    return {row.id: (row.display_name, row.company) for row in rows}


def _out(
    order: Order,
    due: date | None,
    cal: Calendar,
    items: list[OrderItem],
    customers: dict[uuid.UUID, tuple[str, str | None]],
    staff: dict[uuid.UUID, str],
    todos: dict[uuid.UUID, Any],
) -> ReceivableOut:
    day = cal.today
    overdue_days = (day - due).days if due is not None and due < day else 0
    confirmed = order.confirmed_at.astimezone(cal.tz).date() if order.confirmed_at else day
    customer = customers.get(order.customer_id) if order.customer_id else None
    todo = todos.get(order.collection_todo_id) if order.collection_todo_id else None
    return ReceivableOut(
        id=order.id,
        no=order.no,
        status=order.status,
        customer_id=order.customer_id,
        customer_name=customer[0] if customer else None,
        customer_company=customer[1] if customer else None,
        assignee_id=order.assignee_id,
        assignee_name=staff.get(order.assignee_id) if order.assignee_id else None,
        summary=service.summary(items),
        payment_method=order.payment_method,
        payment_status=order.payment_status,
        total=order.total,
        paid_amount=order.paid_amount,
        refunded_amount=order.refunded_amount,
        outstanding=service.outstanding(order),
        confirmed_at=order.confirmed_at,
        due_date=due,
        overdue_days=overdue_days,
        age_days=max(0, (day - confirmed).days),
        bucket=bucket_of(overdue_days),
        promise_date=order.promise_date,
        promise_overdue=order.promise_date is not None and order.promise_date < day,
        followed_up_at=order.followed_up_at,
        follow_up_note=order.follow_up_note,
        collection_todo=CollectionTodoOut(
            id=todo.id,
            no=todo.no,
            status=todo.status,
            assignee_id=todo.assignee_id,
            assignee_name=staff.get(todo.assignee_id) if todo.assignee_id else None,
        )
        if todo is not None
        else None,
    )


async def summary(session: AsyncSession, cal: Calendar) -> ReceivableSummary:
    due = due_date(cal)
    day = cal.today
    amount = outstanding()

    async def stats(*extra: ColumnElement[bool]) -> AmountCount:
        row = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(amount), 0))
                .select_from(Order)
                .where(receivable(), *extra)
            )
        ).one()
        return AmountCount(count=int(row[0]), amount=Decimal(row[1]))

    bucket = bucket_expr(due, day)
    found = {
        str(row[0]): (int(row[1]), Decimal(row[2]))
        for row in await session.execute(
            select(bucket, func.count(), func.coalesce(func.sum(amount), 0))
            .select_from(Order)
            .where(receivable())
            .group_by(bucket)
        )
    }
    month_start = datetime.combine(day.replace(day=1), time.min, tzinfo=cal.tz)
    next_month = (day.replace(day=1) + timedelta(days=32)).replace(day=1)
    month_end = datetime.combine(next_month, time.min, tzinfo=cal.tz)
    signed = case(
        (OrderPayment.kind == PaymentKind.PAYMENT.value, OrderPayment.amount),
        else_=-OrderPayment.amount,
    )
    received = await session.scalar(
        select(func.coalesce(func.sum(signed), 0)).where(
            OrderPayment.voided_at.is_(None),
            OrderPayment.paid_at >= month_start,
            OrderPayment.paid_at < month_end,
        )
    )
    return ReceivableSummary(
        today=day,
        open=await stats(),
        overdue=await stats(due < day),
        due_today=await stats(due == day),
        due_soon=await stats(due > day, due <= day + timedelta(days=DUE_SOON_DAYS)),
        received_this_month=Decimal(received or 0),
        buckets=[
            BucketOut(
                bucket=name,
                label=BUCKET_LABELS[name],
                count=found.get(name, (0, ZERO))[0],
                amount=found.get(name, (0, ZERO))[1],
            )
            for name in BUCKETS
        ],
    )


async def list_customers(
    session: AsyncSession,
    cal: Calendar,
    *,
    q: str | None = None,
    overdue_only: bool = False,
    sort: CustomerSort = "outstanding",
    limit: int = 20,
    offset: int = 0,
) -> CustomerReceivablePage:
    due = due_date(cal)
    day = cal.today
    amount = outstanding()
    overdue_amount = func.sum(case((due < day, amount), else_=0))
    where: list[ColumnElement[bool]] = [receivable(), Order.customer_id.is_not(None)]
    if q and q.strip():
        like = f"%{q.strip()}%"
        where.append(
            Order.customer_id.in_(
                select(Customer.id).where(
                    or_(Customer.display_name.ilike(like), Customer.company.ilike(like))
                )
            )
        )
    grouped = (
        select(
            Order.customer_id.label("customer_id"),
            func.sum(amount).label("outstanding"),
            overdue_amount.label("overdue"),
            func.count().label("orders"),
            func.min(due).label("earliest_due"),
            func.max(Order.followed_up_at).label("followed_up_at"),
        )
        .where(*where)
        .group_by(Order.customer_id)
    )
    if overdue_only:
        grouped = grouped.having(overdue_amount > 0)
    sub = grouped.subquery()
    total = int(await session.scalar(select(func.count()).select_from(sub)) or 0)
    primary = sub.c.overdue.desc() if sort == "overdue" else sub.c.outstanding.desc()
    rows = (
        await session.execute(
            select(sub)
            .order_by(primary, sub.c.earliest_due.asc().nulls_last(), sub.c.customer_id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    ids = [row.customer_id for row in rows]
    if not ids:
        return CustomerReceivablePage(items=[], total=total)
    customers = await customer_names(session, ids)
    last_paid = dict(
        (
            await session.execute(
                select(Order.customer_id, func.max(OrderPayment.paid_at))
                .join(Order, Order.id == OrderPayment.order_id)
                .where(
                    Order.customer_id.in_(ids),
                    OrderPayment.kind == PaymentKind.PAYMENT.value,
                    OrderPayment.voided_at.is_(None),
                )
                .group_by(Order.customer_id)
            )
        ).all()
    )
    notes: dict[uuid.UUID, str | None] = {}
    latest = await session.execute(
        select(Order.customer_id, Order.follow_up_note)
        .where(receivable(), Order.customer_id.in_(ids), Order.followed_up_at.is_not(None))
        .order_by(Order.followed_up_at.desc())
    )
    for customer_id, note in latest.all():
        if customer_id is not None and customer_id not in notes:
            notes[customer_id] = note
    items = []
    for row in rows:
        name, company = customers.get(row.customer_id, ("（已删除的客户）", None))
        earliest: date | None = row.earliest_due
        items.append(
            CustomerReceivableOut(
                customer_id=row.customer_id,
                customer_name=name,
                company=company,
                outstanding=Decimal(row.outstanding),
                overdue=Decimal(row.overdue),
                orders=int(row.orders),
                earliest_due=earliest,
                max_overdue_days=(day - earliest).days if earliest and earliest < day else 0,
                last_paid_at=last_paid.get(row.customer_id),
                followed_up_at=row.followed_up_at,
                follow_up_note=notes.get(row.customer_id),
            )
        )
    return CustomerReceivablePage(items=items, total=total)


async def recent_payments(session: AsyncSession, limit: int = 10) -> RecentPaymentList:
    rows = (
        await session.execute(
            select(OrderPayment, Order.no, Order.customer_id)
            .join(Order, Order.id == OrderPayment.order_id)
            .where(OrderPayment.voided_at.is_(None))
            .order_by(OrderPayment.created_at.desc(), OrderPayment.id.desc())
            .limit(limit)
        )
    ).all()
    customers = await customer_names(session, [row[2] for row in rows])
    staff = await queries.names(
        session, Staff, Staff.display_name, [row[0].recorded_by for row in rows]
    )
    items = []
    for payment, order_no, customer_id in rows:
        customer = customers.get(customer_id) if customer_id else None
        items.append(
            RecentPaymentOut(
                id=payment.id,
                order_id=payment.order_id,
                order_no=order_no,
                customer_id=customer_id,
                customer_name=customer[0] if customer else None,
                kind=payment.kind,
                amount=payment.amount,
                channel=payment.channel,
                paid_at=payment.paid_at,
                recorded_by_name=(
                    staff.get(payment.recorded_by)
                    if payment.recorded_by_type == "staff" and payment.recorded_by
                    else None
                ),
                created_at=payment.created_at,
            )
        )
    return RecentPaymentList(items=items)


async def staff_options(session: AsyncSession) -> StaffOptionList:
    """启用状态的员工（应收账款页筛选处理人、指定催收人用；财务没有查看员工的权限）。"""
    rows = await session.execute(
        select(Staff.id, Staff.display_name)
        .where(Staff.status == StaffStatus.ACTIVE)
        .order_by(Staff.display_name, Staff.id)
    )
    return StaffOptionList(items=[StaffOption(id=row.id, name=row.display_name) for row in rows])


# ---- 跟进与催收（§28.4） ----


async def _open_order(session: AsyncSession, order_id: uuid.UUID, *, lock: bool = False) -> Order:
    query = select(Order).where(Order.id == order_id, receivable())
    if lock:
        query = query.with_for_update()
    order = await session.scalar(query)
    if order is None:
        raise NotFound("订单不存在，或者已经收清")
    return order


async def follow_up(
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    payload: FollowupIn,
    *,
    now: datetime | None = None,
) -> Order:
    """记一次跟进：承诺付款日（可以不改）和备注，写入订单动态。"""
    order = await _open_order(session, order_id, lock=True)
    note = payload.note.strip()
    changes_promise = "promise_date" in payload.model_fields_set
    if not changes_promise and not note:
        raise Unprocessable("请填写承诺付款日或备注")
    if changes_promise:
        order.promise_date = payload.promise_date
    order.followed_up_at = now or datetime.now(UTC)
    order.follow_up_note = note or None
    service.event(
        session,
        order,
        FOLLOWUP_EVENT,
        actor_type=ActorType.STAFF.value,
        actor_id=principal.staff_id,
        payload={
            "promise_date": order.promise_date.isoformat() if order.promise_date else None,
            "note": note,
        },
    )
    await session.commit()
    return order


async def collect(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    payload: CollectIn,
    *,
    now: datetime | None = None,
) -> Order:
    """手工生成一条"催收"待办（系统类型，§24.2）。一个订单同时只有一条未完成的催收待办；收清后由
    收款流程自动完成（orders.actions）。"""
    now = now or datetime.now(UTC)
    order = await _open_order(session, order_id, lock=True)
    if order.collection_todo_id is not None:
        existing = await session.get(Todo, order.collection_todo_id)
        if existing is not None and existing.status in UNFINISHED:
            raise Conflict(f"已有催收待办 {existing.no}，还没有完成")
    if payload.assignee_id is not None:
        active = await session.scalar(
            select(Staff.id).where(
                Staff.id == payload.assignee_id, Staff.status == StaffStatus.ACTIVE
            )
        )
        if active is None:
            raise Unprocessable("指定的员工不存在或已停用")
    assignee = payload.assignee_id or order.assignee_id
    cal = await calendar(session, principal.tenant_id, now)
    due = await session.scalar(select(due_date(cal)).where(Order.id == order.id))
    amount = service.outstanding(order)
    parts = [f"还有 {service.text_money(amount)} 元未收"]
    if due is not None:
        parts.append(f"到期日 {due:%Y-%m-%d}")
    if order.promise_date is not None:
        parts.append(f"客户承诺 {order.promise_date:%Y-%m-%d} 付款")
    type_ = await presets.type_by_code(session, principal.tenant_id, presets.COLLECTION)
    todo = await todo_service.create(
        session,
        ctx.keys,
        todo_service.Draft(
            type=type_,
            title=f"催收：订单 {order.no}",
            detail="，".join(parts),
            source=TodoSource.STAFF,
            created_by_type=ActorType.STAFF,
            created_by=principal.staff_id,
            customer_id=order.customer_id,
            session_id=order.session_id,
            order_id=order.id,
            explicit=assignee is not None,
            assignee_id=assignee,
        ),
        now=now,
    )
    order.collection_todo_id = todo.id
    service.event(
        session,
        order,
        COLLECT_EVENT,
        actor_type=ActorType.STAFF.value,
        actor_id=principal.staff_id,
        payload={
            "todo_id": str(todo.id),
            "assignee_id": str(assignee) if assignee else None,
            "outstanding": service.text_money(amount),
        },
    )
    await session.commit()
    await todo_notify.dispatch(ctx, tenant_id=principal.tenant_id, ids=[todo.id])
    return order


# ---- 对账单（§28.4） ----


async def statement(
    session: AsyncSession,
    cal: Calendar,
    customer_id: uuid.UUID,
    period_from: date,
    period_to: date,
    *,
    generated_by: str,
) -> CustomerStatement:
    """客户对账单：期初未收、期间内的订单和收款、期末未收，以及目前未收清的订单。只算已确认且没有
    取消的订单（取消的订单有收款时已登记退款）。"""
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise NotFound("客户不存在")
    if period_to < period_from:
        raise Unprocessable("结束日期不能早于开始日期")
    tz = cal.tz
    start = datetime.combine(period_from, time.min, tzinfo=tz)
    end = datetime.combine(period_to + timedelta(days=1), time.min, tzinfo=tz)
    booked = and_(Order.customer_id == customer_id, Order.status.in_(queries.RECEIVABLE))
    signed = case(
        (OrderPayment.kind == PaymentKind.PAYMENT.value, OrderPayment.amount),
        else_=-OrderPayment.amount,
    )
    opening_orders = await session.scalar(
        select(func.coalesce(func.sum(Order.total), 0)).where(booked, Order.confirmed_at < start)
    )
    opening_paid = await session.scalar(
        select(func.coalesce(func.sum(signed), 0))
        .select_from(OrderPayment)
        .join(Order, Order.id == OrderPayment.order_id)
        .where(booked, OrderPayment.voided_at.is_(None), OrderPayment.paid_at < start)
    )
    opening = Decimal(opening_orders or 0) - Decimal(opening_paid or 0)

    orders = list(
        (
            await session.scalars(
                select(Order)
                .where(booked, Order.confirmed_at >= start, Order.confirmed_at < end)
                .order_by(Order.confirmed_at)
            )
        ).all()
    )
    items: dict[uuid.UUID, list[OrderItem]] = defaultdict(list)
    if orders:
        for item in await session.scalars(
            select(OrderItem)
            .where(OrderItem.order_id.in_([o.id for o in orders]))
            .order_by(OrderItem.sort)
        ):
            items[item.order_id].append(item)
    payments = (
        await session.execute(
            select(OrderPayment, Order.no)
            .join(Order, Order.id == OrderPayment.order_id)
            .where(
                booked,
                OrderPayment.voided_at.is_(None),
                OrderPayment.paid_at >= start,
                OrderPayment.paid_at < end,
            )
            .order_by(OrderPayment.paid_at, OrderPayment.id)
        )
    ).all()
    kinds = {"order": 0, "payment": 1, "refund": 2}
    entries: list[tuple[date, int, datetime, dict[str, Any]]] = []
    for order in orders:
        assert order.confirmed_at is not None
        entries.append(
            (
                order.confirmed_at.astimezone(tz).date(),
                kinds["order"],
                order.confirmed_at,
                {
                    "kind": "order",
                    "order_id": order.id,
                    "order_no": order.no,
                    "description": service.summary(items[order.id]),
                    "amount": order.total,
                },
            )
        )
    for payment, order_no in payments:
        kind = "refund" if payment.kind == PaymentKind.REFUND.value else "payment"
        description = CHANNEL_LABELS.get(payment.channel, payment.channel)
        if payment.reference_no:
            description += f" · {payment.reference_no}"
        entries.append(
            (
                payment.paid_at.astimezone(tz).date(),
                kinds[kind],
                payment.paid_at,
                {
                    "kind": kind,
                    "order_id": payment.order_id,
                    "order_no": order_no,
                    "description": description,
                    "amount": payment.amount,
                },
            )
        )
    entries.sort(key=lambda e: (e[0], e[1], e[2]))
    balance = opening
    orders_amount = received = refunded = ZERO
    lines: list[StatementLine] = []
    for day, _, _, entry in entries:
        amount = Decimal(entry["amount"])
        if entry["kind"] == "order":
            balance += amount
            orders_amount += amount
        elif entry["kind"] == "payment":
            balance -= amount
            received += amount
        else:
            balance += amount
            refunded += amount
        lines.append(StatementLine(date=day, balance=balance, **entry))
    open_orders = await list_receivables(
        session, cal, ReceivableFilters(customer_id=customer_id), limit=100
    )
    return CustomerStatement(
        customer_id=customer.id,
        customer_name=customer.display_name,
        company=customer.company,
        period_from=period_from,
        period_to=period_to,
        opening=opening,
        orders_amount=orders_amount,
        received=received,
        refunded=refunded,
        closing=balance,
        lines=lines,
        open_orders=open_orders.items,
        generated_at=datetime.now(UTC),
        generated_by=generated_by,
    )
