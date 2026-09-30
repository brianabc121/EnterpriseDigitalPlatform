"""订单的核心逻辑（设计文档 §25）：数据范围、编号与跟踪链接、收货信息、商品行与金额、收款状态、
版本快照与差异、动态，以及关联的"订单审核""催收"待办。"""

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.db.counters import next_number
from app.modules.customer.models import Customer
from app.modules.customer.sensitive import mask_phone
from app.modules.customer.service import visible_to as customer_visible_to
from app.modules.iam.principal import Principal
from app.modules.integration import outbox as webhook_outbox
from app.modules.orders.models import (
    PAYMENT_METHOD_LABELS,
    Order,
    OrderEvent,
    OrderItem,
    OrderPayment,
    OrderRevision,
    OrderStatus,
    PaymentKind,
    PaymentMethod,
    PaymentStatus,
    RevisionKind,
    WorkStatus,
)
from app.modules.orders.settings import OrderSettings
from app.modules.orders.settings import load as load_settings
from app.modules.products.models import Product, ProductKind, ProductStatus
from app.modules.routing.scope import led_groups, team_members
from app.modules.security.keys import TenantKeyring
from app.modules.todos import events as todo_events
from app.modules.todos import presets, sla
from app.modules.todos import service as todo_service
from app.modules.todos.models import UNFINISHED, ActorType, Todo, TodoSource, TodoStatus
from app.modules.warehouse.models import OPEN as DOCUMENT_OPEN
from app.modules.warehouse.models import DocumentKind, DocumentStatus, StockDocument

NOT_FOUND = "订单不存在或没有权限查看"
NUMBER_SCOPE = "order"
MAX_LINES = 20
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
RECEIVER_KEYS = ("name", "phone", "address")
RECEIVER_LABELS = {"name": "收货人", "phone": "联系电话", "address": "收货地址"}
PHONE = re.compile(r"^\+?[\d\- ]{5,20}$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def text_money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


# ---- 数据范围（与客户一致） ----


def visible_to(principal: Principal) -> ColumnElement[bool]:
    """分派给自己的、自己新建的、自己能看到其客户的，以及所在技能组待认领的；主管另外能看到团队
    成员的和所带技能组的；能分派工作的人能看到还没有处理人的；能看到全部客户的人能看到全部订单。"""
    if principal.has(Permission.CUSTOMER_READ_ALL) or principal.has(Permission.SESSION_READ_ALL):
        return true()
    me = principal.staff_id
    conditions: list[ColumnElement[bool]] = [
        Order.assignee_id == me,
        Order.created_by == me,
        and_(Order.assignee_id.is_(None), Order.skill_group_id.in_(todo_service.my_groups(me))),
        Order.customer_id.in_(select(Customer.id).where(customer_visible_to(principal))),
    ]
    if principal.has(Permission.SESSION_READ_TEAM):
        conditions += [
            Order.assignee_id.in_(team_members(me)),
            Order.skill_group_id.in_(led_groups(me)),
        ]
    if principal.has(Permission.TODO_ASSIGN):
        conditions.append(and_(Order.assignee_id.is_(None), Order.skill_group_id.is_(None)))
    return or_(*conditions)


async def get_visible(
    session: AsyncSession, principal: Principal, order_id: uuid.UUID, *, lock: bool = False
) -> Order:
    query = select(Order).where(Order.id == order_id, visible_to(principal))
    if lock:
        query = query.with_for_update(of=Order)
    order = await session.scalar(query)
    if order is None:
        raise NotFound(NOT_FOUND)
    return order


# ---- 编号与跟踪链接 ----


async def next_no(
    session: AsyncSession, tenant_id: uuid.UUID, settings: OrderSettings, now: datetime
) -> str:
    spec = await sla.business_hours(session)
    return await next_number(
        session, tenant_id, scope=NUMBER_SCOPE, prefix=settings.prefix, now=now, tz=sla.tz_of(spec)
    )


def new_token() -> str:
    return secrets.token_urlsafe(24)


def tracking_url(app_settings: Settings, token: str) -> str:
    return f"{app_settings.widget_public_url.rstrip('/')}/?track={token}"


def tracking_active(order: Order, now: datetime | None = None) -> bool:
    return order.tracking_expires_at is None or order.tracking_expires_at > (now or utcnow())


# ---- 收货信息（加密保存，默认掩码） ----


def mask_receiver(key: str, value: str) -> str:
    if key == "phone":
        return mask_phone(value)
    if key == "address":
        return value[:6] + "****" if len(value) > 6 else "****"
    return value[:1] + "*" * min(max(len(value) - 1, 1), 3)


def masked_receiver(stored: dict[str, Any]) -> dict[str, str]:
    return {k: str(v.get("masked", "****")) for k, v in stored.items() if isinstance(v, dict)}


async def reveal_receiver(
    keys: TenantKeyring, tenant_id: uuid.UUID, stored: dict[str, Any]
) -> dict[str, str]:
    return {
        k: await keys.unseal(tenant_id, v["enc"])
        for k, v in stored.items()
        if isinstance(v, dict) and "enc" in v
    }


def check_receiver(values: dict[str, str | None]) -> None:
    phone = values.get("phone")
    if phone and not PHONE.match(phone.strip()):
        raise Unprocessable("联系电话的格式不正确")
    for key, limit in (("name", 32), ("phone", 20), ("address", 200)):
        value = values.get(key)
        if value and len(value.strip()) > limit:
            raise Unprocessable(f"{RECEIVER_LABELS[key]}最多 {limit} 个字")


async def merge_receiver(
    keys: TenantKeyring | None,
    tenant_id: uuid.UUID,
    stored: dict[str, Any],
    changes: dict[str, str | None],
) -> tuple[dict[str, Any], list[str]]:
    """修改收货信息：None 表示不修改，空字符串表示清除。返回新的保存值和改动的项。"""
    check_receiver(changes)
    result = dict(stored)
    changed: list[str] = []
    for key in RECEIVER_KEYS:
        value = changes.get(key)
        if value is None:
            continue
        value = value.strip()
        if not value:
            if key in result:
                result.pop(key)
                changed.append(key)
            continue
        if keys is None:
            raise RuntimeError("sealing receiver details needs the tenant keyring")
        result[key] = {
            "enc": await keys.seal(tenant_id, value),
            "masked": mask_receiver(key, value),
        }
        changed.append(key)
    return result, changed


def missing_required(order: Order, settings: OrderSettings) -> list[str]:
    labels = {
        "receiver_name": ("name", "收货人"),
        "receiver_phone": ("phone", "联系电话"),
        "receiver_address": ("address", "收货地址"),
    }
    missing = [
        label
        for field, (key, label) in labels.items()
        if field in settings.required_fields and key not in order.receiver
    ]
    if "expected_at" in settings.required_fields and order.expected_at is None:
        missing.append("期望时间")
    return missing


# ---- 商品行与金额 ----


@dataclass(frozen=True)
class Line:
    """一行商品。product_id 为空时是没有匹配商品库的行（保留客户的原话）。unit_price 为空时
    按商品的建议零售价（没有建议零售价、或没有匹配商品库时待定价）。"""

    quantity: int
    product_id: uuid.UUID | None = None
    unit_price: Decimal | None = None
    raw_text: str | None = None
    name: str | None = None


async def build_items(
    session: AsyncSession, order: Order, lines: list[Line], settings: OrderSettings
) -> list[OrderItem]:
    if not lines:
        raise Unprocessable("订单至少要有一个商品")
    if len(lines) > MAX_LINES:
        raise Unprocessable(f"每单最多 {MAX_LINES} 个商品行")
    ids = [line.product_id for line in lines if line.product_id is not None]
    products = {p.id: p for p in await session.scalars(select(Product).where(Product.id.in_(ids)))}
    items: list[OrderItem] = []
    for sort, line in enumerate(lines):
        if not 1 <= line.quantity <= settings.max_quantity:
            raise Unprocessable(f"数量必须在 1 到 {settings.max_quantity} 之间")
        unit = cents(line.unit_price) if line.unit_price is not None else None
        if line.product_id is not None:
            product = products.get(line.product_id)
            if product is None:
                raise Unprocessable("商品不存在")
            if product.kind != ProductKind.GOODS:
                raise Unprocessable(f"「{product.name}」是生产用的材料，不能下单")
            if product.status != ProductStatus.ON:
                raise Unprocessable(f"「{product.name}」已下架，不能下单")
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                code=product.code,
                name=product.name,
                model=product.model,
                spec=product.spec,
                image_url=product.image_url,
                quantity=line.quantity,
                list_price=product.retail_price,
                unit_price=unit if unit is not None else product.retail_price,
                cost_price=product.cost_price,
                sort=sort,
                work_status=WorkStatus.PENDING.value,
            )
        else:
            text = (line.raw_text or line.name or "").strip()
            if not text:
                raise Unprocessable("没有匹配商品库的商品行需要填写商品的说明")
            item = OrderItem(
                order_id=order.id,
                product_id=None,
                name=(line.name or text).strip()[:128],
                raw_text=text[:200],
                quantity=line.quantity,
                unit_price=unit,
                sort=sort,
                work_status=WorkStatus.PENDING.value,
            )
        item.amount = (
            cents(item.unit_price * item.quantity) if item.unit_price is not None else ZERO
        )
        items.append(item)
    return items


def payment_status(order: Order) -> str:
    net = order.paid_amount - order.refunded_amount
    if order.refunded_amount > 0 and net <= 0:
        return PaymentStatus.REFUNDED
    if net <= 0:
        return PaymentStatus.UNPAID
    if order.total > 0 and net >= order.total:
        return PaymentStatus.PAID
    if (
        order.payment_method == PaymentMethod.DEPOSIT
        and order.deposit_amount is not None
        and net >= order.deposit_amount
    ):
        return PaymentStatus.DEPOSIT
    return PaymentStatus.PARTIAL


def recompute(order: Order, items: list[OrderItem], payments: list[OrderPayment]) -> None:
    order.items_amount = cents(sum((i.amount for i in items), ZERO))
    order.price_pending = any(i.unit_price is None for i in items)
    order.discount = cents(min(order.discount, order.items_amount))
    order.total = cents(order.items_amount - order.discount)
    valid = [p for p in payments if p.voided_at is None]
    order.paid_amount = cents(sum((p.amount for p in valid if p.kind == PaymentKind.PAYMENT), ZERO))
    order.refunded_amount = cents(
        sum((p.amount for p in valid if p.kind == PaymentKind.REFUND), ZERO)
    )
    order.payment_status = payment_status(order)


def outstanding(order: Order) -> Decimal:
    """未收金额（合计减去净收款）。"""
    return max(ZERO, order.total - (order.paid_amount - order.refunded_amount))


def discount_rate(items: list[OrderItem], total: Decimal) -> Decimal:
    """相对建议零售价的优惠比例（百分比）。只计算有建议零售价的商品行。"""
    listed = sum(
        (i.list_price * i.quantity for i in items if i.list_price is not None), Decimal("0")
    )
    if listed <= 0:
        return Decimal("0")
    return max(Decimal("0"), (listed - total) / listed * 100)


def check_discount(
    principal: Principal, items: list[OrderItem], total: Decimal, settings: OrderSettings
) -> None:
    rate = discount_rate(items, total)
    if rate > settings.discount_limit and not principal.has(Permission.ORDER_CREDIT):
        raise Forbidden(
            f"优惠超过上限（{settings.discount_limit}%），需要有审批权限（order:credit）的主管修改"
        )


def summary(items: list[OrderItem]) -> str:
    parts = [f"{i.name}{' ' + i.spec if i.spec else ''} × {i.quantity}" for i in items]
    text = "、".join(parts)
    return text if len(text) <= 300 else text[:297] + "…"


async def load_items(session: AsyncSession, order_id: uuid.UUID) -> list[OrderItem]:
    return list(
        (
            await session.scalars(
                select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.sort)
            )
        ).all()
    )


async def load_payments(session: AsyncSession, order_id: uuid.UUID) -> list[OrderPayment]:
    return list(
        (
            await session.scalars(
                select(OrderPayment)
                .where(OrderPayment.order_id == order_id)
                .order_by(OrderPayment.created_at, OrderPayment.id)
            )
        ).all()
    )


# ---- 版本：快照与差异 ----

SCALARS = (
    "status",
    "discount",
    "total",
    "payment_method",
    "deposit_amount",
    "credit_due_date",
    "expected_at",
    "customer_note",
    "internal_note",
    "payment_status",
    "shipping_company",
    "tracking_no",
)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def snapshot(order: Order, items: list[OrderItem], payments: list[OrderPayment]) -> dict[str, Any]:
    """订单的完整内容（修改记录里保存；收货信息只有掩码，不含成本价）。"""
    return {
        "status": order.status,
        "items": [
            {
                "product_id": str(i.product_id) if i.product_id else None,
                "code": i.code,
                "name": i.name,
                "model": i.model,
                "spec": i.spec,
                "raw_text": i.raw_text,
                "quantity": i.quantity,
                "list_price": text_money(i.list_price),
                "unit_price": text_money(i.unit_price),
                "amount": text_money(i.amount),
            }
            for i in items
        ],
        "items_amount": text_money(order.items_amount),
        "discount": text_money(order.discount),
        "total": text_money(order.total),
        "payment_method": order.payment_method,
        "deposit_amount": text_money(order.deposit_amount),
        "credit_due_date": _iso(order.credit_due_date),
        "receiver": masked_receiver(order.receiver),
        "expected_at": _iso(order.expected_at),
        "customer_note": order.customer_note,
        "internal_note": order.internal_note,
        "payments": [
            {
                "id": str(p.id),
                "kind": p.kind,
                "amount": text_money(p.amount),
                "channel": p.channel,
                "paid_at": _iso(p.paid_at),
                "voided": p.voided_at is not None,
            }
            for p in payments
        ],
        "paid_amount": text_money(order.paid_amount),
        "refunded_amount": text_money(order.refunded_amount),
        "payment_status": order.payment_status,
        "shipping_company": order.shipping_company,
        "tracking_no": order.tracking_no,
    }


def _line_key(line: dict[str, Any]) -> str:
    return line["product_id"] or f"text:{line.get('raw_text') or line['name']}"


def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for key in SCALARS:
        if before.get(key) != after.get(key):
            changes[key] = {"from": before.get(key), "to": after.get(key)}
    old = {_line_key(x): x for x in before.get("items", [])}
    new = {_line_key(x): x for x in after.get("items", [])}
    added = [
        {"name": x["name"], "spec": x["spec"], "quantity": x["quantity"]}
        for k, x in new.items()
        if k not in old
    ]
    removed = [
        {"name": x["name"], "spec": x["spec"], "quantity": x["quantity"]}
        for k, x in old.items()
        if k not in new
    ]
    changed = []
    for key in old.keys() & new.keys():
        entry: dict[str, Any] = {"name": new[key]["name"], "spec": new[key]["spec"]}
        for field in ("quantity", "unit_price"):
            if old[key][field] != new[key][field]:
                entry[field] = {"from": old[key][field], "to": new[key][field]}
        if len(entry) > 2:
            changed.append(entry)
    if added or removed or changed:
        changes["items"] = {"added": added, "removed": removed, "changed": changed}
    receiver = sorted(
        k
        for k in RECEIVER_KEYS
        if before.get("receiver", {}).get(k) != after.get("receiver", {}).get(k)
    )
    if receiver:
        changes["receiver"] = receiver
    old_payments = {p["id"]: p for p in before.get("payments", [])}
    new_payments = {p["id"]: p for p in after.get("payments", [])}
    payment_added = [p for k, p in new_payments.items() if k not in old_payments]
    voided = [
        p
        for k, p in new_payments.items()
        if p["voided"] and not old_payments.get(k, {}).get("voided")
    ]
    if payment_added or voided:
        changes["payments"] = {
            "added": payment_added,
            "voided": [p for p in voided if p["id"] in old_payments],
        }
    return changes


def add_revision(
    session: AsyncSession,
    order: Order,
    *,
    kind: RevisionKind,
    actor_type: str,
    actor_id: uuid.UUID | None,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    reason: str | None = None,
    note: str | None = None,
    extra: dict[str, Any] | None = None,
) -> OrderRevision:
    """记一个版本（由调用方提交）。除了最初的版本，每次都把订单的版本号加一。"""
    if before is not None:
        order.version += 1
    changes = {**(diff(before, after) if before is not None else {}), **(extra or {})}
    revision = OrderRevision(
        tenant_id=order.tenant_id,
        order_id=order.id,
        version=order.version,
        kind=kind,
        actor_type=actor_type,
        actor_id=actor_id,
        reason=reason,
        note=(note or "").strip()[:500] or None,
        changes=changes,
        snapshot=after,
    )
    session.add(revision)
    return revision


def event(
    session: AsyncSession,
    order: Order,
    type_: str,
    *,
    actor_type: str,
    actor_id: uuid.UUID | None,
    payload: dict[str, Any] | None = None,
    public: bool = False,
) -> OrderEvent:
    item = OrderEvent(
        tenant_id=order.tenant_id,
        order_id=order.id,
        type=type_,
        actor_type=actor_type,
        actor_id=actor_id,
        payload=payload or {},
        public=public,
    )
    session.add(item)
    # 同一个事务里写入推送事件（企业系统对接，§25.8）。
    webhook_outbox.order_event(session, order, type_, actor_type=actor_type, payload=payload)
    return item


# ---- 关联的待办 ----


async def open_review_todo(
    session: AsyncSession,
    keys: TenantKeyring | None,
    order: Order,
    items: list[OrderItem],
    *,
    actor_type: ActorType,
    actor_id: uuid.UUID | None,
    assignee_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Todo:
    """订单提交审核时生成"订单审核"待办：按这个类型的分派规则确定处理人（或由员工指定），
    订单的处理人与它一致（由调用方提交）。草稿的"跟进未完成的订单"待办随之完成。"""
    await close_todo(
        session,
        order.review_todo_id,
        result="订单已提交审核",
        cancelled=False,
        actor_type=actor_type,
        actor_id=actor_id,
        now=now,
    )
    type_ = await presets.type_by_code(session, order.tenant_id, presets.ORDER_REVIEW)
    todo = await todo_service.create(
        session,
        keys,
        todo_service.Draft(
            type=type_,
            title=f"审核订单 {order.no}",
            detail=f"{summary(items)}，合计 {text_money(order.total)} 元"
            + ("（有待定价的商品）" if order.price_pending else ""),
            source=TodoSource.RULE,
            created_by_type=actor_type,
            created_by=actor_id,
            customer_id=order.customer_id,
            session_id=order.session_id,
            order_id=order.id,
            explicit=assignee_id is not None,
            assignee_id=assignee_id,
        ),
        now=now,
    )
    order.review_todo_id = todo.id
    order.assignee_id, order.skill_group_id = todo.assignee_id, todo.skill_group_id
    return todo


async def close_todo(
    session: AsyncSession,
    todo_id: uuid.UUID | None,
    *,
    result: str,
    cancelled: bool,
    actor_type: str,
    actor_id: uuid.UUID | None,
    now: datetime | None = None,
) -> None:
    """订单确认、取消或收清款项后，结束关联的待办（由调用方提交）。"""
    if todo_id is None:
        return
    todo = await session.scalar(select(Todo).where(Todo.id == todo_id).with_for_update())
    if todo is None or todo.status not in UNFINISHED:
        return
    now = now or utcnow()
    todo.status = TodoStatus.CANCELLED if cancelled else TodoStatus.DONE
    if cancelled:
        todo.close_note = result
    else:
        todo.result = result
    todo.closed_at = now
    todo.paused_at = None
    todo.first_response_at = todo.first_response_at or now
    todo.pending_remind_at = todo.remind_at = todo.escalate_at = None
    todo_events.record(
        session,
        todo,
        "cancelled" if cancelled else "done",
        actor_type=actor_type,
        actor_id=actor_id,
        payload={"result": result, "by_order": True},
    )


def payment_label(order: Order) -> str:
    return PAYMENT_METHOD_LABELS.get(order.payment_method or "", "待定")


def expire_tracking(order: Order, settings: OrderSettings, now: datetime) -> None:
    order.tracking_expires_at = now + timedelta(days=settings.tracking_days)


def require_status(order: Order, allowed: tuple[OrderStatus, ...], message: str) -> None:
    if order.status not in allowed:
        raise Unprocessable(message)


OPEN_FOR_PAYMENT = (
    OrderStatus.PENDING_REVIEW,
    OrderStatus.CONFIRMED,
    OrderStatus.FULFILLING,
    OrderStatus.SHIPPED,
    OrderStatus.COMPLETED,
)


def revisions_query(order_id: uuid.UUID) -> Any:
    return (
        select(OrderRevision)
        .where(OrderRevision.order_id == order_id)
        .order_by(OrderRevision.version)
    )


# ---- 加工（设计文档 §25.11） ----


def item_label(item: OrderItem) -> str:
    return f"{item.name}{' ' + item.spec if item.spec else ''}"


def shortage_items(items: list[OrderItem]) -> list[OrderItem]:
    return [i for i in items if i.work_status == WorkStatus.OUT_OF_STOCK]


def shortage_text(items: list[OrderItem]) -> str:
    """ "缺货处理"待办的说明：每个缺货的商品一行（缺多少、预计到货、说明）。"""
    lines: list[str] = []
    for item in shortage_items(items):
        parts = [f"{item_label(item)} 缺 {item.shortage_qty or item.quantity}/{item.quantity}"]
        if item.restock_date is not None:
            parts.append(f"预计 {item.restock_date.isoformat()} 到货")
        if item.shortage_note:
            parts.append(item.shortage_note)
        lines.append("，".join(parts))
    return "\n".join(lines)


def carry_work(old_items: list[OrderItem], new_items: list[OrderItem]) -> None:
    """修改商品后保留原有商品行（同一商品、同一说明）的加工进度：数量没有增加的已完成仍算完成，
    数量不变的缺货仍算缺货，其余的重新加工。"""
    remaining: dict[tuple[uuid.UUID | None, str | None], list[OrderItem]] = {}
    for old in old_items:
        remaining.setdefault((old.product_id, old.raw_text), []).append(old)
    for item in new_items:
        matches = remaining.get((item.product_id, item.raw_text))
        previous = matches.pop(0) if matches else None
        item.work_status = WorkStatus.PENDING.value
        if previous is None:
            continue
        if previous.work_status == WorkStatus.DONE and item.quantity <= previous.quantity:
            item.work_status = WorkStatus.DONE.value
            item.done_at, item.done_by = previous.done_at, previous.done_by
        elif previous.work_status == WorkStatus.OUT_OF_STOCK and item.quantity == previous.quantity:
            item.work_status = WorkStatus.OUT_OF_STOCK.value
            item.shortage_qty, item.shortage_note = previous.shortage_qty, previous.shortage_note
            item.restock_date = previous.restock_date
            item.shortage_at, item.shortage_by = previous.shortage_at, previous.shortage_by


def _todo_owner(order: Order) -> dict[str, Any]:
    """订单相关的待办交给订单的处理人（或它所在技能组的待认领池）；都没有时按类型的分派规则。"""
    if order.assignee_id is None and order.skill_group_id is None:
        return {}
    return {
        "explicit": True,
        "assignee_id": order.assignee_id,
        "skill_group_id": order.skill_group_id if order.assignee_id is None else None,
    }


async def open_ship_todo(
    session: AsyncSession,
    keys: TenantKeyring | None,
    order: Order,
    items: list[OrderItem],
    settings: OrderSettings,
    *,
    actor_id: uuid.UUID | None,
    now: datetime,
) -> Todo:
    """加工完成后生成"待发货"待办，提醒客服发货（没有发货环节的是交付并完成订单）。"""
    type_ = await presets.type_by_code(session, order.tenant_id, presets.ORDER_SHIP)
    verb = "发货" if settings.shipping_enabled else "交付并完成订单"
    detail = summary(items)
    if order.expected_at is not None:
        detail += f"\n客户期望时间：{order.expected_at.isoformat(timespec='minutes')}"
    todo = await todo_service.create(
        session,
        keys,
        todo_service.Draft(
            type=type_,
            title=f"订单 {order.no} 已加工完成，请{verb}",
            detail=detail,
            source=TodoSource.RULE,
            created_by_type=ActorType.STAFF,
            created_by=actor_id,
            customer_id=order.customer_id,
            session_id=order.session_id,
            order_id=order.id,
            **_todo_owner(order),
        ),
        now=now,
    )
    order.ship_todo_id = todo.id
    return todo


async def finish_production(
    session: AsyncSession,
    keys: TenantKeyring | None,
    order: Order,
    items: list[OrderItem],
    *,
    worker_id: uuid.UUID | None,
    now: datetime,
) -> Todo:
    """加工完成（§25.11、§25.13）：订单进入"待发货"，客服在待办里收到提醒。要入库的订单在入库单
    确认后才完成（由调用方提交，提交后分发待办提醒）。"""
    settings = await load_settings(session, order.tenant_id)
    order.processed_at, order.processed_by = now, worker_id
    event(
        session,
        order,
        "processed",
        actor_type=ActorType.STAFF,
        actor_id=worker_id,
        payload={"shipping": settings.shipping_enabled},
        public=True,
    )
    return await open_ship_todo(session, keys, order, items, settings, actor_id=worker_id, now=now)


def needs_production() -> ColumnElement[bool]:
    """订单里有需要加工的商品：不是现货的商品，或者没有对应到商品库的行（§25.13）。"""
    return exists(
        select(OrderItem.id)
        .outerjoin(Product, Product.id == OrderItem.product_id)
        .where(
            OrderItem.order_id == Order.id,
            or_(Product.id.is_(None), Product.ready_made.is_(False)),
        )
    )


async def ready_made_ids(session: AsyncSession, items: list[OrderItem]) -> set[uuid.UUID]:
    """订单行里的现货商品（直接从成品库存发货，不需要加工）。"""
    ids = {i.product_id for i in items if i.product_id is not None}
    if not ids:
        return set()
    return set(
        await session.scalars(
            select(Product.id).where(Product.id.in_(ids), Product.ready_made.is_(True))
        )
    )


async def sync_shortage(
    session: AsyncSession,
    keys: TenantKeyring | None,
    order: Order,
    items: list[OrderItem],
    *,
    actor_id: uuid.UUID | None,
    now: datetime,
) -> Todo | None:
    """缺货变化后：订单的缺货标记与"缺货处理"待办跟着变化（有缺货时说明随之更新，都处理好后
    结束待办）。新建了待办时返回它（由调用方提醒处理人）。"""
    short = shortage_items(items)
    if not short:
        order.shortage_at = None
        await close_todo(
            session,
            order.shortage_todo_id,
            result="缺货已处理",
            cancelled=False,
            actor_type=ActorType.STAFF,
            actor_id=actor_id,
            now=now,
        )
        return None
    order.shortage_at = order.shortage_at or now
    detail = shortage_text(items)
    if order.shortage_todo_id is not None:
        existing = await session.scalar(
            select(Todo).where(Todo.id == order.shortage_todo_id).with_for_update()
        )
        if existing is not None and existing.status in UNFINISHED:
            existing.detail = detail
            return None
    type_ = await presets.type_by_code(session, order.tenant_id, presets.ORDER_SHORTAGE)
    names = "、".join(item_label(i) for i in short)
    todo = await todo_service.create(
        session,
        keys,
        todo_service.Draft(
            type=type_,
            title=f"订单 {order.no} 缺货：{names}"[:120],
            detail=detail,
            source=TodoSource.RULE,
            created_by_type=ActorType.STAFF,
            created_by=actor_id,
            customer_id=order.customer_id,
            session_id=order.session_id,
            order_id=order.id,
            **_todo_owner(order),
        ),
        now=now,
    )
    order.shortage_todo_id = todo.id
    return todo


async def close_production_todos(
    session: AsyncSession,
    order: Order,
    *,
    result: str,
    cancelled: bool,
    actor_type: str,
    actor_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """订单发货、完成或取消后，结束"待发货""缺货处理"待办（由调用方提交）。"""
    for todo_id in (order.ship_todo_id, order.shortage_todo_id):
        await close_todo(
            session,
            todo_id,
            result=result,
            cancelled=cancelled,
            actor_type=actor_type,
            actor_id=actor_id,
            now=now,
        )


async def after_edit(
    session: AsyncSession,
    keys: TenantKeyring | None,
    order: Order,
    items: list[OrderItem],
    *,
    actor_id: uuid.UUID | None,
    now: datetime,
) -> list[uuid.UUID]:
    """修改商品后：加工完成的订单多了要加工的商品时退回加工（结束"待发货"待办）；缺货标记与
    "缺货处理"待办跟着变化。返回新建的待办（由调用方提醒处理人）。"""
    if order.status not in (OrderStatus.CONFIRMED, OrderStatus.FULFILLING):
        return []
    if any(i.work_status == WorkStatus.PENDING for i in items):
        # 还有要加工的商品：还没生效的入库单作废，加工完成后重新开（§25.13）。
        voided = await session.scalars(
            update(StockDocument)
            .where(
                StockDocument.order_id == order.id,
                StockDocument.kind == DocumentKind.RECEIPT,
                StockDocument.status.in_(DOCUMENT_OPEN),
            )
            .values(
                status=DocumentStatus.VOIDED.value,
                voided_by=actor_id,
                voided_at=now,
                void_reason="订单修改后需要重新加工",
            )
            .returning(StockDocument.no)
        )
        for no in voided.all():
            event(
                session,
                order,
                "document_voided",
                actor_type=ActorType.STAFF,
                actor_id=actor_id,
                payload={
                    "no": no,
                    "kind": DocumentKind.RECEIPT.value,
                    "reason": "订单修改后需要重新加工",
                },
            )
    if order.processed_at is not None and any(i.work_status == WorkStatus.PENDING for i in items):
        order.processed_at = order.processed_by = None
        await close_todo(
            session,
            order.ship_todo_id,
            result="订单修改后需要重新加工",
            cancelled=True,
            actor_type=ActorType.STAFF,
            actor_id=actor_id,
            now=now,
        )
        event(
            session,
            order,
            "reprocess",
            actor_type=ActorType.STAFF,
            actor_id=actor_id,
            payload={},
        )
    if order.shortage_at is None and not shortage_items(items):
        return []
    todo = await sync_shortage(session, keys, order, items, actor_id=actor_id, now=now)
    return [todo.id] if todo is not None else []
