"""加工（设计文档 §25.11）：工人在"加工"页领取订单，逐个商品标记完成或缺货，全部完成后点
"完成订单"，订单进入订单中心的"待发货"，客服在待办里收到提醒；缺货的订单进入"缺货"，客服收到
"缺货处理"待办（等到货、换货或取消）。

工人只看加工需要的信息：商品、规格、数量、备注、期望时间和客户称呼，看不到金额和收货信息。
有 production:assign 权限的主管可以指派或改派加工人，也可以替工人操作。
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.permissions import Permission
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.notifications import service as notifications
from app.modules.orders import actions, service
from app.modules.orders import settings as order_settings
from app.modules.orders.models import (
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    WorkStatus,
)
from app.modules.orders.schemas import (
    ProductionCounts,
    ProductionItemOut,
    ProductionOrder,
    ProductionPage,
    ProductionView,
    ShortageIn,
)
from app.modules.todos import assign as todo_assign
from app.modules.todos import notify as todo_notify
from app.modules.todos.models import ActorType

STAFF = ActorType.STAFF
NOT_FOUND = "订单不存在，或不在你的加工列表里"
DONE_LIMIT = 50


# ---- 查询 ----


def claimable() -> ColumnElement[bool]:
    """待领取：还没有人领取、没有加工完成的处理中订单，以及满足开工条件的已确认订单（收款条件与
    actions.check_startable 一致：在线收款要收清、预付定金要收到定金、暂欠要主管同意）。"""
    net = Order.paid_amount - Order.refunded_amount
    startable = or_(
        Order.payment_method.is_(None),
        Order.payment_method == PaymentMethod.COD,
        and_(Order.payment_method == PaymentMethod.ONLINE, net >= Order.total),
        and_(
            Order.payment_method == PaymentMethod.DEPOSIT,
            or_(Order.deposit_amount.is_(None), net >= Order.deposit_amount),
        ),
        and_(Order.payment_method == PaymentMethod.CREDIT, Order.credit_approved_at.is_not(None)),
    )
    return and_(
        Order.worker_id.is_(None),
        Order.processed_at.is_(None),
        or_(
            Order.status == OrderStatus.FULFILLING,
            and_(Order.status == OrderStatus.CONFIRMED, startable),
        ),
    )


def _working() -> ColumnElement[bool]:
    return and_(
        Order.worker_id.is_not(None),
        Order.status == OrderStatus.FULFILLING,
        Order.processed_at.is_(None),
    )


def _view(principal: Principal, view: ProductionView) -> ColumnElement[bool]:
    me = principal.staff_id
    match view:
        case "pool":
            return claimable()
        case "mine":
            return and_(_working(), Order.worker_id == me)
        case "done":
            return and_(Order.worker_id == me, Order.processed_at.is_not(None))
        case "all":
            return _working() if principal.has(Permission.PRODUCTION_ASSIGN) else false()
    return false()


def visible(principal: Principal) -> ColumnElement[bool]:
    """加工页能看到的订单：待领取的、自己加工的；主管另外能看到别人加工的。"""
    conditions = [claimable(), Order.worker_id == principal.staff_id]
    if principal.has(Permission.PRODUCTION_ASSIGN):
        conditions.append(
            and_(
                Order.worker_id.is_not(None),
                Order.status.in_((OrderStatus.CONFIRMED, OrderStatus.FULFILLING)),
            )
        )
    return or_(*conditions)


def _ordering(view: ProductionView) -> list[Any]:
    if view == "done":
        return [Order.processed_at.desc()]
    # 期望时间早的先做；没有期望时间的按确认先后。
    return [Order.expected_at.asc().nulls_last(), Order.confirmed_at, Order.created_at]


async def list_orders(
    session: AsyncSession,
    principal: Principal,
    *,
    view: ProductionView,
    q: str | None,
    limit: int,
    offset: int,
) -> ProductionPage:
    conditions = [_view(principal, view)]
    if q and q.strip():
        like = f"%{q.strip()}%"
        conditions.append(
            or_(
                Order.no.ilike(like),
                Order.id.in_(select(OrderItem.order_id).where(OrderItem.name.ilike(like))),
            )
        )
    where = and_(*conditions)
    total = int(await session.scalar(select(func.count()).select_from(Order).where(where)) or 0)
    if view == "done":
        limit = min(limit, DONE_LIMIT)
    rows = list(
        (
            await session.scalars(
                select(Order).where(where).order_by(*_ordering(view)).limit(limit).offset(offset)
            )
        ).all()
    )
    return ProductionPage(items=await outs(session, principal, rows), total=total)


async def counts(session: AsyncSession, principal: Principal) -> ProductionCounts:
    async def count(condition: ColumnElement[bool]) -> int:
        return int(
            await session.scalar(select(func.count()).select_from(Order).where(condition)) or 0
        )

    mine = _view(principal, "mine")
    return ProductionCounts(
        pool=await count(claimable()),
        mine=await count(mine),
        mine_shortage=await count(and_(mine, Order.shortage_at.is_not(None))),
        all=await count(_working()) if principal.has(Permission.PRODUCTION_ASSIGN) else 0,
    )


async def outs(
    session: AsyncSession, principal: Principal, orders: list[Order]
) -> list[ProductionOrder]:
    if not orders:
        return []
    items: dict[uuid.UUID, list[OrderItem]] = {o.id: [] for o in orders}
    for item in await session.scalars(
        select(OrderItem)
        .where(OrderItem.order_id.in_(list(items)))
        .order_by(OrderItem.order_id, OrderItem.sort)
    ):
        items[item.order_id].append(item)
    customers = dict(
        (
            await session.execute(
                select(Customer.id, Customer.display_name).where(
                    Customer.id.in_({o.customer_id for o in orders if o.customer_id})
                )
            )
        ).all()
    )
    staff_ids = {o.worker_id for o in orders if o.worker_id}
    staff_ids |= {i.done_by for rows in items.values() for i in rows if i.done_by}
    staff = dict(
        (
            await session.execute(
                select(Staff.id, Staff.display_name).where(Staff.id.in_(staff_ids))
            )
        ).all()
    )
    pool = {
        row
        for row in await session.scalars(
            select(Order.id).where(Order.id.in_(list(items)), claimable())
        )
    }
    return [_out(principal, o, items[o.id], customers, staff, o.id in pool) for o in orders]


def _out(
    principal: Principal,
    order: Order,
    items: list[OrderItem],
    customers: dict[uuid.UUID, str],
    staff: dict[uuid.UUID, str],
    in_pool: bool,
) -> ProductionOrder:
    mine = order.worker_id == principal.staff_id
    manage = principal.has(Permission.PRODUCTION_ASSIGN)
    working = (
        order.status == OrderStatus.FULFILLING
        and order.worker_id is not None
        and order.processed_at is None
    )
    return ProductionOrder(
        id=order.id,
        no=order.no,
        status=order.status,
        customer_name=customers.get(order.customer_id) if order.customer_id else None,
        expected_at=order.expected_at,
        customer_note=order.customer_note,
        internal_note=order.internal_note,
        items=[
            ProductionItemOut(
                id=i.id,
                code=i.code,
                name=i.name,
                model=i.model,
                spec=i.spec,
                image_url=i.image_url,
                raw_text=i.raw_text,
                quantity=i.quantity,
                work_status=i.work_status,
                done_at=i.done_at,
                done_by_name=staff.get(i.done_by) if i.done_by else None,
                shortage_qty=i.shortage_qty,
                shortage_note=i.shortage_note,
                restock_date=i.restock_date,
            )
            for i in items
        ],
        done_count=sum(1 for i in items if i.work_status == WorkStatus.DONE),
        shortage=order.shortage_at is not None,
        worker_id=order.worker_id,
        worker_name=staff.get(order.worker_id) if order.worker_id else None,
        claimed_at=order.claimed_at,
        processed_at=order.processed_at,
        confirmed_at=order.confirmed_at,
        can_claim=in_pool,
        can_work=working and (mine or manage),
    )


async def get(session: AsyncSession, principal: Principal, order_id: uuid.UUID) -> ProductionOrder:
    order = await session.scalar(select(Order).where(Order.id == order_id, visible(principal)))
    if order is None:
        raise NotFound(NOT_FOUND)
    [out] = await outs(session, principal, [order])
    return out


async def workers(session: AsyncSession) -> list[tuple[uuid.UUID, str]]:
    """可以指派的加工人：有加工权限、启用状态的员工。"""
    ids = await todo_assign.staff_with(session, Permission.PRODUCTION_WORK)
    if not ids:
        return []
    names = dict(
        (await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(ids)))).all()
    )
    return [(i, names[i]) for i in ids if i in names]


# ---- 操作 ----


async def _lock(session: AsyncSession, order_id: uuid.UUID) -> Order:
    order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if order is None:
        raise NotFound(NOT_FOUND)
    return order


async def _working_order(session: AsyncSession, principal: Principal, order_id: uuid.UUID) -> Order:
    """自己加工中的订单（主管可以替工人操作）。"""
    order = await _lock(session, order_id)
    manage = principal.has(Permission.PRODUCTION_ASSIGN)
    if order.worker_id is None or not (order.worker_id == principal.staff_id or manage):
        raise NotFound(NOT_FOUND)
    if order.status != OrderStatus.FULFILLING:
        raise Unprocessable("订单已经不在加工中（可能已发货、完成或取消）")
    if order.processed_at is not None:
        raise Unprocessable("这个订单已经加工完成")
    return order


async def _item(session: AsyncSession, order: Order, item_id: uuid.UUID) -> OrderItem:
    item = await session.scalar(
        select(OrderItem)
        .where(OrderItem.id == item_id, OrderItem.order_id == order.id)
        .with_for_update()
    )
    if item is None:
        raise NotFound("订单里没有这个商品")
    return item


async def _take(
    session: AsyncSession,
    principal: Principal,
    order: Order,
    worker_id: uuid.UUID,
    now: datetime,
) -> None:
    """交给某个工人加工：已确认的订单同时开始处理（收款条件与"开始处理"相同）。"""
    if order.status == OrderStatus.CONFIRMED:
        await actions.begin(session, principal, order)
    order.worker_id, order.claimed_at = worker_id, now


async def claim(
    ctx: AppContext, session: AsyncSession, principal: Principal, order_id: uuid.UUID
) -> Order:
    """领取：待领取的订单交给自己加工。"""
    order = await _lock(session, order_id)
    me = principal.staff_id
    if order.worker_id == me and order.processed_at is None:
        return order
    if order.worker_id is not None:
        name = await session.scalar(select(Staff.display_name).where(Staff.id == order.worker_id))
        raise Conflict(f"这个订单已经由{name or '其他工人'}领取")
    if order.processed_at is not None or order.status not in (
        OrderStatus.CONFIRMED,
        OrderStatus.FULFILLING,
    ):
        raise Unprocessable("这个订单不在待领取的列表里")
    now = service.utcnow()
    await _take(session, principal, order, me, now)
    service.event(session, order, "claimed", actor_type=STAFF, actor_id=me, payload={})
    await session.commit()
    return order


async def release(
    ctx: AppContext, session: AsyncSession, principal: Principal, order_id: uuid.UUID
) -> Order:
    """放弃：退回待领取（已标记的完成和缺货保留）。"""
    order = await _working_order(session, principal, order_id)
    order.worker_id = order.claimed_at = None
    service.event(session, order, "released", actor_type=STAFF, actor_id=principal.staff_id)
    await session.commit()
    return order


async def assign(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    worker_id: uuid.UUID | None,
) -> Order:
    """主管指派或改派加工人（worker_id 为空时退回待领取）。"""
    order = await _lock(session, order_id)
    if order.processed_at is not None or order.status not in (
        OrderStatus.CONFIRMED,
        OrderStatus.FULFILLING,
    ):
        raise Unprocessable("只有待加工、加工中的订单可以指派加工人")
    if worker_id == order.worker_id:
        return order
    now = service.utcnow()
    me = principal.staff_id
    if worker_id is None:
        order.worker_id = order.claimed_at = None
        service.event(session, order, "released", actor_type=STAFF, actor_id=me)
        await session.commit()
        return order
    if worker_id not in await todo_assign.staff_with(session, Permission.PRODUCTION_WORK):
        raise Unprocessable("只能指派给有加工权限、启用状态的员工")
    name = await session.scalar(select(Staff.display_name).where(Staff.id == worker_id))
    await _take(session, principal, order, worker_id, now)
    service.event(
        session,
        order,
        "worker_assigned",
        actor_type=STAFF,
        actor_id=me,
        payload={"worker": name},
    )
    notifications.add(
        session,
        order.tenant_id,
        [worker_id],
        kind="production_assigned",
        title=f"订单 {order.no} 交给你加工",
        link=f"/production?order={order.id}",
    )
    await session.commit()
    return order


async def set_done(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    done: bool,
) -> Order:
    """标记一个商品加工完成，或撤销完成。"""
    order = await _working_order(session, principal, order_id)
    item = await _item(session, order, item_id)
    me = principal.staff_id
    if done:
        if item.work_status == WorkStatus.OUT_OF_STOCK:
            raise Unprocessable("缺货的商品要先登记到货，才能标记完成")
        if item.work_status == WorkStatus.DONE:
            return order
        item.work_status = WorkStatus.DONE.value
        item.done_at, item.done_by = service.utcnow(), me
        kind = "item_done"
    else:
        if item.work_status != WorkStatus.DONE:
            return order
        item.work_status = WorkStatus.PENDING.value
        item.done_at = item.done_by = None
        kind = "item_reopened"
    service.event(
        session,
        order,
        kind,
        actor_type=STAFF,
        actor_id=me,
        payload={"item_id": str(item.id), "name": service.item_label(item)},
    )
    await session.commit()
    return order


async def mark_shortage(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: ShortageIn,
) -> Order:
    """登记（或修改）一个商品缺货：缺多少、说明、预计到货日期。订单进入"缺货"，客服收到提醒。"""
    order = await _working_order(session, principal, order_id)
    item = await _item(session, order, item_id)
    if payload.quantity is not None and payload.quantity > item.quantity:
        raise Unprocessable(f"缺货数量不能超过订购数量 {item.quantity}")
    now = service.utcnow()
    me = principal.staff_id
    first = item.work_status != WorkStatus.OUT_OF_STOCK
    item.work_status = WorkStatus.OUT_OF_STOCK.value
    item.done_at = item.done_by = None
    item.shortage_qty = payload.quantity
    item.shortage_note = (payload.note or "").strip() or None
    item.restock_date = payload.restock_date
    if first:
        item.shortage_at, item.shortage_by = now, me
    service.event(
        session,
        order,
        "shortage",
        actor_type=STAFF,
        actor_id=me,
        payload={
            "item_id": str(item.id),
            "name": service.item_label(item),
            "quantity": item.shortage_qty or item.quantity,
            "note": item.shortage_note,
            "restock_date": _iso(item.restock_date),
            "edited": not first,
        },
    )
    items = await service.load_items(session, order.id)
    todo = await service.sync_shortage(session, ctx.keys, order, items, actor_id=me, now=now)
    await session.commit()
    if todo is not None:
        await todo_notify.dispatch(ctx, tenant_id=order.tenant_id, ids=[todo.id])
    return order


async def restock(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order: Order,
    item_id: uuid.UUID,
) -> Order:
    """登记到货：缺货的商品回到待加工；订单没有其他缺货时离开"缺货"，"缺货处理"待办随之完成。
    工人和客服都可以登记（由调用方检查权限并锁定订单）。"""
    item = await _item(session, order, item_id)
    if item.work_status != WorkStatus.OUT_OF_STOCK:
        return order
    now = service.utcnow()
    me = principal.staff_id
    item.work_status = WorkStatus.PENDING.value
    item.shortage_qty = item.shortage_note = item.restock_date = None
    item.shortage_at = item.shortage_by = None
    service.event(
        session,
        order,
        "restocked",
        actor_type=STAFF,
        actor_id=me,
        payload={"item_id": str(item.id), "name": service.item_label(item)},
    )
    items = await service.load_items(session, order.id)
    await service.sync_shortage(session, ctx.keys, order, items, actor_id=me, now=now)
    await session.commit()
    return order


async def worker_restock(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    item_id: uuid.UUID,
) -> Order:
    order = await _working_order(session, principal, order_id)
    return await restock(ctx, session, principal, order, item_id)


async def complete(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    order_id: uuid.UUID,
    *,
    mark_all: bool,
) -> Order:
    """完成订单（加工完成）：订单进入订单中心的"待发货"，客服在待办里收到提醒。有缺货的商品时
    不能完成；还有没标记的商品时，mark_all 为真表示一并标记完成。"""
    order = await _working_order(session, principal, order_id)
    items = await service.load_items(session, order.id)
    if service.shortage_items(items):
        raise Unprocessable("有缺货的商品，到货或由客服换货后才能完成")
    pending = [i for i in items if i.work_status == WorkStatus.PENDING]
    if pending and not mark_all:
        raise Unprocessable(f"还有 {len(pending)} 个商品没有标记完成")
    now = service.utcnow()
    me = principal.staff_id
    for item in pending:
        item.work_status = WorkStatus.DONE.value
        item.done_at, item.done_by = now, me
    order.processed_at, order.processed_by = now, me
    settings = await order_settings.load(session, order.tenant_id)
    service.event(
        session,
        order,
        "processed",
        actor_type=STAFF,
        actor_id=me,
        payload={"shipping": settings.shipping_enabled},
        public=True,
    )
    todo = await service.open_ship_todo(
        session, ctx.keys, order, items, settings, actor_id=me, now=now
    )
    await session.commit()
    await todo_notify.dispatch(ctx, tenant_id=order.tenant_id, ids=[todo.id])
    return order


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None
