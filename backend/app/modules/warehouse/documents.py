"""仓库单据（设计文档 §25.13）：领料单和入库单的开单、修改、确认、退回和作废，按配方预填。

- 开单：工人给自己加工的订单开领料单（主管可以代开）；订单的入库单在加工页"完成加工"时开（见
  orders.production.complete）；有仓库权限的员工还可以开不关联订单的单据（例如备货生产）。
- 仓管确认后才修改库存：领料单扣减材料库存（不够也可以领，库存变成负数，提示盘点），入库单增加
  成品库存；订单的入库单确认后订单加工完成，进入"待发货"。仓管自己开的单据、或者设置为不需要
  确认时，开单即确认。
- 待确认、已退回的单据可以修改后重新提交，也可以作废；订单取消时它还没生效的单据随之作废。
- 锁的顺序与订单操作一致：先订单、再单据、最后按 ID 顺序锁商品。
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ColumnElement, delete, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.db.counters import next_number
from app.modules.history import service as history
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.notifications import service as notifications
from app.modules.orders import service as order_service
from app.modules.orders.models import Order, OrderItem, OrderStatus
from app.modules.products import stock
from app.modules.products.models import Product, ProductKind, ProductMaterial, StockKind
from app.modules.todos import assign as todo_assign
from app.modules.todos import sla
from app.modules.todos.models import ActorType, Todo
from app.modules.warehouse import history as document_history
from app.modules.warehouse import settings as warehouse_settings
from app.modules.warehouse import usage
from app.modules.warehouse.models import (
    KIND_LABELS,
    OPEN,
    PREFIXES,
    STATUS_LABELS,
    DocumentKind,
    DocumentStatus,
    StockDocument,
    StockDocumentLine,
)
from app.modules.warehouse.schemas import (
    DocumentBrief,
    DocumentLineIn,
    DocumentLineOut,
    DocumentOut,
    DraftBasisValue,
    DraftItem,
    DraftItemBasisValue,
    DraftLine,
    DraftSource,
)

STAFF = ActorType.STAFF
ZERO = Decimal(0)
NOT_FOUND = "单据不存在，或没有权限查看"
ORDER_NOT_FOUND = "订单不存在，或不在你的加工列表里"


def is_keeper(principal: Principal) -> bool:
    return principal.has(Permission.WAREHOUSE_CONFIRM)


def manages(principal: Principal) -> bool:
    """仓库的员工：可以查看全部单据、开不关联订单的单据。"""
    return principal.has(Permission.INVENTORY_MANAGE) or is_keeper(principal)


def visible(principal: Principal) -> ColumnElement[bool]:
    """能看到的单据：仓库的员工看全部；其他人看自己开的、自己加工的订单的；主管看订单的。"""
    if manages(principal):
        return true()
    me = principal.staff_id
    conditions: list[ColumnElement[bool]] = [StockDocument.created_by == me]
    if principal.has(Permission.PRODUCTION_WORK):
        conditions.append(StockDocument.order_id.in_(select(Order.id).where(Order.worker_id == me)))
    if principal.has(Permission.PRODUCTION_ASSIGN):
        conditions.append(StockDocument.order_id.is_not(None))
    return or_(*conditions)


# ---- 开单 ----


@dataclass(frozen=True)
class Line:
    product: Product
    quantity: Decimal
    planned: Decimal | None


async def check_lines(
    session: AsyncSession, kind: DocumentKind, lines: list[DocumentLineIn]
) -> list[Line]:
    """领料单只能是材料，入库单只能是成品；数量大于 0（成品是整数）；同一个商品只能有一行。"""
    if not lines:
        raise Unprocessable("单据至少要有一行")
    ids = [line.product_id for line in lines]
    if len(set(ids)) != len(ids):
        raise Unprocessable("同一个商品只能有一行，请合并数量")
    products = {p.id: p for p in await session.scalars(select(Product).where(Product.id.in_(ids)))}
    want = ProductKind.MATERIAL if kind == DocumentKind.REQUISITION else ProductKind.GOODS
    result: list[Line] = []
    for line in lines:
        product = products.get(line.product_id)
        if product is None:
            raise Unprocessable("商品不存在")
        if product.kind != want:
            raise Unprocessable(
                f"「{product.name}」不是材料，领料单只能领材料"
                if kind == DocumentKind.REQUISITION
                else f"「{product.name}」不是成品，入库单只能入成品"
            )
        if line.quantity <= 0:
            raise Unprocessable(f"「{product.name}」的数量要大于 0")
        stock.check_quantity(product, line.quantity)
        result.append(Line(product, line.quantity, line.planned))
    return result


def _add_lines(session: AsyncSession, document: StockDocument, lines: list[Line]) -> None:
    for sort, line in enumerate(lines):
        product = line.product
        session.add(
            StockDocumentLine(
                tenant_id=document.tenant_id,
                document_id=document.id,
                product_id=product.id,
                code=product.code,
                name=product.name,
                spec=product.spec,
                unit=product.unit,
                planned=line.planned,
                quantity=line.quantity,
                sort=sort,
            )
        )


async def lock_order(session: AsyncSession, principal: Principal, order_id: uuid.UUID) -> Order:
    """给订单开单：自己加工的订单（主管、仓库的员工可以代开）；订单要在加工中、还没加工完成。"""
    order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if order is None:
        raise NotFound(ORDER_NOT_FOUND)
    mine = order.worker_id is not None and order.worker_id == principal.staff_id
    if not (
        (principal.has(Permission.PRODUCTION_WORK) and mine)
        or principal.has(Permission.PRODUCTION_ASSIGN)
        or manages(principal)
    ):
        raise NotFound(ORDER_NOT_FOUND)
    if order.status not in (OrderStatus.CONFIRMED, OrderStatus.FULFILLING):
        raise Unprocessable("订单已经不在加工中（可能已发货、完成或取消）")
    if order.processed_at is not None:
        raise Unprocessable("这个订单已经加工完成")
    return order


async def open_document(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    kind: DocumentKind,
    order: Order | None,
    lines: list[Line],
    note: str,
) -> tuple[StockDocument, Todo | None]:
    """开单并提交：仓管开的、或者设置为不需要确认的直接确认；否则提醒仓管确认。返回单据和订单
    加工完成时生成的"待发货"待办（由调用方提交后分发）。"""
    now = order_service.utcnow()
    me = principal.staff_id
    spec = await sla.business_hours(session)
    no = await next_number(
        session,
        principal.tenant_id,
        scope=kind.value,
        prefix=PREFIXES[kind],
        now=now,
        tz=sla.tz_of(spec),
    )
    document = StockDocument(
        tenant_id=principal.tenant_id,
        kind=kind.value,
        no=no,
        status=DocumentStatus.PENDING.value,
        order_id=order.id if order else None,
        note=note.strip(),
        created_by=me,
        submitted_at=now,
    )
    session.add(document)
    await session.flush()
    _add_lines(session, document, lines)
    await session.flush()
    _track(session, document, "create", me)
    if order is not None:
        order_service.event(
            session,
            order,
            kind.value,
            actor_type=STAFF,
            actor_id=me,
            payload={"no": no, "document_id": str(document.id)},
        )
    return document, await _submitted(ctx, session, principal, document, order, now)


async def _submitted(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    document: StockDocument,
    order: Order | None,
    now: datetime,
) -> Todo | None:
    settings = await warehouse_settings.load(session, principal.tenant_id)
    if not settings.confirm_required or is_keeper(principal):
        return await _apply(ctx, session, principal, document, order, now)
    await _notify_keepers(session, document, order)
    return None


async def create(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    *,
    kind: DocumentKind,
    order_id: uuid.UUID | None,
    lines: list[DocumentLineIn],
    note: str,
) -> StockDocument:
    """开单（由这里提交）：关联订单的领料单，或者仓库直接开的单据。"""
    order: Order | None = None
    if order_id is not None:
        if kind == DocumentKind.RECEIPT:
            raise Unprocessable("订单的入库单在加工页“完成加工”时开")
        order = await lock_order(session, principal, order_id)
    elif not manages(principal):
        raise Forbidden("只有仓库的员工可以开不关联订单的单据")
    checked = await check_lines(session, kind, lines)
    document, todo = await open_document(ctx, session, principal, kind, order, checked, note)
    await session.commit()
    await _dispatch(ctx, document, todo)
    return document


# ---- 修改、确认、退回、作废 ----


async def _lock(
    session: AsyncSession, principal: Principal, document_id: uuid.UUID
) -> tuple[StockDocument, Order | None]:
    """先锁订单、再锁单据（与订单取消时作废单据的顺序一致，避免死锁）。"""
    found = await session.scalar(
        select(StockDocument).where(StockDocument.id == document_id, visible(principal))
    )
    if found is None:
        raise NotFound(NOT_FOUND)
    order: Order | None = None
    if found.order_id is not None:
        order = await session.scalar(
            select(Order).where(Order.id == found.order_id).with_for_update()
        )
    document = await session.scalar(
        select(StockDocument)
        .where(StockDocument.id == document_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert document is not None
    return document, order


def _require_open(document: StockDocument) -> None:
    if document.status == DocumentStatus.CONFIRMED:
        raise Conflict("单据已经确认，不能再修改；数量有出入时请盘点调整库存")
    if document.status == DocumentStatus.VOIDED:
        raise Conflict("单据已经作废")


async def resubmit(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    document_id: uuid.UUID,
    *,
    lines: list[DocumentLineIn],
    note: str,
) -> StockDocument:
    """修改后重新提交（待确认、已退回的单据；开单人或仓管）。"""
    document, order = await _lock(session, principal, document_id)
    _require_open(document)
    if not (document.created_by == principal.staff_id or is_keeper(principal)):
        raise Forbidden("只有开单人或仓管可以修改单据")
    checked = await check_lines(session, DocumentKind(document.kind), lines)
    await session.execute(
        delete(StockDocumentLine).where(StockDocumentLine.document_id == document.id)
    )
    _add_lines(session, document, checked)
    now = order_service.utcnow()
    document.note = note.strip()
    document.status = DocumentStatus.PENDING.value
    document.submitted_at = now
    document.rejected_by = document.rejected_at = document.reject_reason = None
    await session.flush()
    _track(session, document, "update", principal.staff_id)
    todo = await _submitted(ctx, session, principal, document, order, now)
    await session.commit()
    await _dispatch(ctx, document, todo)
    return document


async def confirm(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    document_id: uuid.UUID,
    *,
    quantities: dict[uuid.UUID, Decimal] | None = None,
) -> StockDocument:
    """仓管确认：修改库存（可以按实际数量修改，为 0 的行不领、不入库）；订单的入库单确认后订单
    加工完成，进入"待发货"。"""
    if not is_keeper(principal):
        raise Forbidden("只有仓管可以确认单据")
    document, order = await _lock(session, principal, document_id)
    if document.status != DocumentStatus.PENDING:
        raise Conflict(
            "单据已经确认"
            if document.status == DocumentStatus.CONFIRMED
            else "只能确认待确认的单据"
        )
    if quantities:
        lines = await _lines(session, document.id)
        known = {line.id for line in lines}
        for line_id in quantities:
            if line_id not in known:
                raise Unprocessable("单据里没有这一行")
        products = {
            p.id: p
            for p in await session.scalars(
                select(Product).where(Product.id.in_([line.product_id for line in lines]))
            )
        }
        kept = 0
        for line in lines:
            quantity = quantities.get(line.id, line.quantity)
            if quantity <= 0:
                await session.delete(line)
                continue
            stock.check_quantity(products[line.product_id], quantity)
            line.quantity = quantity
            kept += 1
        if not kept:
            raise Unprocessable("至少要有一行的数量大于 0；都不需要时请作废单据")
        await session.flush()
    now = order_service.utcnow()
    todo = await _apply(ctx, session, principal, document, order, now)
    await session.commit()
    await _dispatch(ctx, document, todo)
    return document


async def reject(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    document_id: uuid.UUID,
    reason: str,
) -> StockDocument:
    """仓管退回：写明原因，开单人修改后重新提交。"""
    if not is_keeper(principal):
        raise Forbidden("只有仓管可以退回单据")
    reason = reason.strip()
    if not reason:
        raise Unprocessable("请填写退回的原因")
    document, order = await _lock(session, principal, document_id)
    if document.status != DocumentStatus.PENDING:
        raise Conflict("只能退回待确认的单据")
    now = order_service.utcnow()
    document.status = DocumentStatus.REJECTED.value
    document.rejected_by, document.rejected_at, document.reject_reason = (
        principal.staff_id,
        now,
        reason,
    )
    _track(session, document, "reject", principal.staff_id, reason=reason)
    if order is not None:
        order_service.event(
            session,
            order,
            "document_rejected",
            actor_type=STAFF,
            actor_id=principal.staff_id,
            payload={"no": document.no, "kind": document.kind, "reason": reason},
        )
    _notify_creator(
        session,
        principal,
        document,
        f"{KIND_LABELS[document.kind]} {document.no} 被仓管退回：{reason}",
    )
    await session.commit()
    return document


async def void(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    document_id: uuid.UUID,
    reason: str,
) -> StockDocument:
    """作废还没生效的单据（开单人或仓管）。"""
    document, order = await _lock(session, principal, document_id)
    _require_open(document)
    if not (document.created_by == principal.staff_id or is_keeper(principal)):
        raise Forbidden("只有开单人或仓管可以作废单据")
    now = order_service.utcnow()
    _mark_void(session, document, principal.staff_id, now, reason.strip() or None)
    if order is not None:
        order_service.event(
            session,
            order,
            "document_voided",
            actor_type=STAFF,
            actor_id=principal.staff_id,
            payload={"no": document.no, "kind": document.kind, "reason": document.void_reason},
        )
    _notify_creator(
        session, principal, document, f"{KIND_LABELS[document.kind]} {document.no} 已作废"
    )
    await session.commit()
    return document


def _track(
    session: AsyncSession,
    document: StockDocument,
    action: str,
    staff_id: uuid.UUID | None,
    *,
    reason: str | None = None,
) -> None:
    """修改历史（§25.14）；没有员工时是系统操作（例如订单取消时作废）。"""
    history.track(
        session,
        document_history.record_type(document),
        document,
        action=action,
        actor_type=STAFF if staff_id else ActorType.SYSTEM,
        actor_id=staff_id,
        reason=reason,
    )


def _mark_void(
    session: AsyncSession,
    document: StockDocument,
    staff_id: uuid.UUID | None,
    now: datetime,
    reason: str | None,
) -> None:
    document.status = DocumentStatus.VOIDED.value
    document.voided_by, document.voided_at, document.void_reason = staff_id, now, reason
    _track(session, document, "void", staff_id, reason=reason)


async def void_for_order(
    session: AsyncSession, order: Order, *, staff_id: uuid.UUID | None, reason: str
) -> int:
    """订单取消时作废它还没生效的单据（调用方已锁定订单，由调用方提交）。"""
    rows = list(
        (
            await session.scalars(
                select(StockDocument)
                .where(StockDocument.order_id == order.id, StockDocument.status.in_(OPEN))
                .order_by(StockDocument.id)
                .with_for_update()
            )
        ).all()
    )
    now = order_service.utcnow()
    for document in rows:
        _mark_void(session, document, staff_id, now, reason)
    return len(rows)


async def _apply(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    document: StockDocument,
    order: Order | None,
    now: datetime,
) -> Todo | None:
    """确认：领料单扣减材料库存，入库单增加成品库存（原来不管理库存的成品从 0 开始），写库存记录；
    订单的入库单确认后订单加工完成。由调用方提交。"""
    me = principal.staff_id
    lines = await _lines(session, document.id)
    products = await stock.lock(session, (line.product_id for line in lines))
    requisition = document.kind == DocumentKind.REQUISITION
    label = KIND_LABELS[document.kind]
    note = f"{label} {document.no}" + (f"（订单 {order.no}）" if order else "")
    for line in lines:
        product = products[line.product_id]
        before = product.stock
        base = before if before is not None else ZERO
        after = base - line.quantity if requisition else base + line.quantity
        stock.record(
            session,
            product,
            StockKind.REQUISITION if requisition else StockKind.RECEIPT,
            after,
            actor=stock.Actor(STAFF, me),
            order_id=document.order_id,
            document_id=document.id,
            note=note,
        )
        line.stock_before, line.stock_after = before, after
    document.status = DocumentStatus.CONFIRMED.value
    document.confirmed_by, document.confirmed_at = me, now
    _track(session, document, "confirm", me)
    if document.created_by != me:
        _notify_creator(session, principal, document, f"{label} {document.no} 仓管已确认")
    if order is None:
        return None
    if document.created_by != me:
        order_service.event(
            session,
            order,
            "document_confirmed",
            actor_type=STAFF,
            actor_id=me,
            payload={"no": document.no, "kind": document.kind},
        )
    if not requisition and order.status == OrderStatus.FULFILLING and order.processed_at is None:
        items = await order_service.load_items(session, order.id)
        return await order_service.finish_production(
            session, ctx.keys, order, items, worker_id=document.created_by, now=now
        )
    return None


async def _lines(session: AsyncSession, document_id: uuid.UUID) -> list[StockDocumentLine]:
    return list(
        (
            await session.scalars(
                select(StockDocumentLine)
                .where(StockDocumentLine.document_id == document_id)
                .order_by(StockDocumentLine.sort, StockDocumentLine.id)
            )
        ).all()
    )


# ---- 提醒 ----


async def confirmers(session: AsyncSession, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    """要提醒确认单据的人：仓管（有"仓管"角色的员工都算）；没有仓管（也没有工人）时是有确认
    权限的员工。"""
    keeper = await warehouse_settings.keeper(session, tenant_id)
    if keeper.staff_id is not None:
        return [keeper.staff_id]
    if keeper.by_role:
        return list(keeper.by_role)
    return await todo_assign.staff_with(session, Permission.WAREHOUSE_CONFIRM)


async def _notify_keepers(
    session: AsyncSession, document: StockDocument, order: Order | None
) -> None:
    targets = [s for s in await confirmers(session, document.tenant_id) if s != document.created_by]
    title = f"{KIND_LABELS[document.kind]} {document.no} 待确认" + (
        f"（订单 {order.no}）" if order else ""
    )
    notifications.add(
        session,
        document.tenant_id,
        targets,
        kind="warehouse_pending",
        title=title,
        link=f"/warehouse?doc={document.id}",
    )


def _notify_creator(
    session: AsyncSession, principal: Principal, document: StockDocument, title: str
) -> None:
    if document.created_by is None or document.created_by == principal.staff_id:
        return
    link = (
        f"/production?order={document.order_id}"
        if document.order_id
        else f"/warehouse?doc={document.id}"
    )
    notifications.add(
        session,
        document.tenant_id,
        [document.created_by],
        kind="warehouse_document",
        title=title,
        link=link,
    )


async def _dispatch(ctx: AppContext, document: StockDocument, todo: Todo | None) -> None:
    if todo is not None:
        from app.modules.todos import notify as todo_notify

        await todo_notify.dispatch(ctx, tenant_id=document.tenant_id, ids=[todo.id])


# ---- 查询 ----


async def get(session: AsyncSession, principal: Principal, document_id: uuid.UUID) -> StockDocument:
    document = await session.scalar(
        select(StockDocument).where(StockDocument.id == document_id, visible(principal))
    )
    if document is None:
        raise NotFound(NOT_FOUND)
    return document


async def page(
    session: AsyncSession,
    principal: Principal,
    *,
    kind: DocumentKind | None,
    status: DocumentStatus | None,
    order_id: uuid.UUID | None,
    q: str | None,
    mine: bool,
    limit: int,
    offset: int,
) -> tuple[list[StockDocument], int]:
    conditions: list[ColumnElement[bool]] = [visible(principal)]
    if kind is not None:
        conditions.append(StockDocument.kind == kind)
    if status is not None:
        conditions.append(StockDocument.status == status)
    if order_id is not None:
        conditions.append(StockDocument.order_id == order_id)
    if mine:
        conditions.append(StockDocument.created_by == principal.staff_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        conditions.append(
            or_(
                StockDocument.no.ilike(like),
                StockDocument.order_id.in_(select(Order.id).where(Order.no.ilike(like))),
                StockDocument.id.in_(
                    select(StockDocumentLine.document_id).where(StockDocumentLine.name.ilike(like))
                ),
            )
        )
    total = int(
        await session.scalar(select(func.count()).select_from(StockDocument).where(*conditions))
        or 0
    )
    # 待确认的排在前面，其余按开单时间倒序。
    pending_first = (StockDocument.status != DocumentStatus.PENDING).asc()
    rows = await session.scalars(
        select(StockDocument)
        .where(*conditions)
        .order_by(pending_first, StockDocument.submitted_at.desc(), StockDocument.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), total


async def outs(
    session: AsyncSession, principal: Principal, documents: list[StockDocument]
) -> list[DocumentOut]:
    if not documents:
        return []
    ids = [d.id for d in documents]
    lines: dict[uuid.UUID, list[StockDocumentLine]] = defaultdict(list)
    for line in await session.scalars(
        select(StockDocumentLine)
        .where(StockDocumentLine.document_id.in_(ids))
        .order_by(StockDocumentLine.document_id, StockDocumentLine.sort, StockDocumentLine.id)
    ):
        lines[line.document_id].append(line)
    stocks = dict(
        (
            await session.execute(
                select(Product.id, Product.stock).where(
                    Product.id.in_({line.product_id for rows in lines.values() for line in rows})
                )
            )
        ).all()
    )
    orders = dict(
        (
            await session.execute(
                select(Order.id, Order.no).where(
                    Order.id.in_({d.order_id for d in documents if d.order_id})
                )
            )
        ).all()
    )
    people = {
        s
        for d in documents
        for s in (d.created_by, d.confirmed_by, d.rejected_by, d.voided_by)
        if s is not None
    }
    names = dict(
        (
            await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(people)))
        ).all()
    )
    keeper = is_keeper(principal)
    me = principal.staff_id
    result: list[DocumentOut] = []
    for d in documents:
        rows = lines[d.id]
        pending = d.status == DocumentStatus.PENDING
        open_ = d.status in OPEN
        short = [
            line.name
            for line in rows
            if pending
            and d.kind == DocumentKind.REQUISITION
            and (stocks.get(line.product_id) or ZERO) < line.quantity
        ]
        result.append(
            DocumentOut(
                id=d.id,
                kind=d.kind,
                kind_label=KIND_LABELS[d.kind],
                no=d.no,
                status=d.status,
                status_label=STATUS_LABELS[d.status],
                order_id=d.order_id,
                order_no=orders.get(d.order_id) if d.order_id else None,
                note=d.note,
                lines=[
                    DocumentLineOut(
                        id=line.id,
                        product_id=line.product_id,
                        code=line.code,
                        name=line.name,
                        spec=line.spec,
                        unit=line.unit,
                        planned=line.planned,
                        quantity=line.quantity,
                        stock=stocks.get(line.product_id),
                        stock_before=line.stock_before,
                        stock_after=line.stock_after,
                    )
                    for line in rows
                ],
                created_by_name=names.get(d.created_by) if d.created_by else None,
                created_at=d.created_at,
                submitted_at=d.submitted_at,
                confirmed_by_name=names.get(d.confirmed_by) if d.confirmed_by else None,
                confirmed_at=d.confirmed_at,
                rejected_by_name=names.get(d.rejected_by) if d.rejected_by else None,
                rejected_at=d.rejected_at,
                reject_reason=d.reject_reason,
                voided_by_name=names.get(d.voided_by) if d.voided_by else None,
                voided_at=d.voided_at,
                void_reason=d.void_reason,
                short=short,
                can_edit=open_ and (d.created_by == me or keeper),
                can_confirm=pending and keeper,
                can_void=open_ and (d.created_by == me or keeper),
            )
        )
    return result


async def one(session: AsyncSession, principal: Principal, document_id: uuid.UUID) -> DocumentOut:
    [out] = await outs(session, principal, [await get(session, principal, document_id)])
    return out


async def for_orders(
    session: AsyncSession, order_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[StockDocument]]:
    """订单的单据（不含作废的），按开单先后。"""
    result: dict[uuid.UUID, list[StockDocument]] = defaultdict(list)
    if not order_ids:
        return result
    for document in await session.scalars(
        select(StockDocument)
        .where(
            StockDocument.order_id.in_(order_ids),
            StockDocument.status != DocumentStatus.VOIDED,
        )
        .order_by(StockDocument.created_at, StockDocument.id)
    ):
        assert document.order_id is not None
        result[document.order_id].append(document)
    return result


def brief(document: StockDocument) -> DocumentBrief:
    return DocumentBrief(
        id=document.id,
        kind=document.kind,
        no=document.no,
        status=document.status,
        reject_reason=document.reject_reason,
    )


async def pending_counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(StockDocument.kind, func.count())
        .where(StockDocument.status == DocumentStatus.PENDING)
        .group_by(StockDocument.kind)
    )
    return {kind: int(count) for kind, count in rows}


# ---- 预填 ----


async def boms(
    session: AsyncSession, product_ids: set[uuid.UUID]
) -> dict[uuid.UUID, list[ProductMaterial]]:
    """成品的配方。"""
    result: dict[uuid.UUID, list[ProductMaterial]] = defaultdict(list)
    if not product_ids:
        return result
    for row in await session.scalars(
        select(ProductMaterial)
        .where(ProductMaterial.product_id.in_(product_ids))
        .order_by(ProductMaterial.product_id, ProductMaterial.sort, ProductMaterial.id)
    ):
        result[row.product_id].append(row)
    return result


def made_items(items: list[OrderItem], ready: set[uuid.UUID]) -> list[OrderItem]:
    """需要加工的订单行：不是现货的商品，或者没有对应到商品库的行。"""
    return [i for i in items if i.product_id is None or i.product_id not in ready]


@dataclass
class Plan:
    """领料单按什么算（§25.17）：每种材料要用多少（还没减去已领的）和怎么算的、这次加工的商品、
    按以往领料估算的商品、算不出用量的商品。"""

    needs: dict[uuid.UUID, Decimal]
    sources: dict[uuid.UUID, list[DraftSource]]
    items: list[DraftItem]
    estimated: list[str]
    missing: list[str]


async def requisition_plan(
    session: AsyncSession, order_id: uuid.UUID, items: list[OrderItem], ready: set[uuid.UUID]
) -> Plan:
    """需要加工的商品（不含现货）按配方算用量；没有配方的按以往领料估算；都没有的列为 missing。
    数量 × 每件用量按材料合计，每个商品的用量四舍五入到三位小数。"""
    made = made_items(items, ready)
    product_ids = {i.product_id for i in made if i.product_id}
    recipes = await boms(session, product_ids)
    learned = await usage.history(
        session, {pid for pid in product_ids if not recipes.get(pid)}, exclude=order_id
    )
    units: dict[uuid.UUID, str] = (
        dict(
            (
                await session.execute(
                    select(Product.id, Product.unit).where(Product.id.in_(product_ids))
                )
            ).all()
        )
        if product_ids
        else {}
    )
    plan = Plan(defaultdict(Decimal), defaultdict(list), [], [], [])

    def add(item: OrderItem, unit: str, material_id: uuid.UUID, per_unit: Decimal,
            basis: DraftBasisValue, orders: int | None) -> None:  # fmt: skip
        amount = usage.rounded(per_unit * item.quantity)
        if amount <= 0:
            return
        plan.needs[material_id] += amount
        plan.sources[material_id].append(
            DraftSource(
                item=order_service.item_label(item),
                quantity=item.quantity,
                unit=unit,
                per_unit=usage.rounded(per_unit),
                amount=amount,
                basis=basis,
                orders=orders,
            )
        )

    for item in made:
        label = order_service.item_label(item)
        unit = units.get(item.product_id, "") if item.product_id else ""
        rows = recipes.get(item.product_id) if item.product_id else None
        guess = learned.get(item.product_id) if item.product_id else None
        basis: DraftItemBasisValue
        orders: int | None
        if rows:
            basis, orders = "recipe", None
            for row in rows:
                add(item, unit, row.material_id, row.quantity, "recipe", None)
        elif guess is not None:
            basis, orders = "history", guess.orders
            for material_id, per_unit in guess.per_unit.items():
                add(item, unit, material_id, per_unit, "history", orders)
            if label not in plan.estimated:
                plan.estimated.append(label)
        else:
            basis, orders = ("none" if item.product_id else "unmatched"), None
            if label not in plan.missing:
                plan.missing.append(label)
        plan.items.append(
            DraftItem(
                name=item.name,
                spec=item.spec,
                quantity=item.quantity,
                unit=unit,
                basis=basis,
                orders=orders,
            )
        )
    return plan


async def _taken(
    session: AsyncSession,
    order_id: uuid.UUID,
    kind: DocumentKind,
    statuses: tuple[DocumentStatus, ...],
) -> dict[uuid.UUID, Decimal]:
    """这个订单已经开过的某种单据里每个商品的数量。"""
    rows = await session.execute(
        select(StockDocumentLine.product_id, func.sum(StockDocumentLine.quantity))
        .join(StockDocument, StockDocument.id == StockDocumentLine.document_id)
        .where(
            StockDocument.order_id == order_id,
            StockDocument.kind == kind,
            StockDocument.status.in_(statuses),
        )
        .group_by(StockDocumentLine.product_id)
    )
    return {pid: Decimal(qty) for pid, qty in rows}


def _remaining(
    total: dict[uuid.UUID, Decimal], taken: dict[uuid.UUID, Decimal]
) -> dict[uuid.UUID, Decimal]:
    return {
        pid: qty - taken.get(pid, ZERO) for pid, qty in total.items() if qty > taken.get(pid, ZERO)
    }


async def _draft_lines(session: AsyncSession, planned: dict[uuid.UUID, Decimal]) -> list[DraftLine]:
    if not planned:
        return []
    products = {
        p.id: p for p in await session.scalars(select(Product).where(Product.id.in_(planned)))
    }
    known = await stock.levels(session, planned)
    lines = [
        DraftLine(
            product_id=product.id,
            code=product.code,
            name=product.name,
            spec=product.spec,
            unit=product.unit,
            kind=product.kind,
            planned=planned[product.id],
            quantity=planned[product.id],
            stock=known[product.id].stock if product.id in known else None,
            available=known[product.id].available if product.id in known else None,
            sources=[],
            taken=ZERO,
            estimated=False,
        )
        for product in products.values()
    ]
    return sorted(lines, key=lambda line: (line.name, line.spec))


async def requisition_taken(
    session: AsyncSession, order_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[uuid.UUID, Decimal]]:
    """这些订单已经领过的材料（待确认和已确认的领料单）：订单 → 材料 → 数量。"""
    result: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = defaultdict(dict)
    if not order_ids:
        return result
    rows = await session.execute(
        select(
            StockDocument.order_id,
            StockDocumentLine.product_id,
            func.sum(StockDocumentLine.quantity),
        )
        .join(StockDocument, StockDocument.id == StockDocumentLine.document_id)
        .where(
            StockDocument.order_id.in_(order_ids),
            StockDocument.kind == DocumentKind.REQUISITION,
            StockDocument.status.in_((DocumentStatus.PENDING, DocumentStatus.CONFIRMED)),
        )
        .group_by(StockDocument.order_id, StockDocumentLine.product_id)
    )
    for order_id, product_id, quantity in rows:
        if order_id is not None:
            result[order_id][product_id] = Decimal(quantity)
    return result


@dataclass(frozen=True)
class RequisitionDraft:
    lines: list[DraftLine]
    items: list[DraftItem]
    estimated: list[str]
    missing: list[str]
    covered: bool


async def requisition_draft(session: AsyncSession, order: Order) -> RequisitionDraft:
    """领料单的预填（§25.17）：按配方（没有配方时按以往领料估算）算要领的材料，减去这个订单已经
    开过的领料单（再开时只剩没领的）；每行带上怎么算的和已领的数量。"""
    items = await order_service.load_items(session, order.id)
    ready = await order_service.ready_made_ids(session, items)
    plan = await requisition_plan(session, order.id, items, ready)
    taken = (await requisition_taken(session, [order.id])).get(order.id, {})
    remaining = _remaining(plan.needs, taken)
    lines = await _draft_lines(session, remaining)
    for line in lines:
        line.sources = plan.sources.get(line.product_id, [])
        line.taken = taken.get(line.product_id, ZERO)
        line.estimated = any(s.basis == "history" for s in line.sources)
    return RequisitionDraft(
        lines=lines,
        items=plan.items,
        estimated=plan.estimated,
        missing=plan.missing,
        covered=bool(plan.needs) and not remaining,
    )


async def receipt_draft(session: AsyncSession, order: Order) -> tuple[list[DraftLine], list[str]]:
    """入库单的预填：订单里需要加工、对应到成品的商品，按商品汇总订购数量（减去这个订单已经入库的，
    例如加工完成后又加了数量）。"""
    items = await order_service.load_items(session, order.id)
    ready = await order_service.ready_made_ids(session, items)
    planned: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    missing: list[str] = []
    for item in made_items(items, ready):
        if item.product_id is None:
            missing.append(order_service.item_label(item))
        else:
            planned[item.product_id] += item.quantity
    received = await _taken(session, order.id, DocumentKind.RECEIPT, (DocumentStatus.CONFIRMED,))
    return await _draft_lines(session, _remaining(dict(planned), received)), missing


async def open_receipt(session: AsyncSession, order_id: uuid.UUID) -> StockDocument | None:
    """订单还没生效的入库单（待确认或已退回）。"""
    return await session.scalar(
        select(StockDocument)
        .where(
            StockDocument.order_id == order_id,
            StockDocument.kind == DocumentKind.RECEIPT,
            StockDocument.status.in_(OPEN),
        )
        .order_by(StockDocument.created_at.desc())
        .limit(1)
    )


async def void_open_receipts(
    session: AsyncSession, order: Order, *, staff_id: uuid.UUID | None, reason: str
) -> None:
    """订单回到加工中时（例如修改了商品），作废它还没生效的入库单。"""
    rows = await session.scalars(
        select(StockDocument)
        .where(
            StockDocument.order_id == order.id,
            StockDocument.kind == DocumentKind.RECEIPT,
            StockDocument.status.in_(OPEN),
        )
        .order_by(StockDocument.id)
        .with_for_update()
    )
    now = order_service.utcnow()
    for document in rows.all():
        _mark_void(session, document, staff_id, now, reason)
