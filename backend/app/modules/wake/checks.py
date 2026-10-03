"""数据巡检的检查项（设计文档 §33.3）。

每个检查项是一段查询：在租户自己的会话里找出符合口径的对象，返回"发现"（Hit）——对象、标题、说明、
级别、链接和负责人。是否新问题、要不要通知、什么时候消除由 runner.py 统一处理。

- 口径里的数字（几小时、几天、几条）可以在设置里改（Param），默认值见各检查项；
- 负责人优先是对象上的具体员工（订单的处理人、加工的工人、待办的处理人……），没有时交给有相应权限的
  员工，再没有时交给租户管理员；只取启用状态的员工；
- 标题和说明不含客户的手机号、地址（订单用单号）；不写随时间变化的时长（"已 5 小时"），写设定的时限
  和具体时间，页面按 data.since 显示已经过了多久——数据不变时结果也不变，检查项可以跳过；
- 增量更新索引（§33.9）：每个检查项声明它读的表（domains），这些表都没有变化、口径没改、也没到它登记
  的时刻（Scope.due：过了时限、变严重、移出统计范围的时刻）时，runner.py 跳过它，不再查询这些表。
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dates import day_bounds
from app.core.permissions import Permission
from app.modules.ai.models import SessionIntent
from app.modules.contracts import settings as contract_settings
from app.modules.contracts.models import Contract, ContractStatus
from app.modules.conversation.models import ChatSession, SessionStatus
from app.modules.customer.models import Customer
from app.modules.finance import service as finance
from app.modules.iam.models import Staff, StaffStatus
from app.modules.kb.models import CandidateKind, CandidateSource, CandidateStatus, KbCandidate
from app.modules.mail.models import MailAccount, MailStatus
from app.modules.opportunities.models import Opportunity, OpportunityStatus
from app.modules.orders.models import Order, OrderItem, OrderStatus, WorkStatus
from app.modules.print.models import Printer, PrinterStatus
from app.modules.products import stock
from app.modules.products.models import Product, ProductKind, ProductStatus
from app.modules.todos import assign
from app.modules.todos.models import ACTIVE, Todo, TodoStatus
from app.modules.wake.models import Severity
from app.modules.warehouse.models import KIND_LABELS, DocumentStatus, StockDocument


class Category:
    ORDER = "order"
    RECEIVABLE = "receivable"
    PRODUCTION = "production"
    WAREHOUSE = "warehouse"
    TODO = "todo"
    SERVICE = "service"
    TREND = "trend"
    KNOWLEDGE = "knowledge"
    CUSTOMER = "customer"
    CONTRACT = "contract"
    SYSTEM = "system"


CATEGORY_LABELS: dict[str, str] = {
    Category.ORDER: "订单",
    Category.RECEIVABLE: "应收",
    Category.PRODUCTION: "加工",
    Category.WAREHOUSE: "仓库",
    Category.TODO: "待办",
    Category.SERVICE: "客服",
    Category.TREND: "趋势",
    Category.KNOWLEDGE: "知识",
    Category.CUSTOMER: "客户",
    Category.CONTRACT: "合同",
    Category.SYSTEM: "系统",
}

# 一次检查最多记下多少个对象（其余的下次再看，避免异常数据刷出成千上万条问题）。
MAX_HITS = 200
# 检查口径的版本：检查项的查询或标题改了时加 1，之前记下的检查状态（wake_check_state）全部作废。
VERSION = 1
# 每个检查项都读的表：负责人（员工、角色）和时区（默认路由策略的工作时间）。
COMMON_DOMAINS = ("staff", "staff_roles", "roles", "routing_policies")
# 等待超过 24 小时、超期 3 天、过了承诺的付款日 7 天算严重。
CRITICAL_WAIT = timedelta(hours=24)
CRITICAL_LATE = timedelta(days=3)
CRITICAL_UNPAID_DAYS = 7
CONTRACT_CRITICAL_DAYS = 7


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    unit: str
    default: int
    minimum: int
    maximum: int


@dataclass
class Hit:
    """一个对象上发现的问题。key 是对象（如 order:<id>），和检查项一起是问题的唯一标识。"""

    key: str
    title: str
    severity: Severity
    assignees: list[uuid.UUID]
    detail: str | None = None
    link: str | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    data: dict[str, Any] = field(default_factory=dict)


class Scope:
    """一个检查项的运行环境：租户会话、当前时间、租户时区、口径数字，以及找负责人的方法（缓存）。"""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        now: datetime,
        tz: ZoneInfo,
        params: dict[str, int] | None = None,
        cache: dict[str, Any] | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.now = now
        self.tz = tz
        self.params = params or {}
        self._cache = cache if cache is not None else {}
        # 数据不变、到了这个时刻检查结果也可能变化（见 due）。
        self.next_due: datetime | None = None

    @property
    def today(self) -> date:
        return self.now.astimezone(self.tz).date()

    def due(self, at: datetime | None) -> None:
        """登记一个时刻：数据不变，到了这个时刻检查结果也可能变化（有对象过了时限、问题变严重、
        对象移出统计范围、日期变了）。到这个时刻之前、数据也没变时，这个检查项可以跳过。"""
        if at is not None and (self.next_due is None or at < self.next_due):
            self.next_due = at

    def midnight(self, day: date) -> datetime:
        """租户时区里某一天的零点。"""
        return day_bounds(day, self.tz)[0]

    async def earliest(self, column: Any, *conditions: Any) -> Any:
        """符合条件的对象里最早的时间（没有时为空）。"""
        return await self.session.scalar(select(func.min(column)).where(*conditions))

    def with_params(self, params: dict[str, int]) -> "Scope":
        return Scope(self.session, self.tenant_id, self.now, self.tz, params, self._cache)

    def local(self, value: datetime | None) -> str:
        return value.astimezone(self.tz).strftime("%m-%d %H:%M") if value else ""

    async def _active_ids(self) -> set[uuid.UUID]:
        if "active" not in self._cache:
            rows = await self.session.scalars(
                select(Staff.id).where(Staff.status == StaffStatus.ACTIVE)
            )
            self._cache["active"] = set(rows.all())
        active: set[uuid.UUID] = self._cache["active"]
        return active

    async def names(self) -> dict[uuid.UUID, str]:
        if "names" not in self._cache:
            rows = await self.session.execute(select(Staff.id, Staff.display_name))
            self._cache["names"] = {staff_id: name for staff_id, name in rows}
        names: dict[uuid.UUID, str] = self._cache["names"]
        return names

    async def holders(self, permission: Permission) -> list[uuid.UUID]:
        key = f"perm:{permission.value}"
        if key not in self._cache:
            self._cache[key] = await assign.staff_with(self.session, permission)
        holders: list[uuid.UUID] = self._cache[key]
        return holders

    async def admins(self) -> list[uuid.UUID]:
        """租户管理员（只有他们有 tenant:manage）。"""
        return await self.holders(Permission.TENANT_MANAGE)

    async def people(
        self,
        *staff_ids: uuid.UUID | None,
        permission: Permission | None = None,
        also: Permission | None = None,
    ) -> list[uuid.UUID]:
        """负责人：对象上的员工（启用的）；没有时是有 permission 的员工；also 的员工总是加上；
        都没有时是租户管理员。"""
        active = await self._active_ids()
        found = [s for s in dict.fromkeys(staff_ids) if s is not None and s in active]
        if not found and permission is not None:
            found = list(await self.holders(permission))
        if also is not None:
            found = list(dict.fromkeys([*found, *await self.holders(also)]))
        return found or list(await self.admins())


@dataclass(frozen=True)
class Check:
    code: str
    category: str
    title: str
    description: str
    run: Callable[[Scope], Awaitable[list[Hit]]]
    # 每小时检查也做（需要及时处理的）；否则只在每日巡检里做。
    hourly: bool = False
    # 套餐需要包含的功能（AI 唤醒本身需要 ai）。
    feature: str | None = None
    params: tuple[Param, ...] = ()
    # 检查时读的表（另加 COMMON_DOMAINS）：增量更新索引里这些表都没有变化时可以跳过。
    domains: tuple[str, ...] = ()

    @property
    def watched(self) -> tuple[str, ...]:
        return (*self.domains, *COMMON_DOMAINS)

    def resolve(self, overrides: dict[str, int] | None = None) -> dict[str, int]:
        """口径数字：默认值，加上设置里改过的（超出范围的按边界）。"""
        values: dict[str, int] = {}
        for param in self.params:
            raw = (overrides or {}).get(param.name, param.default)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                value = param.default
            values[param.name] = min(max(value, param.minimum), param.maximum)
        return values


def digest(check: Check, params: dict[str, int]) -> str:
    """口径的摘要（版本、检查项、数字）：和上次检查时不同就重新检查。"""
    raw = json.dumps({"v": VERSION, "c": check.code, "p": params}, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def money(value: Decimal | float | int | None) -> str:
    return f"¥{Decimal(value or 0):,.2f}"


def _over(waited: timedelta, hours: int) -> int:
    """标题里的"超过 N 小时"：已经超过 24 小时（严重）时是 24，否则是设定的小时数。"""
    return 24 if waited >= CRITICAL_WAIT and hours < 24 else hours


def _order_link(order_id: uuid.UUID) -> str:
    return f"/orders?id={order_id}"


# ---- 订单 ----


async def order_review(scope: Scope) -> list[Hit]:
    hours = scope.params["hours"]
    limit = scope.now - timedelta(hours=hours)
    pending = Order.status == OrderStatus.PENDING_REVIEW
    rows = await scope.session.execute(
        select(Order.id, Order.no, Order.submitted_at, Order.total)
        .where(pending, Order.submitted_at <= limit)
        .order_by(Order.submitted_at)
        .limit(MAX_HITS)
    )
    # 还没到时限的：最早提交的那一单到时限时再检查。
    upcoming = await scope.earliest(Order.submitted_at, pending, Order.submitted_at > limit)
    if upcoming is not None:
        scope.due(upcoming + timedelta(hours=hours))
    reviewers = await scope.people(permission=Permission.ORDER_REVIEW)
    hits = []
    for order_id, no, submitted_at, total in rows:
        assert submitted_at is not None
        waited = scope.now - submitted_at
        critical = waited >= CRITICAL_WAIT
        if not critical:
            scope.due(submitted_at + CRITICAL_WAIT)
        hits.append(
            Hit(
                key=f"order:{order_id}",
                title=f"订单 {no} 提交审核超过 {_over(waited, hours)} 小时还没审核",
                detail=f"{scope.local(submitted_at)} 提交审核，合计 {money(total)}，请尽快审核。",
                severity=Severity.CRITICAL if critical else Severity.WARNING,
                assignees=reviewers,
                link=_order_link(order_id),
                entity_type="order",
                entity_id=order_id,
                data={"since": submitted_at.isoformat()},
            )
        )
    return hits


async def order_late(scope: Scope) -> list[Hit]:
    days = scope.params["days"]
    deadline = func.coalesce(Order.expected_at, Order.confirmed_at + timedelta(days=days))
    open_ = and_(
        Order.status.in_((OrderStatus.CONFIRMED, OrderStatus.FULFILLING)),
        Order.confirmed_at.is_not(None),
    )
    rows = await scope.session.execute(
        select(Order.id, Order.no, Order.assignee_id, Order.expected_at, deadline.label("due"))
        .where(open_, deadline < scope.now)
        .order_by(deadline)
        .limit(MAX_HITS)
    )
    scope.due(await scope.earliest(deadline, open_, deadline >= scope.now))
    hits = []
    for order_id, no, assignee_id, expected_at, due in rows:
        critical = scope.now - due >= CRITICAL_LATE
        if not critical:
            scope.due(due + CRITICAL_LATE)
        basis = (
            f"预计 {scope.local(expected_at)}"
            if expected_at
            else f"确认后 {days} 天（{scope.local(due)}）"
        )
        hits.append(
            Hit(
                key=f"order:{order_id}",
                title=f"订单 {no} 超期 3 天以上仍未发货" if critical else f"订单 {no} 超期仍未发货",
                detail=f"{basis}应发货或完成，目前还在处理。",
                severity=Severity.CRITICAL if critical else Severity.WARNING,
                assignees=await scope.people(assignee_id, permission=Permission.ORDER_REVIEW),
                link=_order_link(order_id),
                entity_type="order",
                entity_id=order_id,
                data={"since": due.isoformat()},
            )
        )
    return hits


async def order_below_cost(scope: Scope) -> list[Hit]:
    days = scope.params["days"]
    rows = await scope.session.execute(
        select(
            Order.id,
            Order.no,
            Order.confirmed_at,
            OrderItem.name,
            OrderItem.unit_price,
            OrderItem.cost_price,
        )
        .join(
            OrderItem, and_(OrderItem.tenant_id == Order.tenant_id, OrderItem.order_id == Order.id)
        )
        .where(
            Order.confirmed_at >= scope.now - timedelta(days=days),
            Order.status != OrderStatus.CANCELLED,
            OrderItem.unit_price.is_not(None),
            OrderItem.cost_price.is_not(None),
            OrderItem.unit_price < OrderItem.cost_price,
        )
        .order_by(Order.confirmed_at.desc(), OrderItem.sort)
    )
    lines: dict[uuid.UUID, tuple[str, list[str]]] = {}
    for order_id, no, confirmed_at, name, unit_price, cost_price in rows:
        assert confirmed_at is not None
        # 确认满设定的天数后不再统计（问题自动消除）。
        scope.due(confirmed_at + timedelta(days=days))
        lines.setdefault(order_id, (no, []))[1].append(
            f"{name} 成交价 {money(unit_price)}，成本价 {money(cost_price)}"
        )
    people = await scope.people(permission=Permission.PROFIT_VIEW)
    return [
        Hit(
            key=f"order:{order_id}",
            title=f"订单 {no} 低于成本价成交",
            detail="；".join(items[:3]) + ("……" if len(items) > 3 else ""),
            severity=Severity.WARNING,
            assignees=people,
            link=_order_link(order_id),
            entity_type="order",
            entity_id=order_id,
            data={"items": len(items)},
        )
        for order_id, (no, items) in list(lines.items())[:MAX_HITS]
    ]


# ---- 应收 ----


async def receivable_promise(scope: Scope) -> list[Hit]:
    cal = await finance.calendar(scope.session, scope.tenant_id, scope.now)
    promised_unpaid = and_(finance.receivable(), Order.promise_date.is_not(None))
    rows = await scope.session.execute(
        select(Order.id, Order.no, Order.promise_date, finance.outstanding().label("left"))
        .where(promised_unpaid, Order.promise_date < cal.today)
        .order_by(Order.promise_date)
        .limit(MAX_HITS)
    )
    upcoming = await scope.earliest(
        Order.promise_date, promised_unpaid, Order.promise_date >= cal.today
    )
    if upcoming is not None:
        scope.due(scope.midnight(upcoming + timedelta(days=1)))
    people = await scope.people(permission=Permission.FINANCE_MANAGE)
    hits = []
    for order_id, no, promised, left in rows:
        critical = (cal.today - promised).days >= CRITICAL_UNPAID_DAYS
        if not critical:
            scope.due(scope.midnight(promised + timedelta(days=CRITICAL_UNPAID_DAYS)))
        title = (
            f"订单 {no} 过了客户承诺的付款日 {CRITICAL_UNPAID_DAYS} 天以上"
            if critical
            else f"订单 {no} 过了客户承诺的付款日"
        )
        hits.append(
            Hit(
                key=f"order:{order_id}",
                title=title,
                detail=f"客户承诺 {promised:%m-%d} 付款，还有 {money(left)} 没收。",
                severity=Severity.CRITICAL if critical else Severity.WARNING,
                assignees=people,
                link=f"/receivables?id={order_id}",
                entity_type="order",
                entity_id=order_id,
                data={"promised": promised.isoformat(), "outstanding": str(left)},
            )
        )
    return hits


# ---- 加工 ----


async def shortage_restock(scope: Scope) -> list[Hit]:
    waiting = and_(
        OrderItem.work_status == WorkStatus.OUT_OF_STOCK,
        OrderItem.restock_date.is_not(None),
        Order.status.notin_((OrderStatus.COMPLETED, OrderStatus.CANCELLED)),
    )
    joined = and_(OrderItem.tenant_id == Order.tenant_id, OrderItem.order_id == Order.id)
    rows = await scope.session.execute(
        select(Order.id, Order.no, Order.assignee_id, OrderItem.name, OrderItem.restock_date)
        .join(OrderItem, joined)
        .where(waiting, OrderItem.restock_date < scope.today)
        .order_by(OrderItem.restock_date)
    )
    upcoming = await scope.session.scalar(
        select(func.min(OrderItem.restock_date))
        .join(Order, joined)
        .where(waiting, OrderItem.restock_date >= scope.today)
    )
    if upcoming is not None:
        scope.due(scope.midnight(upcoming + timedelta(days=1)))
    found: dict[uuid.UUID, tuple[str, uuid.UUID | None, list[str]]] = {}
    for order_id, no, assignee_id, name, restock in rows:
        found.setdefault(order_id, (no, assignee_id, []))[2].append(
            f"{name}（预计 {restock:%m-%d} 到货）"
        )
    hits = []
    for order_id, (no, assignee_id, items) in list(found.items())[:MAX_HITS]:
        hits.append(
            Hit(
                key=f"order:{order_id}",
                title=f"订单 {no} 缺货的预计到货日已过",
                detail="仍然缺货：" + "、".join(items[:3]) + "。请确认到货时间并告知客户。",
                severity=Severity.WARNING,
                assignees=await scope.people(
                    assignee_id,
                    permission=Permission.ORDER_REVIEW,
                    also=Permission.INVENTORY_MANAGE,
                ),
                link=_order_link(order_id),
                entity_type="order",
                entity_id=order_id,
            )
        )
    return hits


async def production_stalled(scope: Scope) -> list[Hit]:
    days = scope.params["days"]
    limit = scope.now - timedelta(days=days)
    working = and_(
        Order.worker_id.is_not(None),
        Order.processed_at.is_(None),
        Order.status.in_((OrderStatus.CONFIRMED, OrderStatus.FULFILLING)),
    )
    rows = await scope.session.execute(
        select(Order.id, Order.no, Order.worker_id, Order.claimed_at)
        .where(working, Order.claimed_at <= limit)
        .order_by(Order.claimed_at)
        .limit(MAX_HITS)
    )
    upcoming = await scope.earliest(Order.claimed_at, working, Order.claimed_at > limit)
    if upcoming is not None:
        scope.due(upcoming + timedelta(days=days))
    names = await scope.names()
    hits = []
    for order_id, no, worker_id, claimed_at in rows:
        assert worker_id is not None and claimed_at is not None
        hits.append(
            Hit(
                key=f"order:{order_id}",
                title=f"订单 {no} 领取超过 {days} 天还没加工完成",
                detail=f"{names.get(worker_id, '工人')} {scope.local(claimed_at)} 领取。",
                severity=Severity.WARNING,
                assignees=await scope.people(worker_id, also=Permission.PRODUCTION_ASSIGN),
                link=f"/production?order={order_id}",
                entity_type="order",
                entity_id=order_id,
                data={"since": claimed_at.isoformat()},
            )
        )
    return hits


# ---- 仓库 ----


async def warehouse_pending(scope: Scope) -> list[Hit]:
    hours = scope.params["hours"]
    limit = scope.now - timedelta(hours=hours)
    pending = StockDocument.status == DocumentStatus.PENDING
    rows = await scope.session.execute(
        select(StockDocument.id, StockDocument.no, StockDocument.kind, StockDocument.submitted_at)
        .where(pending, StockDocument.submitted_at <= limit)
        .order_by(StockDocument.submitted_at)
        .limit(MAX_HITS)
    )
    upcoming = await scope.earliest(
        StockDocument.submitted_at, pending, StockDocument.submitted_at > limit
    )
    if upcoming is not None:
        scope.due(upcoming + timedelta(hours=hours))
    keepers = await scope.people(permission=Permission.WAREHOUSE_CONFIRM)
    hits = []
    for doc_id, no, kind, submitted_at in rows:
        waited = scope.now - submitted_at
        critical = waited >= CRITICAL_WAIT
        if not critical:
            scope.due(submitted_at + CRITICAL_WAIT)
        label = KIND_LABELS.get(kind, "单据")
        hits.append(
            Hit(
                key=f"document:{doc_id}",
                title=f"{label} {no} 提交超过 {_over(waited, hours)} 小时还没确认",
                detail=f"{scope.local(submitted_at)} 提交，等仓管确认。",
                severity=Severity.CRITICAL if critical else Severity.WARNING,
                assignees=keepers,
                link=f"/warehouse?doc={doc_id}",
                entity_type="document",
                entity_id=doc_id,
                data={"since": submitted_at.isoformat()},
            )
        )
    return hits


# 可用库存 = 现有库存 - 已确认订单要发出的 - 待确认领料单要领的。
STOCK_DOMAINS = ("products", "orders", "order_items", "stock_documents", "stock_document_lines")


async def stock_low(scope: Scope) -> list[Hit]:
    rows = (
        await scope.session.execute(
            select(Product.id, Product.name, Product.kind, Product.unit).where(
                Product.stock.is_not(None), Product.status == ProductStatus.ON
            )
        )
    ).all()
    levels = await stock.levels(scope.session, [r.id for r in rows])
    people = await scope.people(permission=Permission.INVENTORY_MANAGE)
    hits = []
    for product_id, name, kind, unit in rows:
        level = levels.get(product_id)
        if level is None or not level.low:
            continue
        available = level.available
        assert available is not None
        what = "材料" if kind == ProductKind.MATERIAL else "成品"
        alert = f"，预警值 {stock.fmt(level.alert)}{unit}" if level.alert is not None else ""
        hits.append(
            Hit(
                key=f"product:{product_id}",
                title=f"{what}「{name}」库存不足",
                detail=f"可用 {stock.fmt(available)}{unit}（现有 {stock.fmt(level.stock)}{unit}）"
                f"{alert}。",
                severity=Severity.CRITICAL if available < 0 else Severity.WARNING,
                assignees=people,
                link="/warehouse?low=1",
                entity_type="product",
                entity_id=product_id,
                data={"available": str(available)},
            )
        )
        if len(hits) >= MAX_HITS:
            break
    return hits


# ---- 待办 ----


async def todo_unclaimed(scope: Scope) -> list[Hit]:
    hours = scope.params["hours"]
    limit = scope.now - timedelta(hours=hours)
    unclaimed = and_(Todo.status.in_(ACTIVE), Todo.assignee_id.is_(None))
    rows = await scope.session.execute(
        select(Todo.id, Todo.no, Todo.title, Todo.created_at)
        .where(unclaimed, Todo.created_at <= limit)
        .order_by(Todo.created_at)
        .limit(MAX_HITS)
    )
    upcoming = await scope.earliest(Todo.created_at, unclaimed, Todo.created_at > limit)
    if upcoming is not None:
        scope.due(upcoming + timedelta(hours=hours))
    people = await scope.people(permission=Permission.TODO_ASSIGN)
    hits = []
    for todo_id, no, title, created_at in rows:
        waited = scope.now - created_at
        critical = waited >= CRITICAL_WAIT
        if not critical:
            scope.due(created_at + CRITICAL_WAIT)
        hits.append(
            Hit(
                key=f"todo:{todo_id}",
                title=f"待办 {no} 超过 {_over(waited, hours)} 小时没人认领",
                detail=f"「{title}」，{scope.local(created_at)} 进入待认领，请分派给合适的人。",
                severity=Severity.CRITICAL if critical else Severity.WARNING,
                assignees=people,
                link=f"/todos?id={todo_id}",
                entity_type="todo",
                entity_id=todo_id,
                data={"since": created_at.isoformat()},
            )
        )
    return hits


async def todo_overdue(scope: Scope) -> list[Hit]:
    count = scope.params["count"]
    assigned = and_(Todo.status.in_(ACTIVE), Todo.assignee_id.is_not(None))
    rows = await scope.session.execute(
        select(Todo.assignee_id, func.count())
        .where(assigned, Todo.due_at < scope.now)
        .group_by(Todo.assignee_id)
        .having(func.count() >= count)
        .order_by(func.count().desc())
        .limit(MAX_HITS)
    )
    # 下一条到期的待办逾期时，有人的逾期条数会变。
    scope.due(await scope.earliest(Todo.due_at, assigned, Todo.due_at >= scope.now))
    names = await scope.names()
    hits = []
    for staff_id, overdue in rows:
        assert staff_id is not None
        name = names.get(staff_id, "员工")
        hits.append(
            Hit(
                key=f"staff:{staff_id}",
                title=f"{name} 有 {overdue} 条待办已逾期",
                detail="逾期的待办积压较多，可以调整优先级或改派。",
                severity=Severity.CRITICAL if overdue >= count * 2 else Severity.WARNING,
                assignees=await scope.people(staff_id, also=Permission.TODO_ASSIGN),
                link="/todos?due=overdue",
                entity_type="staff",
                entity_id=staff_id,
                data={"overdue": overdue},
            )
        )
    return hits


# ---- 客服 ----


async def intent_no_order(scope: Scope) -> list[Hit]:
    days = scope.params["days"]
    later_order = exists().where(
        Order.tenant_id == ChatSession.tenant_id,
        Order.customer_id == ChatSession.customer_id,
        Order.created_at >= ChatSession.created_at,
        Order.status != OrderStatus.CANCELLED,
    )
    rows = await scope.session.execute(
        select(
            ChatSession.id,
            ChatSession.customer_id,
            ChatSession.assignee_id,
            ChatSession.closed_at,
            SessionIntent.peak_stage,
            SessionIntent.peak_at,
            Customer.display_name,
            Customer.owner_id,
        )
        .join(
            SessionIntent,
            and_(
                SessionIntent.tenant_id == ChatSession.tenant_id,
                SessionIntent.session_id == ChatSession.id,
            ),
        )
        .join(
            Customer,
            and_(
                Customer.tenant_id == ChatSession.tenant_id, Customer.id == ChatSession.customer_id
            ),
        )
        .where(
            ChatSession.status == SessionStatus.CLOSED,
            SessionIntent.peak_stage >= 3,
            SessionIntent.peak_at >= scope.now - timedelta(days=days),
            ~later_order,
        )
        .order_by(SessionIntent.peak_stage.desc(), ChatSession.closed_at.desc())
        .limit(MAX_HITS)
    )
    hits = []
    for session_id, customer_id, assignee_id, closed_at, peak, peak_at, name, owner_id in rows:
        assert peak is not None and peak_at is not None
        # 意向出现满设定的天数后不再提醒。
        scope.due(peak_at + timedelta(days=days))
        ready = peak >= 4
        hits.append(
            Hit(
                key=f"session:{session_id}",
                title=f"{name}{'准备下单' if ready else '有明确的购买意向'}，但还没有订单",
                detail=f"会话 {scope.local(closed_at)} 结束，之后没有这个客户的订单，"
                "可以回访跟进。",
                severity=Severity.WARNING if ready else Severity.INFO,
                assignees=await scope.people(
                    assignee_id, owner_id, permission=Permission.SESSION_READ_ALL
                ),
                link=f"/customers?customer={customer_id}",
                entity_type="session",
                entity_id=session_id,
                data={"stage": peak},
            )
        )
    return hits


async def unhappy_customer(scope: Scope) -> list[Hit]:
    days = scope.params["days"]
    follow_up = exists().where(
        Todo.tenant_id == ChatSession.tenant_id,
        Todo.customer_id == ChatSession.customer_id,
        Todo.created_at >= ChatSession.created_at,
        Todo.status != TodoStatus.REJECTED,
    )
    rows = await scope.session.execute(
        select(
            ChatSession.id,
            ChatSession.customer_id,
            ChatSession.assignee_id,
            ChatSession.closed_at,
            ChatSession.csat,
            SessionIntent.emotion,
            Customer.display_name,
        )
        .join(
            Customer,
            and_(
                Customer.tenant_id == ChatSession.tenant_id, Customer.id == ChatSession.customer_id
            ),
        )
        .outerjoin(
            SessionIntent,
            and_(
                SessionIntent.tenant_id == ChatSession.tenant_id,
                SessionIntent.session_id == ChatSession.id,
            ),
        )
        .where(
            ChatSession.status == SessionStatus.CLOSED,
            ChatSession.closed_at >= scope.now - timedelta(days=days),
            or_(ChatSession.csat <= 2, SessionIntent.emotion >= 1.5),
            ~follow_up,
        )
        .order_by(ChatSession.closed_at.desc())
        .limit(MAX_HITS)
    )
    hits = []
    for session_id, customer_id, assignee_id, closed_at, csat, emotion, name in rows:
        assert closed_at is not None
        scope.due(closed_at + timedelta(days=days))
        reason = f"评价 {csat} 星" if csat is not None and csat <= 2 else "情绪激动"
        hits.append(
            Hit(
                key=f"session:{session_id}",
                title=f"{name} {reason}，还没有跟进",
                detail=f"会话 {scope.local(closed_at)} 结束，之后没有给这个客户建待办。",
                severity=Severity.WARNING,
                assignees=await scope.people(assignee_id, permission=Permission.SESSION_READ_ALL),
                link=f"/customers?customer={customer_id}",
                entity_type="session",
                entity_id=session_id,
                data={"csat": csat, "emotion": emotion},
            )
        )
    return hits


# ---- 趋势 ----


async def _orders_on(scope: Scope, day: date) -> int:
    start, end = day_bounds(day, scope.tz)
    count = await scope.session.scalar(
        select(func.count())
        .select_from(Order)
        .where(Order.created_at >= start, Order.created_at < end, Order.status != OrderStatus.DRAFT)
    )
    return int(count or 0)


async def orders_drop(scope: Scope) -> list[Hit]:
    # 比较的是"昨天"：明天零点重新比较。
    scope.due(scope.midnight(scope.today + timedelta(days=1)))
    yesterday = scope.today - timedelta(days=1)
    before = [await _orders_on(scope, yesterday - timedelta(weeks=w)) for w in range(1, 5)]
    average = sum(before) / len(before)
    if average < scope.params["min_avg"]:
        return []
    count = await _orders_on(scope, yesterday)
    drop = 1 - count / average
    if drop * 100 < scope.params["percent"]:
        return []
    return [
        Hit(
            key="tenant",
            title=f"昨天的订单比平时少了 {round(drop * 100)}%",
            detail=f"昨天 {count} 单，过去 4 周同一天平均 {average:.1f} 单。",
            severity=Severity.WARNING,
            assignees=list(await scope.admins()),
            link="/reports",
            data={"count": count, "average": round(average, 1), "day": yesterday.isoformat()},
        )
    ]


async def _satisfaction(scope: Scope, start: datetime, end: datetime) -> tuple[int, int]:
    rated, satisfied = (
        await scope.session.execute(
            select(func.count(), func.count().filter(ChatSession.csat >= 4)).where(
                ChatSession.csat.is_not(None),
                ChatSession.closed_at >= start,
                ChatSession.closed_at < end,
            )
        )
    ).one()
    return int(rated or 0), int(satisfied or 0)


async def csat_drop(scope: Scope) -> list[Hit]:
    # 统计范围随时间移动：每天重新比较一次。
    scope.due(scope.midnight(scope.today + timedelta(days=1)))
    week_start = scope.now - timedelta(days=7)
    rated, satisfied = await _satisfaction(scope, week_start, scope.now)
    if rated < scope.params["min_count"]:
        return []
    before_rated, before_satisfied = await _satisfaction(
        scope, week_start - timedelta(days=28), week_start
    )
    if not before_rated:
        return []
    rate, before = satisfied / rated, before_satisfied / before_rated
    points = round((before - rate) * 100)
    if points < scope.params["points"]:
        return []
    return [
        Hit(
            key="tenant",
            title=f"近 7 天的满意率下降了 {points} 个百分点",
            detail=f"近 7 天 {rate:.0%}（{rated} 条评价），之前 4 周 {before:.0%}。",
            severity=Severity.WARNING,
            assignees=list(await scope.admins()),
            link="/reports",
            data={"rate": round(rate, 4), "before": round(before, 4), "rated": rated},
        )
    ]


# ---- 知识 ----


async def kb_backlog(scope: Scope) -> list[Hit]:
    days = scope.params["days"]
    limit = scope.now - timedelta(days=days)
    pending = KbCandidate.status == CandidateStatus.PENDING
    policy_conflict = and_(
        pending,
        KbCandidate.source == CandidateSource.POLICY,
        KbCandidate.kind == CandidateKind.CONFLICT,
    )
    total = int(
        await scope.session.scalar(select(func.count()).select_from(KbCandidate).where(pending))
        or 0
    )
    stale_conflicts = int(
        await scope.session.scalar(
            select(func.count())
            .select_from(KbCandidate)
            .where(policy_conflict, KbCandidate.created_at <= limit)
        )
        or 0
    )
    upcoming = await scope.earliest(
        KbCandidate.created_at, policy_conflict, KbCandidate.created_at > limit
    )
    if upcoming is not None:
        scope.due(upcoming + timedelta(days=days))
    if total < scope.params["count"] and not stale_conflicts:
        return []
    detail = f"审核台有 {total} 条建议待处理"
    if stale_conflicts:
        detail += f"，其中 {stale_conflicts} 条和现行制度冲突的已经超过 {days} 天"
    return [
        Hit(
            key="tenant",
            title="知识库的建议积压，等待审核",
            detail=detail + "。",
            severity=Severity.WARNING if stale_conflicts else Severity.INFO,
            assignees=await scope.people(permission=Permission.KB_MANAGE),
            link="/knowledge?tab=review",
            data={"pending": total, "stale_conflicts": stale_conflicts},
        )
    ]


# ---- 客户 ----


async def prospect_due(scope: Scope) -> list[Hit]:
    """跟进中的意向客户到了下次跟进日期（§35.4），按跟进人合并成一条；跟进以后（下次跟进日期改到
    以后）自动消除。过了日期的是警告，过了 3 天以上的是严重。"""
    today = scope.today
    active = and_(
        Opportunity.status == OpportunityStatus.ACTIVE,
        Opportunity.next_follow_at.is_not(None),
    )
    rows = (
        await scope.session.execute(
            select(
                Opportunity.owner_id,
                Opportunity.next_follow_at,
                Customer.display_name,
            )
            .join(Customer, Customer.id == Opportunity.customer_id)
            .where(active, Opportunity.next_follow_at <= today)
            .order_by(Opportunity.next_follow_at, Customer.display_name)
        )
    ).all()
    # 日期变了：今天该跟进的变成逾期，逾期的变严重，新的一批到了日期。
    upcoming = await scope.earliest(
        Opportunity.next_follow_at, active, Opportunity.next_follow_at > today
    )
    if rows:
        scope.due(scope.midnight(today + timedelta(days=1)))
    elif upcoming is not None:
        scope.due(scope.midnight(upcoming))
    groups: dict[uuid.UUID | None, list[tuple[date, str]]] = {}
    for owner_id, next_at, name in rows:
        assert next_at is not None
        groups.setdefault(owner_id, []).append((next_at, name))
    names = await scope.names()
    hits = []
    for owner_id, items in list(groups.items())[:MAX_HITS]:
        overdue = [d for d, _ in items if d < today]
        late = [d for d in overdue if today - d > CRITICAL_LATE]
        who = names.get(owner_id, "员工") if owner_id else None
        detail = "、".join(n for _, n in items[:3])
        detail += f"等 {len(items)} 位。" if len(items) > 3 else "。"
        if overdue:
            detail += f"其中 {len(overdue)} 位已经过了下次跟进日期（最早 {min(overdue):%m-%d}）。"
        view = "overdue" if overdue else "today"
        link = f"/customers?tab=prospects&view={view}"
        hits.append(
            Hit(
                key=f"staff:{owner_id}" if owner_id else "unassigned",
                title=(
                    f"{who} 有 {len(items)} 条商机该跟进了"
                    if who
                    else f"有 {len(items)} 条商机该跟进了，还没有负责人"
                ),
                detail=detail,
                severity=(
                    Severity.CRITICAL if late else Severity.WARNING if overdue else Severity.INFO
                ),
                assignees=await scope.people(owner_id, permission=Permission.CUSTOMER_ASSIGN),
                link=link + (f"&owner={owner_id}" if owner_id else ""),
                entity_type="staff" if owner_id else None,
                entity_id=owner_id,
                data={"due": len(items), "overdue": len(overdue)},
            )
        )
    return hits


# ---- 合同 ----


async def contract_expiring(scope: Scope) -> list[Hit]:
    """已签署、结束日期在合同设置的天数内的合同（§34.9）。7 天内算严重；过了结束日期自动消除。"""
    days = (await contract_settings.load(scope.session, scope.tenant_id)).expiring_days
    today = scope.today
    horizon = today + timedelta(days=days)
    signed = and_(Contract.status == ContractStatus.SIGNED, Contract.end_date.is_not(None))
    rows = await scope.session.execute(
        select(
            Contract.id,
            Contract.no,
            Contract.title,
            Contract.end_date,
            Contract.owner_id,
            Contract.created_by,
        )
        .where(signed, Contract.end_date >= today, Contract.end_date <= horizon)
        .order_by(Contract.end_date)
        .limit(MAX_HITS)
    )
    # 下一份合同进入提醒范围的时刻。
    upcoming = await scope.earliest(Contract.end_date, signed, Contract.end_date > horizon)
    if upcoming is not None:
        scope.due(scope.midnight(upcoming - timedelta(days=days)))
    hits = []
    for contract_id, no, title, end, owner_id, creator in rows:
        assert end is not None
        left = (end - today).days
        # 到期的第二天消除；还没到 7 天的到时变严重。
        scope.due(scope.midnight(end + timedelta(days=1)))
        if left > CONTRACT_CRITICAL_DAYS:
            scope.due(scope.midnight(end - timedelta(days=CONTRACT_CRITICAL_DAYS)))
        hits.append(
            Hit(
                key=f"contract:{contract_id}",
                title=f"合同 {no}「{title}」快到期了",
                detail=f"结束日期 {end:%Y-%m-%d}，到期前和客户确认续签还是结束。",
                severity=Severity.CRITICAL if left <= CONTRACT_CRITICAL_DAYS else Severity.WARNING,
                assignees=await scope.people(
                    owner_id, creator, permission=Permission.CONTRACT_MANAGE
                ),
                link=f"/contracts?id={contract_id}",
                entity_type="contract",
                entity_id=contract_id,
                data={"end_date": end.isoformat()},
            )
        )
    return hits


# ---- 系统 ----


async def mailbox_error(scope: Scope) -> list[Hit]:
    rows = await scope.session.execute(
        select(MailAccount.id, MailAccount.address, MailAccount.last_error).where(
            MailAccount.status == MailStatus.PAUSED
        )
    )
    people = await scope.people(permission=Permission.SETTINGS_MANAGE)
    return [
        Hit(
            key=f"mailbox:{mailbox_id}",
            title=f"邮箱 {address} 收信失败，已暂停收信",
            detail=f'{error or "登录连续失败"}。请检查授权码后点"立即收取"。',
            severity=Severity.CRITICAL,
            assignees=people,
            link="/settings?tab=mail",
            entity_type="mailbox",
            entity_id=mailbox_id,
        )
        for mailbox_id, address, error in rows
    ]


PRINTER_PROBLEMS: dict[str, str] = {
    PrinterStatus.OFFLINE: "离线",
    PrinterStatus.ABNORMAL: "异常（一般是缺纸）",
    PrinterStatus.MISCONFIGURED: "账号或密钥不对",
}


async def printer_offline(scope: Scope) -> list[Hit]:
    rows = await scope.session.execute(
        select(Printer.id, Printer.name, Printer.status).where(
            Printer.enabled.is_(True), Printer.status.in_(tuple(PRINTER_PROBLEMS))
        )
    )
    people = await scope.people(permission=Permission.PRINT_MANAGE)
    return [
        Hit(
            key=f"printer:{printer_id}",
            title=f"打印机「{name}」{PRINTER_PROBLEMS[status]}",
            detail="加工单和领料单可能打印不出来，请检查打印机。",
            severity=Severity.WARNING,
            assignees=people,
            link="/settings?tab=print",
            entity_type="printer",
            entity_id=printer_id,
        )
        for printer_id, name, status in rows
    ]


CHECKS: tuple[Check, ...] = (
    Check(
        "order_review",
        Category.ORDER,
        "订单待审核超时",
        "提交审核超过设定的小时数还没审核",
        order_review,
        hourly=True,
        feature="orders",
        params=(Param("hours", "超过", "小时", 4, 1, 72),),
        domains=("orders",),
    ),
    Check(
        "order_late",
        Category.ORDER,
        "订单超期未发货",
        "已确认的订单过了预计时间（没有预计时间的，确认后若干天）还没发货或完成",
        order_late,
        feature="orders",
        params=(Param("days", "没有预计时间的，确认后", "天", 3, 1, 60),),
        domains=("orders",),
    ),
    Check(
        "order_below_cost",
        Category.ORDER,
        "低于成本价成交",
        "最近几天确认的订单里，有商品的成交价低于下单时的成本价",
        order_below_cost,
        feature="orders",
        params=(Param("days", "最近", "天", 7, 1, 60),),
        domains=("orders", "order_items"),
    ),
    Check(
        "receivable_promise",
        Category.RECEIVABLE,
        "承诺付款日已过",
        "客户承诺的付款日过了还没收清",
        receivable_promise,
        feature="orders",
        domains=("orders",),
    ),
    Check(
        "shortage_restock",
        Category.PRODUCTION,
        "缺货的到货日已过",
        "登记缺货时填的预计到货日期已过，仍然缺货",
        shortage_restock,
        feature="orders",
        domains=("orders", "order_items"),
    ),
    Check(
        "production_stalled",
        Category.PRODUCTION,
        "加工停滞",
        "工人领取后超过设定的天数还没完成加工",
        production_stalled,
        feature="orders",
        params=(Param("days", "领取后超过", "天", 3, 1, 60),),
        domains=("orders",),
    ),
    Check(
        "warehouse_pending",
        Category.WAREHOUSE,
        "单据待确认超时",
        "领料单、入库单提交超过设定的小时数还没确认",
        warehouse_pending,
        hourly=True,
        feature="orders",
        params=(Param("hours", "超过", "小时", 4, 1, 72),),
        domains=("stock_documents",),
    ),
    Check(
        "stock_low",
        Category.WAREHOUSE,
        "库存不足",
        "可用库存低于预警值或为负数的商品和材料",
        stock_low,
        feature="orders",
        domains=STOCK_DOMAINS,
    ),
    Check(
        "todo_unclaimed",
        Category.TODO,
        "待办没人认领",
        "进入待认领超过设定的小时数",
        todo_unclaimed,
        hourly=True,
        feature="todos",
        params=(Param("hours", "超过", "小时", 2, 1, 72),),
        domains=("todos",),
    ),
    Check(
        "todo_overdue",
        Category.TODO,
        "逾期待办积压",
        "一个人手上逾期的待办达到设定的条数",
        todo_overdue,
        feature="todos",
        params=(Param("count", "达到", "条", 5, 2, 100),),
        domains=("todos",),
    ),
    Check(
        "intent_no_order",
        Category.SERVICE,
        "高意向客户没有下单",
        "意图判断到「意向明确」及以上、会话已经结束，之后没有这个客户的订单",
        intent_no_order,
        feature="orders",
        params=(Param("days", "最近", "天", 3, 1, 30),),
        domains=("sessions", "session_intents", "customers", "orders"),
    ),
    Check(
        "unhappy_customer",
        Category.SERVICE,
        "不满意的客户没有跟进",
        "客户评价 1–2 星或者情绪激动，会话结束后没有给这个客户建待办",
        unhappy_customer,
        params=(Param("days", "最近", "天", 1, 1, 30),),
        domains=("sessions", "session_intents", "customers", "todos"),
    ),
    Check(
        "prospect_due",
        Category.CUSTOMER,
        "商机该跟进了",
        "跟进中的商机到了下次跟进日期还没跟进，按负责人合并提醒",
        prospect_due,
        domains=("opportunities", "customers"),
    ),
    Check(
        "orders_drop",
        Category.TREND,
        "订单量明显下降",
        "昨天的订单比过去 4 周同一天的平均少设定的比例（平均太少时不比较）",
        orders_drop,
        feature="orders",
        params=(
            Param("percent", "少了", "%", 50, 10, 90),
            Param("min_avg", "平均至少", "单", 5, 1, 1000),
        ),
        domains=("orders",),
    ),
    Check(
        "csat_drop",
        Category.TREND,
        "满意度明显下降",
        "近 7 天的满意率比之前 4 周低设定的百分点（评价太少时不比较）",
        csat_drop,
        params=(
            Param("points", "下降", "个百分点", 15, 5, 50),
            Param("min_count", "近 7 天至少", "条评价", 10, 1, 1000),
        ),
        domains=("sessions",),
    ),
    Check(
        "kb_backlog",
        Category.KNOWLEDGE,
        "知识建议积压",
        "审核台待处理的建议达到设定的条数，或者有和现行制度冲突的建议超过设定的天数没处理",
        kb_backlog,
        params=(
            Param("count", "待处理达到", "条", 20, 1, 1000),
            Param("days", "冲突的建议超过", "天", 7, 1, 60),
        ),
        domains=("kb_candidates",),
    ),
    Check(
        "mailbox_error",
        Category.SYSTEM,
        "邮箱收信失败",
        "邮箱登录连续失败，已暂停收信",
        mailbox_error,
        hourly=True,
        domains=("mail_accounts",),
    ),
    Check(
        "contract_expiring",
        Category.CONTRACT,
        "合同快到期",
        "已签署的合同结束日期在合同设置的天数内（默认 30 天），提醒负责人续签或结束",
        contract_expiring,
        domains=("contracts", "tenant_settings"),
    ),
    Check(
        "printer_offline",
        Category.SYSTEM,
        "打印机异常",
        "云打印机离线、缺纸或者账号密钥不对",
        printer_offline,
        hourly=True,
        domains=("printers",),
    ),
)
BY_CODE: dict[str, Check] = {check.code: check for check in CHECKS}


def applicable(features: Iterable[str]) -> list[Check]:
    """套餐包含的检查项（features 是租户可用的功能）。"""
    available = set(features)
    return [c for c in CHECKS if c.feature is None or c.feature in available]
