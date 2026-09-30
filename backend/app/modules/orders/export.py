"""订单导出（CSV，设计文档 §25.7）。

需要 order:export 权限并再次输入密码，只导出数据范围内、符合订单中心筛选条件的订单。收货信息在
员工有 customer:view_sensitive 权限时导出明文，否则导出掩码；有 product:view_cost 权限时另外导出
成本合计。每次导出记审计（条数、是否含明文、是否含成本）。
"""

import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.csvfile import BOM, line
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.orders import queries, service
from app.modules.orders.models import (
    PAYMENT_METHOD_LABELS,
    PAYMENT_STATUS_LABELS,
    SOURCE_LABELS,
    STATUS_LABELS,
    Order,
    OrderItem,
)
from app.modules.todos import sla

BATCH = 200
HEADER = [
    "订单号",
    "状态",
    "来源",
    "客户",
    "商品",
    "商品金额",
    "优惠",
    "合计",
    "收款方式",
    "收款状态",
    "已收",
    "已退",
    "未收",
    "收货人",
    "联系电话",
    "收货地址",
    "处理人",
    "物流公司",
    "物流单号",
    "企业系统单号",
    "修改过",
    "下单时间",
    "确认时间",
    "完成时间",
]
COST = "成本合计"


async def count(session: AsyncSession, where: ColumnElement[bool]) -> int:
    return int(await session.scalar(select(func.count()).select_from(Order).where(where)) or 0)


def _time(value: datetime | None, tz: Any) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M") if value else ""


def _cost(items: list[OrderItem]) -> str:
    costs = [i.cost_price * i.quantity for i in items if i.cost_price is not None]
    if not costs:
        return ""
    return service.text_money(sum(costs, Decimal("0"))) or ""


async def rows(
    ctx: AppContext,
    principal: Principal,
    where: ColumnElement[bool],
    *,
    plaintext: bool,
    cost: bool,
) -> AsyncIterator[bytes]:
    """逐批生成 CSV（按下单时间）。使用自己的数据库会话。"""
    yield (BOM + line([*HEADER, *([COST] if cost else [])])).encode()
    last: tuple[datetime, uuid.UUID] | None = None
    async with ctx.db.tenant_session(principal.tenant_id) as session:
        tz = sla.tz_of(await sla.business_hours(session))
        while True:
            query = select(Order).where(where).order_by(Order.created_at, Order.id).limit(BATCH)
            if last is not None:
                query = query.where(tuple_(Order.created_at, Order.id) > last)
            batch = (await session.scalars(query)).all()
            if not batch:
                return
            items: dict[uuid.UUID, list[OrderItem]] = defaultdict(list)
            for item in await session.scalars(
                select(OrderItem)
                .where(OrderItem.order_id.in_([o.id for o in batch]))
                .order_by(OrderItem.sort)
            ):
                items[item.order_id].append(item)
            customers = await queries.names(
                session, Customer, Customer.display_name, (o.customer_id for o in batch)
            )
            staff = await queries.names(
                session, Staff, Staff.display_name, (o.assignee_id for o in batch)
            )
            lines: list[str] = []
            for order in batch:
                receiver = (
                    await service.reveal_receiver(ctx.keys, principal.tenant_id, order.receiver)
                    if plaintext
                    else service.masked_receiver(order.receiver)
                )
                values: list[object] = [
                    order.no,
                    STATUS_LABELS.get(order.status, order.status),
                    SOURCE_LABELS.get(order.source, order.source),
                    customers.get(order.customer_id) if order.customer_id else "",
                    service.summary(items[order.id]),
                    service.text_money(order.items_amount),
                    service.text_money(order.discount),
                    service.text_money(order.total),
                    PAYMENT_METHOD_LABELS.get(order.payment_method or "", ""),
                    PAYMENT_STATUS_LABELS.get(order.payment_status, order.payment_status),
                    service.text_money(order.paid_amount),
                    service.text_money(order.refunded_amount),
                    service.text_money(service.outstanding(order)),
                    receiver.get("name", ""),
                    receiver.get("phone", ""),
                    receiver.get("address", ""),
                    staff.get(order.assignee_id) if order.assignee_id else "",
                    order.shipping_company or "",
                    order.tracking_no or "",
                    order.external_no or "",
                    "是" if order.modified else "",
                    _time(order.created_at, tz),
                    _time(order.confirmed_at, tz),
                    _time(order.completed_at, tz),
                ]
                if cost:
                    values.append(_cost(items[order.id]))
                lines.append(line(values))
            yield "".join(lines).encode()
            last = (batch[-1].created_at, batch[-1].id)
