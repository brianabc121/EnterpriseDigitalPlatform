"""盈利报表的计算（设计文档 §30.3）。

- 算哪些订单：已确认及之后（已确认、处理中、已发货、已完成），按确认日期（租户时区）计入期间；
  已取消的不算。
- 销售收入 = 订单合计；销售成本 = 商品行数量 × 成本价（下单时记在订单上的，没有时用商品库现在的，
  都没有按 0 并计入成本缺失）；按商品看时优惠按行金额的比例分摊。
- 费用、其他收入来自收支登记（按发生日期）。净利润 = 毛利 + 其他收入 − 费用。
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, Select, and_, case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.channels.models import ChannelAccount
from app.modules.conversation.models import ChatSession
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff
from app.modules.orders.models import SOURCE_LABELS, Order, OrderItem, OrderPayment, PaymentKind
from app.modules.orders.queries import RECEIVABLE
from app.modules.products.models import Product
from app.modules.profit import periods
from app.modules.profit.models import EntryKind, ProfitEntry
from app.modules.profit.periods import Period
from app.modules.profit.schemas import (
    BreakdownRow,
    CategoryAmount,
    CostGap,
    CostGapProduct,
    Dimension,
    Direction,
    MonthRow,
    PeriodOut,
    ProfitBreakdown,
    ProfitSummary,
    ProfitTrend,
    SortKey,
    Statement,
)
from app.modules.todos import sla

CENT = Decimal("0.01")
ZERO = Decimal("0")
GAP_PRODUCTS = 10

# 商品行的成本价：下单时记在订单上的，没有时用商品库现在的。
UNIT_COST = func.coalesce(OrderItem.cost_price, Product.cost_price)
LINE_COST = OrderItem.quantity * func.coalesce(UNIT_COST, 0)
LINE_MISSING = case((UNIT_COST.is_(None), 1), else_=0)
# 商品行分摊优惠后的收入：行金额 × 合计 ÷ 商品金额。
LINE_REVENUE = case(
    (Order.items_amount > 0, OrderItem.amount * Order.total / Order.items_amount), else_=0
)


def money(value: Any) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def rate(part: Decimal, whole: Decimal) -> float | None:
    """百分比，一位小数；分母为 0 时为空。"""
    return round(float(part * 100 / whole), 1) if whole else None


async def tenant_zone(session: AsyncSession) -> ZoneInfo:
    """租户时区：工作时间设置里的时区（§24.4），和应收账款一致。"""
    return sla.tz_of(await sla.business_hours(session))


def booked(tz: ZoneInfo, period: Period) -> ColumnElement[bool]:
    """期间内确认、没有取消的订单。"""
    lower, upper = period.bounds(tz)
    return and_(
        Order.status.in_(RECEIVABLE), Order.confirmed_at >= lower, Order.confirmed_at < upper
    )


def lines(*columns: Any) -> Select[Any]:
    """订单商品行（带订单和商品库的商品）。"""
    return (
        select(*columns)
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .outerjoin(Product, Product.id == OrderItem.product_id)
    )


@dataclass(frozen=True)
class Gross:
    orders: int
    revenue: Decimal
    cost: Decimal

    @property
    def profit(self) -> Decimal:
        return self.revenue - self.cost


async def gross(session: AsyncSession, tz: ZoneInfo, period: Period) -> Gross:
    where = booked(tz, period)
    orders, revenue = (
        await session.execute(
            select(func.count(Order.id), func.coalesce(func.sum(Order.total), 0)).where(where)
        )
    ).one()
    cost = await session.scalar(lines(func.coalesce(func.sum(LINE_COST), 0)).where(where))
    return Gross(int(orders), money(revenue), money(cost))


async def entry_totals(session: AsyncSession, period: Period) -> dict[str, list[CategoryAmount]]:
    found: dict[str, list[CategoryAmount]] = {EntryKind.EXPENSE: [], EntryKind.INCOME: []}
    total = func.sum(ProfitEntry.amount)
    for kind, category, amount, count in await session.execute(
        select(ProfitEntry.kind, ProfitEntry.category, total, func.count())
        .where(ProfitEntry.occurred_on >= period.start, ProfitEntry.occurred_on <= period.end)
        .group_by(ProfitEntry.kind, ProfitEntry.category)
        .order_by(total.desc(), ProfitEntry.category)
    ):
        found.setdefault(kind, []).append(
            CategoryAmount(category=category, amount=money(amount), count=int(count))
        )
    return found


async def statement(session: AsyncSession, tz: ZoneInfo, period: Period) -> Statement:
    base = await gross(session, tz, period)
    entries = await entry_totals(session, period)
    income = sum((c.amount for c in entries[EntryKind.INCOME]), ZERO)
    expenses = sum((c.amount for c in entries[EntryKind.EXPENSE]), ZERO)
    net = base.profit + income - expenses
    return Statement(
        period=PeriodOut(start=period.start, end=period.end),
        orders=base.orders,
        revenue=base.revenue,
        cost=base.cost,
        gross_profit=base.profit,
        gross_margin=rate(base.profit, base.revenue),
        other_income=money(income),
        expenses=money(expenses),
        net_profit=money(net),
        net_margin=rate(net, base.revenue),
        income_by_category=entries[EntryKind.INCOME],
        expense_by_category=entries[EntryKind.EXPENSE],
    )


async def cost_gap(session: AsyncSession, tz: ZoneInfo, period: Period) -> CostGap:
    where = and_(booked(tz, period), UNIT_COST.is_(None))
    revenue = func.coalesce(func.sum(LINE_REVENUE), 0)
    count, total = (await session.execute(lines(func.count(), revenue).where(where))).one()
    products = [
        CostGapProduct(
            product_id=product_id,
            name=name or "",
            code=code,
            lines=int(n),
            revenue=money(amount),
        )
        for product_id, name, code, n, amount in await session.execute(
            lines(
                OrderItem.product_id,
                func.max(func.coalesce(Product.name, OrderItem.name)),
                func.max(Product.code),
                func.count(),
                revenue,
            )
            .where(where)
            .group_by(OrderItem.product_id)
            .order_by(revenue.desc(), func.count().desc())
            .limit(GAP_PRODUCTS)
        )
    ]
    return CostGap(lines=int(count), revenue=money(total), products=products)


async def summary(
    session: AsyncSession, tz: ZoneInfo, period: Period, shift: int | None = None
) -> ProfitSummary:
    lower, upper = period.bounds(tz)
    signed = case(
        (OrderPayment.kind == PaymentKind.PAYMENT.value, OrderPayment.amount),
        else_=-OrderPayment.amount,
    )
    collected = await session.scalar(
        select(func.coalesce(func.sum(signed), 0)).where(
            OrderPayment.voided_at.is_(None),
            OrderPayment.paid_at >= lower,
            OrderPayment.paid_at < upper,
        )
    )
    outstanding = func.greatest(Order.total - (Order.paid_amount - Order.refunded_amount), 0)
    uncollected = await session.scalar(
        select(func.coalesce(func.sum(outstanding), 0)).where(booked(tz, period))
    )
    return ProfitSummary(
        timezone=tz.key,
        current=await statement(session, tz, period),
        previous=await statement(session, tz, periods.previous(period, shift)),
        last_year=await statement(session, tz, periods.last_year(period)),
        collected=money(collected),
        uncollected=money(uncollected),
        cost_gap=await cost_gap(session, tz, period),
    )


async def trend(session: AsyncSession, tz: ZoneInfo, end: date) -> ProfitTrend:
    """截至 end 所在月的 12 个月；最后一个月到 end 为止。"""
    months = periods.trend_months(end)
    span = Period(months[0], end)
    where = booked(tz, span)
    local = func.to_char(func.timezone(literal(tz.key), Order.confirmed_at), "YYYY-MM")
    orders: dict[str, tuple[int, Decimal]] = {
        key: (int(n), money(total))
        for key, n, total in await session.execute(
            select(local, func.count(Order.id), func.sum(Order.total)).where(where).group_by(local)
        )
    }
    costs: dict[str, Decimal] = {
        key: money(total)
        for key, total in await session.execute(
            lines(local, func.sum(LINE_COST)).where(where).group_by(local)
        )
    }
    day = func.to_char(ProfitEntry.occurred_on, "YYYY-MM")
    entries: dict[tuple[str, str], Decimal] = {
        (key, kind): money(total)
        for key, kind, total in await session.execute(
            select(day, ProfitEntry.kind, func.sum(ProfitEntry.amount))
            .where(ProfitEntry.occurred_on >= span.start, ProfitEntry.occurred_on <= span.end)
            .group_by(day, ProfitEntry.kind)
        )
    }
    rows = []
    for first in months:
        key = periods.month_key(first)
        count, revenue = orders.get(key, (0, ZERO))
        cost = costs.get(key, ZERO)
        income = entries.get((key, EntryKind.INCOME), ZERO)
        expenses = entries.get((key, EntryKind.EXPENSE), ZERO)
        profit = revenue - cost
        rows.append(
            MonthRow(
                month=key,
                start=first,
                end=min(periods.month_end(first), end),
                orders=count,
                revenue=revenue,
                cost=cost,
                gross_profit=profit,
                gross_margin=rate(profit, revenue),
                other_income=income,
                expenses=expenses,
                net_profit=profit + income - expenses,
            )
        )
    return ProfitTrend(timezone=tz.key, months=rows)


# ---- 毛利分析（§30.4） ----


def _ordering(sort: SortKey, direction: Direction, revenue: Any, profit: Any) -> Any:
    column = {
        "profit": profit,
        "revenue": revenue,
        "margin": profit / func.nullif(revenue, 0),
    }[sort]
    return column.desc().nulls_last() if direction == "desc" else column.asc().nulls_last()


def _row(
    *,
    key: Any,
    label: str,
    detail: str,
    orders: int,
    revenue: Any,
    cost: Any,
    missing: Any,
    total_profit: Decimal,
    **extra: Any,
) -> BreakdownRow:
    revenue, cost = money(revenue), money(cost)
    profit = revenue - cost
    return BreakdownRow(
        key=str(key) if key is not None else "none",
        label=label,
        detail=detail,
        orders=orders,
        revenue=revenue,
        cost=cost,
        gross_profit=profit,
        gross_margin=rate(profit, revenue),
        share=rate(profit, total_profit) if total_profit > 0 else None,
        missing_cost=int(missing or 0),
        **extra,
    )


async def _by_product(
    session: AsyncSession,
    where: ColumnElement[bool],
    sort: SortKey,
    direction: Direction,
    limit: int,
    offset: int,
    total_profit: Decimal,
) -> tuple[int, list[BreakdownRow]]:
    detail = func.concat_ws(
        " · ",
        func.nullif(Product.code, ""),
        func.nullif(Product.model, ""),
        func.nullif(Product.spec, ""),
    )
    grouped = (
        lines(
            OrderItem.product_id.label("key"),
            func.max(func.coalesce(Product.name, OrderItem.name)).label("label"),
            func.max(detail).label("detail"),
            func.count(func.distinct(Order.id)).label("orders"),
            func.sum(OrderItem.quantity).label("quantity"),
            func.round(func.sum(LINE_REVENUE), 2).label("revenue"),
            func.coalesce(func.sum(LINE_COST), 0).label("cost"),
            func.sum(LINE_MISSING).label("missing"),
        )
        .where(where)
        .group_by(OrderItem.product_id)
        .subquery()
    )
    profit = grouped.c.revenue - grouped.c.cost
    total = int(await session.scalar(select(func.count()).select_from(grouped)) or 0)
    rows = []
    for row in await session.execute(
        select(grouped)
        .order_by(
            _ordering(sort, direction, grouped.c.revenue, profit),
            grouped.c.label,
            grouped.c.key,
        )
        .limit(limit)
        .offset(offset)
    ):
        quantity = int(row.quantity or 0)
        revenue, cost = money(row.revenue), money(row.cost)
        rows.append(
            _row(
                key=row.key,
                label=row.label or "",
                detail=row.detail or "",
                orders=int(row.orders),
                revenue=revenue,
                cost=cost,
                missing=row.missing,
                total_profit=total_profit,
                quantity=quantity,
                avg_price=money(revenue / quantity) if quantity else None,
                unit_cost=money(cost / quantity) if quantity else None,
            )
        )
    return total, rows


async def _by_order(
    session: AsyncSession,
    tz: ZoneInfo,
    where: ColumnElement[bool],
    by: Dimension,
    sort: SortKey,
    direction: Direction,
    limit: int,
    offset: int,
    total_profit: Decimal,
) -> tuple[int, list[BreakdownRow]]:
    per_order = (
        lines(
            OrderItem.order_id.label("order_id"),
            func.sum(LINE_COST).label("cost"),
            func.sum(LINE_MISSING).label("missing"),
        )
        .where(where)
        .group_by(OrderItem.order_id)
        .subquery()
    )
    cost = func.coalesce(func.sum(per_order.c.cost), 0)
    missing = func.coalesce(func.sum(per_order.c.missing), 0)
    revenue = func.coalesce(func.sum(Order.total), 0)
    base = (
        select()
        .select_from(Order)
        .outerjoin(per_order, per_order.c.order_id == Order.id)
        .outerjoin(Customer, Customer.id == Order.customer_id)
        .where(where)
    )
    extra: list[Any] = []
    if by == "customer":
        key: Any = Order.customer_id
        label: Any = func.coalesce(func.max(Customer.display_name), "未关联客户")
        detail: Any = func.coalesce(func.max(Customer.company), "")
    elif by == "assignee":
        base = base.outerjoin(Staff, Staff.id == Order.assignee_id)
        key = Order.assignee_id
        label = func.coalesce(func.max(Staff.display_name), "未分派")
        detail = literal("")
    elif by == "source":
        key = Order.source
        label = Order.source
        detail = literal("")
    elif by == "channel":
        base = base.outerjoin(ChatSession, ChatSession.id == Order.session_id).outerjoin(
            ChannelAccount, ChannelAccount.id == ChatSession.channel_account_id
        )
        key = ChannelAccount.id
        label = func.coalesce(func.max(ChannelAccount.name), "没有会话")
        detail = literal("")
    else:  # 按订单
        base = base.outerjoin(Staff, Staff.id == Order.assignee_id)
        key = Order.id
        label = func.max(Order.no)
        detail = func.coalesce(func.max(Customer.display_name), "未关联客户")
        extra = [
            func.max(Order.confirmed_at).label("confirmed_at"),
            func.max(Staff.display_name).label("assignee_name"),
        ]
    grouped = (
        base.add_columns(
            key.label("key"),
            label.label("label"),
            detail.label("detail"),
            func.count(Order.id).label("orders"),
            revenue.label("revenue"),
            cost.label("cost"),
            missing.label("missing"),
            *extra,
        )
        .group_by(key)
        .subquery()
    )
    profit = grouped.c.revenue - grouped.c.cost
    total = int(await session.scalar(select(func.count()).select_from(grouped)) or 0)
    rows = []
    for row in await session.execute(
        select(grouped)
        .order_by(
            _ordering(sort, direction, grouped.c.revenue, profit),
            grouped.c.label,
            grouped.c.key,
        )
        .limit(limit)
        .offset(offset)
    ):
        label_text = str(row.label or "")
        if by == "source":
            label_text = SOURCE_LABELS.get(label_text, label_text)
        fields: dict[str, Any] = {}
        if by == "order":
            confirmed = row.confirmed_at
            fields = {
                "order_id": row.key,
                "confirmed_on": confirmed.astimezone(tz).date() if confirmed else None,
                "assignee_name": row.assignee_name,
            }
        rows.append(
            _row(
                key=row.key,
                label=label_text,
                detail=str(row.detail or ""),
                orders=int(row.orders),
                revenue=row.revenue,
                cost=row.cost,
                missing=row.missing,
                total_profit=total_profit,
                **fields,
            )
        )
    return total, rows


async def breakdown(
    session: AsyncSession,
    tz: ZoneInfo,
    period: Period,
    by: Dimension,
    *,
    sort: SortKey = "profit",
    direction: Direction = "desc",
    limit: int = 20,
    offset: int = 0,
) -> ProfitBreakdown:
    where = booked(tz, period)
    total_profit = (await gross(session, tz, period)).profit
    if by == "product":
        total, rows = await _by_product(
            session, where, sort, direction, limit, offset, total_profit
        )
    else:
        total, rows = await _by_order(
            session, tz, where, by, sort, direction, limit, offset, total_profit
        )
    return ProfitBreakdown(by=by, total=total, gross_profit=total_profit, items=rows)
