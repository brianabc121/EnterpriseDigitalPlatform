"""待办与订单报表（设计文档 §24.10、§25.10）。数据范围与待办中心、订单中心一致。

- 待办：数量（按来源、类型）、待确认、逾期、待认领；时效（确认、首次响应、完成、按时完成率）；
  AI 质量（直接确认、修改后确认、驳回及原因、确认后又取消）；每日趋势。
- 订单：AI 下单（采集过订单的会话、提交、确认率、完成率、被修改的比例及原因）；业务（按来源、
  渠道、处理人、商品，审核时长、取消原因、商品缺口）；收款（收款方式、应收与逾期应收）；安全
  （套价识别与回复拦截）；当前待审核积压。
"""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dates import day_bounds
from app.modules.ai.models import AiSecurityEvent
from app.modules.channels.models import ChannelAccount
from app.modules.conversation.models import ChatSession
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.orders import service as order_service
from app.modules.orders.models import (
    PAYMENT_METHOD_LABELS,
    REASON_LABELS,
    Order,
    OrderItem,
    OrderRevision,
    OrderStatus,
    PaymentMethod,
)
from app.modules.orders.models import SOURCE_LABELS as ORDER_SOURCES
from app.modules.products.models import ProductGap
from app.modules.reports.schemas import (
    Bucket,
    OrderAiStats,
    OrderBusiness,
    OrderNow,
    OrderPayments,
    OrderReport,
    OrderSecurity,
    TodoAiQuality,
    TodoDaily,
    TodoReport,
    TodoTimeliness,
    TodoTotals,
)
from app.modules.todos import service as todo_service
from app.modules.todos.models import (
    ACTIVE,
    AI_SOURCES,
    REJECT_LABELS,
    UNFINISHED,
    Todo,
    TodoEvent,
    TodoStatus,
    TodoType,
)
from app.modules.todos.models import SOURCE_LABELS as TODO_SOURCES

TOP = 10
CENT = Decimal("0.01")


def _minutes(seconds: Any) -> float | None:
    return round(float(seconds) / 60, 1) if seconds is not None else None


def _rate(part: int, whole: int) -> float | None:
    return round(part * 100 / whole, 1) if whole else None


def _money(value: Any) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def _range(start: date, end: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return day_bounds(start, tz)[0], day_bounds(end, tz)[1]


# ---- 待办 ----


async def todo_report(
    session: AsyncSession, principal: Principal, start: date, end: date, tz: ZoneInfo
) -> TodoReport:
    lower, upper = _range(start, end, tz)
    now = datetime.now(UTC)
    scope = todo_service.visible_to(principal)
    created = and_(scope, Todo.created_at >= lower, Todo.created_at < upper)

    async def count(*conditions: Any) -> int:
        return int(
            await session.scalar(select(func.count()).select_from(Todo).where(*conditions)) or 0
        )

    closed_in_range = and_(scope, Todo.closed_at >= lower, Todo.closed_at < upper)
    # 待认领：已进入待办列表（不含待确认）、还没有处理人，与待办中心的公共池一致。
    unclaimed = and_(scope, Todo.status.in_(ACTIVE), Todo.assignee_id.is_(None))
    oldest = await session.scalar(select(func.min(Todo.created_at)).where(unclaimed))
    totals = TodoTotals(
        created=await count(created),
        done=await count(closed_in_range, Todo.status == TodoStatus.DONE),
        cancelled=await count(closed_in_range, Todo.status == TodoStatus.CANCELLED),
        rejected=await count(closed_in_range, Todo.status == TodoStatus.REJECTED),
        pending_now=await count(scope, Todo.status == TodoStatus.PENDING),
        overdue_now=await count(scope, Todo.status.in_(UNFINISHED), Todo.due_at < now),
        unclaimed_now=await count(unclaimed),
        oldest_unclaimed_minutes=int((now - oldest).total_seconds() // 60) if oldest else None,
    )

    by_source = [
        Bucket(key=source, label=TODO_SOURCES.get(source, source), count=n)
        for source, n in await session.execute(
            select(Todo.source, func.count())
            .where(created)
            .group_by(Todo.source)
            .order_by(func.count().desc())
        )
    ]
    by_type = [
        Bucket(key=code, label=name, count=n)
        for code, name, n in await session.execute(
            select(TodoType.code, TodoType.name, func.count())
            .select_from(Todo)
            .join(TodoType, TodoType.id == Todo.type_id)
            .where(created)
            .group_by(TodoType.code, TodoType.name)
            .order_by(func.count().desc())
        )
    ]

    seconds = func.extract("epoch", Todo.confirmed_at - Todo.created_at)
    confirm = await session.scalar(
        select(func.avg(seconds)).where(
            created, Todo.source.in_(AI_SOURCES), Todo.confirmed_at.is_not(None)
        )
    )
    activated = func.coalesce(Todo.confirmed_at, Todo.created_at)
    first_response = await session.scalar(
        select(func.avg(func.extract("epoch", Todo.first_response_at - activated))).where(
            created, Todo.first_response_at.is_not(None)
        )
    )
    resolve = await session.scalar(
        select(func.avg(func.extract("epoch", Todo.closed_at - activated))).where(
            closed_in_range, Todo.status == TodoStatus.DONE
        )
    )
    on_time = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Todo.closed_at <= Todo.due_at),
            ).where(closed_in_range, Todo.status == TodoStatus.DONE, Todo.due_at.is_not(None))
        )
    ).one()
    timeliness = TodoTimeliness(
        avg_confirm_minutes=_minutes(confirm),
        avg_first_response_minutes=_minutes(first_response),
        avg_resolve_minutes=_minutes(resolve),
        on_time_rate=_rate(on_time[1], on_time[0]),
    )

    ai = and_(created, Todo.source.in_(AI_SOURCES))
    confirmations = (
        await session.execute(
            select(
                func.count().filter(TodoEvent.payload["modified"].astext == "false"),
                func.count().filter(TodoEvent.payload["modified"].astext == "true"),
            )
            .select_from(TodoEvent)
            .join(Todo, Todo.id == TodoEvent.todo_id)
            .where(ai, TodoEvent.type == "confirmed")
        )
    ).one()
    reasons = [
        Bucket(key=reason or "other", label=REJECT_LABELS.get(reason or "other", "其他"), count=n)
        for reason, n in await session.execute(
            select(Todo.reject_reason, func.count())
            .where(ai, Todo.status == TodoStatus.REJECTED)
            .group_by(Todo.reject_reason)
            .order_by(func.count().desc())
        )
    ]
    quality = TodoAiQuality(
        ai_created=await count(ai),
        confirmed_direct=int(confirmations[0]),
        confirmed_modified=int(confirmations[1]),
        rejected=sum(r.count for r in reasons),
        reject_reasons=reasons,
        cancelled_after_confirm=await count(
            ai, Todo.status == TodoStatus.CANCELLED, Todo.confirmed_at.is_not(None)
        ),
    )

    local_created = func.date(func.timezone(str(tz), Todo.created_at))
    local_closed = func.date(func.timezone(str(tz), Todo.closed_at))
    per_day: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    for day, n in await session.execute(
        select(local_created, func.count()).where(created).group_by(local_created)
    ):
        per_day[day][0] = n
    for day, n in await session.execute(
        select(local_closed, func.count())
        .where(closed_in_range, Todo.status == TodoStatus.DONE)
        .group_by(local_closed)
    ):
        per_day[day][1] = n
    daily = []
    day = start
    while day <= end:
        daily.append(TodoDaily(day=day, created=per_day[day][0], done=per_day[day][1]))
        day += timedelta(days=1)
    return TodoReport(
        start=start,
        end=end,
        totals=totals,
        by_source=by_source,
        by_type=by_type,
        timeliness=timeliness,
        ai=quality,
        daily=daily,
    )


# ---- 订单 ----

CONFIRMED = (
    OrderStatus.CONFIRMED,
    OrderStatus.FULFILLING,
    OrderStatus.SHIPPED,
    OrderStatus.COMPLETED,
)


async def order_report(
    session: AsyncSession, principal: Principal, start: date, end: date, tz: ZoneInfo, today: date
) -> OrderReport:
    lower, upper = _range(start, end, tz)
    now = datetime.now(UTC)
    scope = order_service.visible_to(principal)
    submitted = and_(scope, Order.submitted_at >= lower, Order.submitted_at < upper)

    async def count(*conditions: Any) -> int:
        return int(
            await session.scalar(select(func.count()).select_from(Order).where(*conditions)) or 0
        )

    # AI 下单。
    by_ai = and_(submitted, Order.created_by_type == "ai")
    ai_submitted = await count(by_ai)
    decided = (
        await session.execute(
            select(
                func.count().filter(Order.confirmed_at.is_not(None)),
                func.count().filter(
                    Order.confirmed_at.is_(None), Order.status == OrderStatus.CANCELLED
                ),
                func.count().filter(Order.status == OrderStatus.COMPLETED),
                func.count().filter(Order.modified),
            ).where(by_ai)
        )
    ).one()
    reasons = [
        Bucket(key=reason or "other", label=REASON_LABELS.get(reason or "other", "其他"), count=n)
        for reason, n in await session.execute(
            select(OrderRevision.reason, func.count(func.distinct(OrderRevision.order_id)))
            .select_from(OrderRevision)
            .join(Order, Order.id == OrderRevision.order_id)
            .where(by_ai, OrderRevision.reason.is_not(None))
            .group_by(OrderRevision.reason)
            .order_by(func.count(func.distinct(OrderRevision.order_id)).desc())
        )
    ]
    intent = int(
        await session.scalar(
            select(func.count(func.distinct(Order.session_id))).where(
                scope,
                Order.created_by_type == "ai",
                Order.created_at >= lower,
                Order.created_at < upper,
            )
        )
        or 0
    )
    ai = OrderAiStats(
        intent_sessions=intent,
        submitted=ai_submitted,
        approval_rate=_rate(decided[0], decided[0] + decided[1]),
        completed_rate=_rate(decided[2], ai_submitted),
        modified_rate=_rate(decided[3], ai_submitted),
        modify_reasons=reasons,
    )

    # 业务。
    live = and_(submitted, Order.status != OrderStatus.CANCELLED)
    orders = await count(submitted)
    amount = await session.scalar(select(func.coalesce(func.sum(Order.total), 0)).where(live))
    by_source = [
        Bucket(key=source, label=ORDER_SOURCES.get(source, source), count=n, amount=_money(total))
        for source, n, total in await session.execute(
            select(Order.source, func.count(), func.sum(Order.total))
            .where(live)
            .group_by(Order.source)
            .order_by(func.count().desc())
        )
    ]
    by_channel = [
        Bucket(
            key=str(channel_id or "none"), label=name or "没有会话", count=n, amount=_money(total)
        )
        for channel_id, name, n, total in await session.execute(
            select(
                ChannelAccount.id, ChannelAccount.name, func.count(Order.id), func.sum(Order.total)
            )
            .select_from(Order)
            .outerjoin(ChatSession, ChatSession.id == Order.session_id)
            .outerjoin(ChannelAccount, ChannelAccount.id == ChatSession.channel_account_id)
            .where(live)
            .group_by(ChannelAccount.id, ChannelAccount.name)
            .order_by(func.count(Order.id).desc())
        )
    ]
    by_agent = [
        Bucket(key=str(staff_id or "none"), label=name or "未分派", count=n, amount=_money(total))
        for staff_id, name, n, total in await session.execute(
            select(Staff.id, Staff.display_name, func.count(Order.id), func.sum(Order.total))
            .select_from(Order)
            .outerjoin(Staff, Staff.id == Order.assignee_id)
            .where(live)
            .group_by(Staff.id, Staff.display_name)
            .order_by(func.count(Order.id).desc())
            .limit(TOP)
        )
    ]
    products = [
        Bucket(key=code or name, label=name, count=int(quantity), amount=_money(total))
        for code, name, quantity, total in await session.execute(
            select(
                OrderItem.code,
                OrderItem.name,
                func.sum(OrderItem.quantity),
                func.sum(OrderItem.amount),
            )
            .select_from(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(live)
            .group_by(OrderItem.code, OrderItem.name)
            .order_by(func.sum(OrderItem.quantity).desc())
            .limit(TOP)
        )
    ]
    review = await session.scalar(
        select(func.avg(func.extract("epoch", Order.confirmed_at - Order.submitted_at))).where(
            scope,
            Order.confirmed_at >= lower,
            Order.confirmed_at < upper,
            Order.submitted_at.is_not(None),
        )
    )
    cancels = [
        Bucket(key=reason or "", label=reason or "（没有填写）", count=n)
        for reason, n in await session.execute(
            select(Order.cancel_reason, func.count())
            .where(
                scope,
                Order.cancelled_at >= lower,
                Order.cancelled_at < upper,
                Order.submitted_at.is_not(None),
            )
            .group_by(Order.cancel_reason)
            .order_by(func.count().desc())
            .limit(TOP)
        )
    ]
    gaps = [
        Bucket(key=term, label=term, count=n)
        for term, n in await session.execute(
            select(ProductGap.term, ProductGap.count)
            .where(ProductGap.resolved_at.is_(None))
            .order_by(ProductGap.count.desc(), ProductGap.last_seen_at.desc())
            .limit(TOP)
        )
    ]
    business = OrderBusiness(
        orders=orders,
        amount=_money(amount),
        by_source=by_source,
        by_channel=by_channel,
        by_agent=by_agent,
        top_products=products,
        avg_review_minutes=_minutes(review),
        cancel_reasons=cancels,
        product_gaps=gaps,
    )

    # 收款。
    confirmed_in_range = and_(
        scope, Order.confirmed_at >= lower, Order.confirmed_at < upper, Order.status.in_(CONFIRMED)
    )
    methods = [
        Bucket(
            key=method or "",
            label=PAYMENT_METHOD_LABELS.get(method or "", method or ""),
            count=n,
            amount=_money(total),
        )
        for method, n, total in await session.execute(
            select(Order.payment_method, func.count(), func.sum(Order.total))
            .where(confirmed_in_range, Order.payment_method.is_not(None))
            .group_by(Order.payment_method)
            .order_by(func.count().desc())
        )
    ]
    net = Order.paid_amount - Order.refunded_amount
    unpaid = case((Order.total - net > 0, Order.total - net), else_=0)
    open_orders = and_(scope, Order.status.in_(CONFIRMED))
    receivable = await session.scalar(select(func.coalesce(func.sum(unpaid), 0)).where(open_orders))
    overdue = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(unpaid), 0)).where(
                open_orders,
                Order.payment_method == PaymentMethod.CREDIT.value,
                Order.credit_due_date < today,
                Order.total - net > 0,
            )
        )
    ).one()
    payments = OrderPayments(
        by_method=methods,
        receivable=_money(receivable),
        overdue_receivable=_money(overdue[1]),
        overdue_orders=int(overdue[0]),
    )

    # 安全：套价识别与回复拦截（全企业）。
    events = dict(
        (
            await session.execute(
                select(AiSecurityEvent.kind, func.count())
                .where(AiSecurityEvent.created_at >= lower, AiSecurityEvent.created_at < upper)
                .group_by(AiSecurityEvent.kind)
            )
        ).all()
    )
    security = OrderSecurity(
        price_probes=int(events.get("price_probe", 0)),
        replies_blocked=int(events.get("reply_blocked", 0)),
    )

    pending = and_(scope, Order.status == OrderStatus.PENDING_REVIEW)
    oldest = await session.scalar(select(func.min(Order.submitted_at)).where(pending))
    return OrderReport(
        start=start,
        end=end,
        ai=ai,
        business=business,
        payments=payments,
        security=security,
        now=OrderNow(
            pending_review=await count(pending),
            oldest_pending_minutes=int((now - oldest).total_seconds() // 60) if oldest else None,
        ),
    )
