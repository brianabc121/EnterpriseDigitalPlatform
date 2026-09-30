"""AI 接待的订单工具（设计文档 §25.2、§25.3、§25.6）。

| 工具 | 作用 |
|---|---|
| search_products | 在商品库里查商品，只返回对客可见的字段；查不到的记为商品缺口 |
| create_order_draft | 保存客户要买的商品和收货信息；信息齐全、客户明确确认后提交审核 |
| lookup_order | 查询当前客户本人的订单（进度、物流、金额），附跟踪链接 |
| request_order_change | 记下客户修改或取消订单的要求，交给员工处理（AI 不能直接修改或取消） |

服务端不信任模型的输出：
- 商品必须在商品库里并且已上架；匹配不到的作为"未匹配商品"行保留客户的说法，由员工处理；
  一个说法对应几个差不多的商品时，请模型先让客户选择。
- 单价一律取建议零售价（模型给出的金额一概不用）；数量和行数有上限；每位客户每天的 AI 订单数有上限。
- 客户的确认必须是这一轮客户本人发出的、明确表示确认的消息（记录消息 ID）；同一次确认重试时
  不重复提交，30 分钟内同一客户相同商品的订单也不重复提交。
- 收货人、电话和地址在模型面前是占位符，执行工具前已还原，这里加密保存。
- 信息不全或客户还没确认时保存为草稿：客户中途离开时员工可以接着处理（设计文档 §25.3）。

AI 用到的商品数据只加载对客可见的字段（见 products.service.public_columns），成本价只在回复检查
（price_check）里由服务端读取、比对，从不进入模型的上下文。"试一试"和评测只校验、不保存。
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.dates import day_bounds, today
from app.core.errors import Unprocessable
from app.modules.ai import price_guard
from app.modules.billing.entitlements import has_feature
from app.modules.conversation.models import ChatSession, Message, SenderType
from app.modules.orders import service
from app.modules.orders import settings as order_settings
from app.modules.orders.actions import render
from app.modules.orders.models import (
    CLOSED,
    PAYMENT_STATUS_LABELS,
    STATUS_LABELS,
    Order,
    OrderItem,
    OrderSource,
    OrderStatus,
    PaymentMethod,
    RevisionKind,
)
from app.modules.orders.public import visitor_scope
from app.modules.orders.settings import OrderSettings
from app.modules.products import service as product_service
from app.modules.products.models import Product, ProductStatus
from app.modules.todos import ai as todo_ai
from app.modules.todos import fields as todo_fields
from app.modules.todos import notify as todo_notify
from app.modules.todos import presets, sla
from app.modules.todos import service as todo_service
from app.modules.todos.models import UNFINISHED, ActorType, Todo, TodoSource

MAX_RESULTS = 3
# 检索的最低相关度；最相关的比第二个高出这么多才算明确对应到它。
MIN_SCORE = 0.5
CLEAR_LEAD = 0.1
# 连续几次找不到客户要的商品时转人工。
MISSES_TO_HANDOFF = 2
DUPLICATE_WINDOW = timedelta(minutes=30)
LOOKUP_LIMIT = 5
# 回复里提到的商品（按名称、型号、代码）：型号至少两个字符，最多取这么多个。
MENTION_MIN = 2
MENTIONS = 50
AI = ActorType.AI.value
RECEIVER_ARGS = {"name": "receiver_name", "phone": "receiver_phone", "address": "receiver_address"}


@dataclass(frozen=True)
class OrderAi:
    """租户的 AI 订单设置：price 可以告诉客户建议零售价；ordering 可以采集并提交订单。"""

    price: bool
    ordering: bool
    required: tuple[str, ...]


async def config(ctx: AppContext, tenant_id: uuid.UUID) -> OrderAi | None:
    """开通了订单功能时的设置；没有开通时为空（不提供订单工具，也不启用价格保护）。"""
    async with ctx.db.tenant_session(tenant_id) as db:
        if not await has_feature(db, tenant_id, "orders"):
            return None
        settings = await order_settings.load(db, tenant_id)
    return OrderAi(
        price=settings.ai_price_enabled,
        ordering=settings.ai_order_mode == "collect",
        required=tuple(settings.required_fields),
    )


# ---- 工具说明 ----

_REQUIRED_LABELS = {
    "receiver_name": "收货人",
    "receiver_phone": "联系电话",
    "receiver_address": "收货地址",
    "expected_at": "期望时间",
}


def _text(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def specs(options: OrderAi) -> dict[str, tuple[str, dict[str, Any]]]:
    """订单工具的说明和参数（按租户的设置提供）。"""
    tools: dict[str, tuple[str, dict[str, Any]]] = {
        "search_products": (
            "在商品库里查找商品（名称、型号、规格、代码、俗称都可以）。客户咨询商品或价格、"
            "或者想购买时使用；有多个候选时列出来请客户选择，不要替客户决定。",
            {
                "type": "object",
                "properties": {"query": _text("客户说的商品，如「黑色的智能门锁」")},
                "required": ["query"],
            },
        ),
        "lookup_order": (
            "查询当前客户本人的订单进度（状态、物流、金额和跟踪链接）。客户询问订单时使用。",
            {
                "type": "object",
                "properties": {"order_no": _text("订单号，客户没有提供时不填（查最近的订单）")},
            },
        ),
        "request_order_change": (
            "客户要修改（如改数量、换规格、改地址）或取消已经提交的订单时，记下客户的要求交给客服"
            "处理。你不能直接修改或取消订单，也不要答应客户一定能改。",
            {
                "type": "object",
                "properties": {
                    "order_no": _text("订单号，客户没有提供时不填（最近的未完成订单）"),
                    "kind": {"type": "string", "enum": ["change", "cancel"]},
                    "request": _text("客户的要求，保持客户原意"),
                },
                "required": ["kind", "request"],
            },
        ),
    }
    if options.ordering:
        required = "、".join(_REQUIRED_LABELS[f] for f in options.required) or "无"
        tools["create_order_draft"] = (
            "保存客户要购买的商品和收货信息。客户确定了商品和数量就可以调用（信息不全时保存为草稿，"
            "并告诉你还缺什么）；客户明确回复确认订单后再次调用即提交给客服审核。"
            f"提交前必须有：{required}。价格由系统按建议零售价计算，不需要提供。",
            {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "商品行；只修改收货信息时可以不填（保持原来的商品）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": _text("search_products 结果里的代码，没有时不填"),
                                "name": _text("商品名称和规格，如「智能门锁 X1 黑色」"),
                                "quantity": {"type": "integer", "minimum": 1},
                            },
                            "required": ["name", "quantity"],
                        },
                    },
                    "receiver_name": _text("收货人（客户说的，照原样填写占位符）"),
                    "receiver_phone": _text("联系电话"),
                    "receiver_address": _text("收货地址"),
                    "expected_time": _text(
                        "客户期望的送达或上门时间，ISO 8601 格式；没有提到时不填"
                    ),
                    "payment": {
                        "type": "string",
                        "enum": [m.value for m in PaymentMethod],
                        "description": "客户提到的付款方式：online 在线付款、cod 货到付款、"
                        "deposit 预付定金、credit 先欠款或月结；没有提到时不填",
                    },
                    "note": _text("客户的其他要求"),
                },
            },
        )
    return tools


# ---- 商品：描述、首轮检索、查商品 ----


def describe(product: Product, *, price: bool) -> str:
    """给模型看的一行商品描述（只用对客可见的字段）。"""
    details = [
        f"{label} {value}"
        for label, value in (
            ("代码", product.code),
            ("型号", product.model),
            ("规格", product.spec),
            ("分类", product.category),
        )
        if value
    ]
    if product.aliases:
        details.append("也叫 " + "、".join(product.aliases[:5]))
    head = f"{product.name}（{'，'.join(details)}）" if details else product.name
    if price and product.retail_price is not None:
        return f"{head}：建议零售价 {product.retail_price:.2f} 元"
    return f"{head}：价格以客服确认为准"


@dataclass(frozen=True)
class ProductHit:
    product_id: uuid.UUID
    line: str
    score: float


async def candidates(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    queries: list[str],
    *,
    price: bool,
    vector: list[float] | None = None,
) -> list[ProductHit]:
    """首轮检索：按（改写后的）问题查商品，作为回复用的【商品信息】。第一个问题用调用方算好的
    向量做语义检索，其余的只做关键词检索。"""
    best: dict[uuid.UUID, ProductHit] = {}
    async with ctx.db.tenant_session(tenant_id) as db:
        for index, query in enumerate(queries):
            found = await product_service.search(
                None,
                db,
                tenant_id,
                query,
                limit=MAX_RESULTS,
                min_score=MIN_SCORE,
                public_only=True,
                vector=vector if index == 0 else None,
            )
            for hit in found:
                current = best.get(hit.product.id)
                if current is None or hit.score > current.score:
                    best[hit.product.id] = ProductHit(
                        hit.product.id, describe(hit.product, price=price), hit.score
                    )
    return sorted(best.values(), key=lambda h: h.score, reverse=True)[:MAX_RESULTS]


@dataclass(frozen=True)
class Found:
    output: str
    product_ids: tuple[uuid.UUID, ...] = ()
    found: bool = True


async def search_tool(
    ctx: AppContext, tenant_id: uuid.UUID, options: OrderAi, query: str, *, dry_run: bool
) -> Found:
    """search_products：只返回对客可见的字段；查不到的记为商品缺口（试一试不记）。"""
    query = query.strip()[:100]
    if not query:
        return Found("请提供要查找的商品。")
    async with ctx.db.tenant_session(tenant_id) as db:
        hits = await product_service.search(
            ctx, db, tenant_id, query, limit=MAX_RESULTS, min_score=MIN_SCORE, public_only=True
        )
        if not hits:
            if not dry_run:
                await product_service.record_gap(db, tenant_id, query)
                await db.commit()
            return Found(
                f"商品库里没有找到「{query}」。请如实告诉客户暂时没有找到这个商品，可以请客户换个"
                "说法或提供型号；不要推荐商品库以外的商品，也不要编造价格。",
                found=False,
            )
        lines = [f"{i}. {describe(h.product, price=options.price)}" for i, h in enumerate(hits, 1)]
    head = "查到这些商品（回答时只使用这里的名称、规格和价格；有多个时请客户选择）："
    return Found("\n".join([head, *lines]), tuple(h.product.id for h in hits))


async def price_check(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    reply: str,
    *,
    product_ids: set[uuid.UUID],
    session_id: uuid.UUID | None,
) -> str | None:
    """回复检查（设计文档 §25.2 第 3 条）：内部价格口径，或者出现了本次对话涉及商品（检索到的、
    回复里提到名称、型号或代码的、这个会话的订单里的）的成本价、又不是建议零售价的金额。
    成本价只在这里由服务端读取比对。"""
    if price_guard.internal_term(reply):
        return "internal_term"
    if not price_guard.amounts(reply):
        return None
    text = literal(reply)
    async with ctx.db.tenant_session(tenant_id) as db:
        ids = set(product_ids)
        ids.update(
            await db.scalars(
                select(Product.id)
                .where(
                    or_(
                        func.strpos(text, Product.name) > 0,
                        and_(
                            func.char_length(Product.model) >= MENTION_MIN,
                            func.strpos(text, Product.model) > 0,
                        ),
                        and_(Product.code.is_not(None), func.strpos(text, Product.code) > 0),
                    )
                )
                .limit(MENTIONS)
            )
        )
        if session_id is not None:
            ids.update(
                pid
                for pid in await db.scalars(
                    select(OrderItem.product_id)
                    .join(Order, Order.id == OrderItem.order_id)
                    .where(Order.session_id == session_id, OrderItem.product_id.is_not(None))
                )
                if pid is not None
            )
        if not ids:
            return None
        rows = (
            await db.execute(
                select(Product.cost_price, Product.retail_price).where(Product.id.in_(ids))
            )
        ).all()
    costs = {cost for cost, _ in rows if cost is not None}
    retail = {price for _, price in rows if price is not None}
    return "cost_amount" if price_guard.cost_amount(reply, costs=costs, retail=retail) else None


async def price_sets(session: AsyncSession) -> tuple[set[Decimal], set[Decimal]]:
    """评测用：租户全部商品的成本价和建议零售价。"""
    rows = (await session.execute(select(Product.cost_price, Product.retail_price))).all()
    return (
        {cost for cost, _ in rows if cost is not None},
        {price for _, price in rows if price is not None},
    )


# ---- 下单 ----

_NO = re.compile(
    r"不(确认|要了|用了|行|对|是|可以|买|下单|提交)|先不|再想想|考虑一下|算了|等一下|等等|取消"
    r"|别(下单|提交|买)"
)
_YES = re.compile(
    r"确认|确定|可以|就这样|没问题|好的|好滴|好嘞|行的|对的|是的|没错|下单|提交|要了|买了"
    r"|^(好|行|对|嗯|ok)",
    re.IGNORECASE,
)


def affirmative(text: str) -> bool:
    """客户明确表示确认（「确认」「可以」「就这样」……），没有否定或犹豫。"""
    stripped = text.strip()
    return not _NO.search(stripped) and bool(_YES.search(stripped))


def quantity_of(value: Any) -> int | None:
    """模型给出的数量（整数、整数的浮点数或数字字符串）；不是整数时为空。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _rows(raw: Any, max_quantity: int) -> list[tuple[str, str, int]] | str:
    """模型给出的商品行：(代码, 名称, 数量)；参数不对时返回给模型的说明。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return "items 需要是商品行的列表。"
    rows: list[tuple[str, str, int]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").strip()[:64]
        name = str(entry.get("name") or "").strip()[:128]
        if not code and not name:
            continue
        quantity = quantity_of(entry.get("quantity"))
        if quantity is None or not 1 <= quantity <= max_quantity:
            return f"「{name or code}」的数量需要是 1 到 {max_quantity} 之间的整数。"
        rows.append((code, name, quantity))
    if len(rows) > service.MAX_LINES:
        return f"每单最多 {service.MAX_LINES} 个商品行。"
    return rows


@dataclass(frozen=True)
class Resolution:
    line: service.Line | None = None
    choices: tuple[str, ...] = ()
    off_shelf: str | None = None


async def _resolve(
    ctx: AppContext, db: AsyncSession, tenant_id: uuid.UUID, code: str, name: str, quantity: int
) -> Resolution:
    """对应到商品库：代码精确匹配；否则按名称检索，唯一或明显最相关的就是它，几个差不多的
    请客户选择；都匹配不到时作为未匹配商品行（保留客户的说法）。"""
    if code:
        product = await db.scalar(
            select(Product)
            .options(product_service.public_columns())
            .where(func.lower(Product.code) == code.lower())
        )
        if product is not None:
            if product.status != ProductStatus.ON:
                return Resolution(off_shelf=product_service.label(product))
            return Resolution(service.Line(quantity, product_id=product.id))
    text = name or code
    hits = await product_service.search(
        ctx, db, tenant_id, text, limit=MAX_RESULTS, min_score=MIN_SCORE, public_only=True
    )
    if not hits:
        return Resolution(service.Line(quantity, raw_text=text, name=text))
    top = hits[0]
    if top.exact or len(hits) == 1 or top.score - hits[1].score >= CLEAR_LEAD:
        return Resolution(service.Line(quantity, product_id=top.product.id))
    return Resolution(choices=tuple(product_service.label(h.product) for h in hits))


def _signature(items: list[OrderItem]) -> list[tuple[str, int]]:
    return sorted(
        (
            str(i.product_id) if i.product_id else f"text:{(i.raw_text or i.name).lower()}",
            i.quantity,
        )
        for i in items
    )


def recap(order: Order, items: list[OrderItem], *, price: bool) -> str:
    """给客户复述的订单清单（金额按建议零售价，收货信息为掩码）。"""
    text = service.summary(items)
    if price and not order.price_pending:
        text += f"，建议零售价合计 {order.total:.2f} 元（最终价格以客服确认为准）"
    else:
        text += "，价格以客服确认为准"
    receiver = service.masked_receiver(order.receiver)
    if receiver:
        text += "；收货信息：" + " ".join(
            receiver[k] for k in service.RECEIVER_KEYS if k in receiver
        )
    return text


async def _confirmation(
    db: AsyncSession, session_id: uuid.UUID, evidence_ids: list[uuid.UUID]
) -> uuid.UUID | None:
    """这一轮客户本人发出的、明确表示确认的最后一条消息。"""
    if not evidence_ids:
        return None
    rows = (
        await db.execute(
            select(Message.id, Message.text_plain)
            .where(
                Message.id.in_(evidence_ids),
                Message.session_id == session_id,
                Message.sender_type == SenderType.CUSTOMER,
            )
            .order_by(Message.sent_at.desc(), Message.id.desc())
        )
    ).all()
    return next((mid for mid, text in rows if text and affirmative(text)), None)


async def _submitted_today(db: AsyncSession, customer_id: uuid.UUID, now: datetime) -> int:
    tz = sla.tz_of(await sla.business_hours(db))
    start, _ = day_bounds(today(tz, now), tz)
    count = await db.scalar(
        select(func.count())
        .select_from(Order)
        .where(
            Order.customer_id == customer_id,
            Order.source == OrderSource.AI_CHAT,
            Order.submitted_at >= start,
        )
    )
    return int(count or 0)


async def _duplicate(
    db: AsyncSession, customer_id: uuid.UUID, signature: list[tuple[str, int]], now: datetime
) -> Order | None:
    """30 分钟内同一客户提交过的、商品和数量完全相同的 AI 订单。"""
    recent = (
        await db.scalars(
            select(Order).where(
                Order.customer_id == customer_id,
                Order.source == OrderSource.AI_CHAT,
                Order.status != OrderStatus.DRAFT,
                Order.submitted_at >= now - DUPLICATE_WINDOW,
            )
        )
    ).all()
    for order in recent:
        if _signature(await service.load_items(db, order.id)) == signature:
            return order
    return None


def _missing(
    settings: OrderSettings, lines: int, receiver: dict[str, str], expected: bool
) -> list[str]:
    """试一试：不保存时按参数判断缺少的信息。"""
    missing = [] if lines else ["商品"]
    for field_, key in (
        ("receiver_name", "name"),
        ("receiver_phone", "phone"),
        ("receiver_address", "address"),
    ):
        if field_ in settings.required_fields and key not in receiver:
            missing.append(_REQUIRED_LABELS[field_])
    if "expected_at" in settings.required_fields and not expected:
        missing.append(_REQUIRED_LABELS["expected_at"])
    return missing


def _promise(settings: OrderSettings, no: str) -> str:
    return render(settings.promise_text, {"no": no})


@dataclass(frozen=True)
class Saved:
    output: str
    order_id: uuid.UUID | None = None
    product_ids: tuple[uuid.UUID, ...] = ()
    # 正在追问订单信息（这一轮不计入 AI 接待轮次）。
    collecting: bool = False
    # 需要转人工时的说明（如今天 AI 下单已达上限）。
    handoff: str | None = None


async def save(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    options: OrderAi,
    args: dict[str, Any],
    *,
    session_id: uuid.UUID | None,
    customer_id: uuid.UUID | None,
    evidence_ids: list[uuid.UUID],
    question: str,
    dry_run: bool,
    now: datetime | None = None,
) -> Saved:
    """create_order_draft：保存草稿；信息齐全且客户这一轮明确确认时提交审核。"""
    if not options.ordering:
        return Saved("现在不能通过 AI 下单，请告诉客户会由人工客服为您下单。")
    now = now or service.utcnow()
    async with ctx.db.tenant_session(tenant_id) as db:
        settings = await order_settings.load(db, tenant_id)
        parsed = _rows(args.get("items"), settings.max_quantity)
        if isinstance(parsed, str):
            return Saved(parsed, collecting=True)
        lines: list[service.Line] = []
        for code, name, quantity in parsed:
            resolved = await _resolve(ctx, db, tenant_id, code, name, quantity)
            if resolved.off_shelf:
                return Saved(f"「{resolved.off_shelf}」已下架，不能下单。请如实告诉客户。")
            if resolved.line is None:
                return Saved(
                    f"「{name or code}」对应多个商品：{'；'.join(resolved.choices)}。"
                    "请先让客户选择是哪一款，不要替客户决定。",
                    collecting=True,
                )
            lines.append(resolved.line)
        product_ids = tuple(line.product_id for line in lines if line.product_id is not None)
        receiver = {key: str(args.get(arg) or "").strip() for key, arg in RECEIVER_ARGS.items()}
        receiver = {key: value for key, value in receiver.items() if value}
        try:
            service.check_receiver(dict(receiver))
        except Unprocessable as exc:
            return Saved(f"{exc.message}，请向客户确认。", collecting=True)
        tz = sla.tz_of(await sla.business_hours(db))
        expected_at = todo_fields.parse_time(str(args.get("expected_time") or "") or None, tz, now)
        payment = str(args.get("payment") or "").strip()
        hint = payment if payment in {m.value for m in PaymentMethod} else None
        note = str(args.get("note") or "").strip()[:500]

        if dry_run or session_id is None or customer_id is None:
            missing = _missing(settings, len(lines), receiver, expected_at is not None)
            if missing:
                return Saved(
                    f"（试一试：不会保存）还不能提交：缺少{'、'.join(missing)}。"
                    "请先向客户询问这些信息。",
                    product_ids=product_ids,
                    collecting=True,
                )
            if not affirmative(question):
                return Saved(
                    "（试一试：不会保存）还不能提交：客户还没有确认订单。"
                    "请把订单复述给客户并请客户确认。",
                    product_ids=product_ids,
                    collecting=True,
                )
            return Saved(
                f"（试一试：不会保存）订单信息完整。请这样答复客户："
                f"{_promise(settings, settings.prefix + '20260101-0001')}",
                product_ids=product_ids,
            )

        # 同一次确认重试：已经提交过了。
        confirm_id = await _confirmation(db, session_id, evidence_ids)
        key = todo_service.dedupe_key("order", session_id, confirm_id) if confirm_id else None
        if key is not None:
            existing = await db.scalar(select(Order).where(Order.dedupe_key == key))
            if existing is not None:
                return Saved(
                    f"订单已提交（编号 {existing.no}）。请这样答复客户："
                    f"{_promise(settings, existing.no)}",
                    existing.id,
                    product_ids,
                )

        order = await db.scalar(
            select(Order)
            .where(
                Order.session_id == session_id,
                Order.source == OrderSource.AI_CHAT,
                Order.status == OrderStatus.DRAFT,
            )
            .order_by(Order.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        created = order is None
        if order is None:
            if not lines:
                return Saved("请先确定客户要买的商品和数量，再保存订单。", collecting=True)
            order = Order(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                no="",
                status=OrderStatus.DRAFT,
                source=OrderSource.AI_CHAT,
                customer_id=customer_id,
                session_id=session_id,
                receiver={},
                discount=Decimal("0"),
                evidence_message_ids=[],
                customer_note="",
                internal_note="",
                tracking_token=service.new_token(),
                created_by_type=AI,
                created_by=None,
                created_at=now,
                version=1,
            )
            old_items: list[OrderItem] = []
            before = None
        else:
            old_items = await service.load_items(db, order.id)
            before = service.snapshot(order, old_items, [])
        try:
            items = await service.build_items(db, order, lines, settings) if lines else old_items
            order.receiver, receiver_changed = await service.merge_receiver(
                ctx.keys, tenant_id, dict(order.receiver or {}), dict(receiver)
            )
        except Unprocessable as exc:
            return Saved(f"{exc.message}。请向客户确认。", collecting=True)
        if expected_at is not None:
            order.expected_at = expected_at
        if hint is not None:
            order.payment_hint = hint
        if note:
            order.customer_note = note
        order.evidence_message_ids = list(
            dict.fromkeys([*(order.evidence_message_ids or []), *evidence_ids])
        )
        service.recompute(order, items, [])

        duplicate = await _duplicate(db, customer_id, _signature(items), now)
        if duplicate is not None:
            no, state = duplicate.no, STATUS_LABELS.get(duplicate.status, duplicate.status)
            duplicate_id = duplicate.id
            await db.rollback()
            return Saved(
                f"客户刚刚已经提交过相同的订单（编号 {no}，{state}），不需要重复提交。"
                "请告诉客户订单已经在处理。",
                duplicate_id,
                product_ids,
            )

        if created:
            order.no = await service.next_no(db, tenant_id, settings, now)
            db.add(order)
            await db.flush()
            db.add_all(items)
            service.add_revision(
                db,
                order,
                kind=RevisionKind.CREATED,
                actor_type=AI,
                actor_id=None,
                before=None,
                after=service.snapshot(order, items, []),
            )
            service.event(
                db, order, "created", actor_type=AI, actor_id=None, payload={"source": order.source}
            )
        else:
            if lines:
                for item in old_items:
                    await db.delete(item)
                await db.flush()
                db.add_all(items)
            after = service.snapshot(order, items, [])
            assert before is not None
            if service.diff(before, after) or receiver_changed:
                service.add_revision(
                    db,
                    order,
                    kind=RevisionKind.EDIT,
                    actor_type=AI,
                    actor_id=None,
                    before=before,
                    after=after,
                    extra={"receiver": receiver_changed} if receiver_changed else None,
                )
        await db.flush()

        missing = service.missing_required(order, settings)
        if missing:
            await db.commit()
            return Saved(
                f"已保存为草稿（编号 {order.no}）。还不能提交：缺少{'、'.join(missing)}。"
                "请先向客户询问这些信息，补全后再调用 create_order_draft 提交。",
                order.id,
                product_ids,
                collecting=True,
            )
        if confirm_id is None or key is None:
            await db.commit()
            return Saved(
                f"已保存为草稿（编号 {order.no}）。还不能提交：客户还没有确认订单。"
                f"请把订单复述给客户并请客户确认：{recap(order, items, price=options.price)}",
                order.id,
                product_ids,
                collecting=True,
            )
        if await _submitted_today(db, customer_id, now) >= settings.ai_daily_limit:
            await db.commit()
            return Saved(
                f"这位客户今天通过 AI 提交的订单已经达到上限（{settings.ai_daily_limit} 个），"
                f"这一单已保存为草稿（编号 {order.no}），不能自动提交。"
                "请告诉客户会转交人工客服处理。",
                order.id,
                product_ids,
                handoff=f"今天 AI 下单已达上限，草稿订单 {order.no} 请人工处理",
            )

        # 提交审核：生成"订单审核"待办并提醒处理人。
        before_submit = service.snapshot(order, items, [])
        order.status = OrderStatus.PENDING_REVIEW
        order.submitted_at = now
        order.confirm_message_id = confirm_id
        order.dedupe_key = key
        todo = await service.open_review_todo(
            db, ctx.keys, order, items, actor_type=ActorType.AI, actor_id=None, now=now
        )
        service.event(
            db,
            order,
            "submitted",
            actor_type=AI,
            actor_id=None,
            payload={"todo_id": str(todo.id), "confirm_message_id": str(confirm_id)},
            public=True,
        )
        service.add_revision(
            db,
            order,
            kind=RevisionKind.STATUS,
            actor_type=AI,
            actor_id=None,
            before=before_submit,
            after=service.snapshot(order, items, []),
        )
        try:
            await db.commit()
        except IntegrityError:
            # 同一次确认并发重试：已经提交过了。
            await db.rollback()
            existing = await db.scalar(select(Order).where(Order.dedupe_key == key))
            if existing is None:
                raise
            return Saved(
                f"订单已提交（编号 {existing.no}）。请这样答复客户："
                f"{_promise(settings, existing.no)}",
                existing.id,
                product_ids,
            )
        no, order_id, todo_id = order.no, order.id, todo.id
    await todo_notify.dispatch(ctx, tenant_id=tenant_id, ids=[todo_id])
    return Saved(
        f"订单已提交（编号 {no}），客服核对价格和收款方式后会联系客户确认。"
        f"请这样答复客户：{_promise(settings, no)}",
        order_id,
        product_ids,
    )


async def collecting(ctx: AppContext, tenant_id: uuid.UUID, session_id: uuid.UUID) -> bool:
    """这个会话里有 AI 正在采集的订单草稿：追问订单信息的轮次不计入 AI 接待轮次（§25.3）。"""
    async with ctx.db.tenant_session(tenant_id) as db:
        found = await db.scalar(
            select(Order.id)
            .where(
                Order.session_id == session_id,
                Order.source == OrderSource.AI_CHAT,
                Order.status == OrderStatus.DRAFT,
            )
            .limit(1)
        )
    return found is not None


# ---- 查订单、修改或取消的要求 ----


def _describe_order(ctx: AppContext, order: Order, items: list[OrderItem], tz: Any) -> str:
    parts = [
        f"订单「{order.no}」：{STATUS_LABELS.get(order.status, order.status)}",
        service.summary(items),
        f"合计 {order.total:.2f} 元"
        + ("（部分商品待客服确认价格）" if order.price_pending else ""),
        f"收款方式：{service.payment_label(order)}，"
        f"{PAYMENT_STATUS_LABELS.get(order.payment_status, order.payment_status)}",
    ]
    if order.shipping_company or order.tracking_no:
        parts.append(f"物流：{order.shipping_company or ''} {order.tracking_no or ''}".strip())
    if order.expected_at is not None and order.status not in CLOSED:
        parts.append(f"期望时间 {order.expected_at.astimezone(tz):%m月%d日 %H:%M}")
    if order.status == OrderStatus.CANCELLED and order.cancel_reason:
        parts.append(f"取消原因：{order.cancel_reason}")
    receiver = service.masked_receiver(order.receiver)
    if receiver:
        parts.append(
            "收货信息：" + " ".join(receiver[k] for k in service.RECEIVER_KEYS if k in receiver)
        )
    if service.tracking_active(order):
        parts.append(f"查看订单进度：{service.tracking_url(ctx.settings, order.tracking_token)}")
    return "；".join(parts)


async def lookup(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    *,
    session_id: uuid.UUID | None,
    customer_id: uuid.UUID | None,
) -> str:
    """lookup_order：只能查到当前客户本人的订单；匿名访客只能查到在当前访客身份下提交的。"""
    if session_id is None or customer_id is None:
        return "（试一试：没有真实客户）"
    no = str(args.get("order_no") or "").strip().upper()[:24]
    async with ctx.db.tenant_session(tenant_id) as db:
        room_id = await todo_ai.anonymous_room(db, session_id)
        query = select(Order).where(*visitor_scope(customer_id, room_id))
        if no:
            query = query.where(Order.no == no)
        rows = (await db.scalars(query.order_by(Order.created_at.desc()).limit(LOOKUP_LIMIT))).all()
        if not rows:
            if no:
                return f"没有查到订单 {no}（只能查到这位客户本人的订单）。请向客户确认订单号。"
            return "没有查到这位客户的订单。"
        tz = sla.tz_of(await sla.business_hours(db))
        lines = [
            _describe_order(ctx, order, await service.load_items(db, order.id), tz)
            for order in rows
        ]
    return "\n".join(lines)


async def request_change(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    args: dict[str, Any],
    *,
    session_id: uuid.UUID | None,
    customer_id: uuid.UUID | None,
    evidence_ids: list[uuid.UUID],
    dry_run: bool,
    now: datetime | None = None,
) -> str:
    """request_order_change（设计文档 §25.6）：待审核的订单把要求记到订单上并提醒处理人；
    已确认的订单登记一条关联这个订单的待办（进入待确认页），由员工处理后告知客户。"""
    request = str(args.get("request") or "").strip()[:500]
    cancel = str(args.get("kind") or "") == "cancel"
    action = "取消" if cancel else "修改"
    no = str(args.get("order_no") or "").strip().upper()[:24]
    if not request:
        return f"请写明客户要{action}的内容。"
    if dry_run or session_id is None or customer_id is None:
        return "（试一试：不会记录）已记录客户的要求。请告诉客户：客服核对后会联系您确认。"
    now = now or service.utcnow()
    notify: list[uuid.UUID] = []
    async with ctx.db.tenant_session(tenant_id) as db:
        room_id = await todo_ai.anonymous_room(db, session_id)
        query = select(Order).where(*visitor_scope(customer_id, room_id))
        query = query.where(Order.no == no) if no else query.where(Order.status.not_in(CLOSED))
        order = await db.scalar(
            query.order_by(Order.created_at.desc()).limit(1).with_for_update(of=Order)
        )
        if order is None:
            return "没有查到这个订单（只能查到这位客户本人的订单）。请向客户确认订单号。"
        if order.status == OrderStatus.COMPLETED:
            return (
                f"订单 {order.no} 已经完成。客户要退换货或维修时，请用 create_todo 登记售后，"
                "并填写订单号。"
            )
        if order.status == OrderStatus.CANCELLED:
            return f"订单 {order.no} 已经取消，不能再{action}。请如实告诉客户。"
        payload: dict[str, Any] = {"kind": "cancel" if cancel else "change", "request": request}
        if order.status == OrderStatus.PENDING_REVIEW:
            todo = (
                await db.scalar(
                    select(Todo).where(Todo.id == order.review_todo_id).with_for_update()
                )
                if order.review_todo_id
                else None
            )
            if todo is not None and todo.status in UNFINISHED:
                todo_service.nudge(
                    db,
                    todo,
                    actor_type=AI,
                    detail=f"客户要求{action}订单：{request}",
                    source=TodoSource.AI_CHAT,
                    evidence=evidence_ids,
                )
                notify.append(todo.id)
            service.event(
                db, order, "change_requested", actor_type=AI, actor_id=None, payload=payload
            )
            await db.commit()
            output = (
                f"已把客户的要求记到订单 {order.no} 上，并提醒客服处理。"
                "请告诉客户：客服核对后会联系您确认。"
            )
        else:
            key = todo_service.dedupe_key("order_change", order.id, request)
            todo = await db.scalar(select(Todo).where(Todo.dedupe_key == key))
            if todo is None:
                type_ = await presets.type_by_code(db, tenant_id, presets.OTHER)
                chat = await db.get(ChatSession, session_id)
                todo = await todo_service.create(
                    db,
                    ctx.keys,
                    todo_service.Draft(
                        type=type_,
                        title=f"订单 {order.no}：客户要求{action}",
                        detail=request,
                        source=TodoSource.AI_CHAT,
                        created_by_type=ActorType.AI,
                        customer_id=order.customer_id,
                        session_id=session_id,
                        order_id=order.id,
                        channel_account_id=chat.channel_account_id if chat else None,
                        evidence_message_ids=evidence_ids,
                        dedupe_key=key,
                    ),
                    now=now,
                )
                payload["todo_id"] = str(todo.id)
                service.event(
                    db, order, "change_requested", actor_type=AI, actor_id=None, payload=payload
                )
            elif todo.status in UNFINISHED:
                todo_service.nudge(
                    db,
                    todo,
                    actor_type=AI,
                    detail=request,
                    source=TodoSource.AI_CHAT,
                    evidence=evidence_ids,
                )
            await db.commit()
            notify.append(todo.id)
            output = (
                f"已登记（编号 {todo.no}），客服确认后处理。"
                "请告诉客户：订单已经在处理中，客服核对后会联系您。"
            )
    if notify:
        await todo_notify.dispatch(ctx, tenant_id=tenant_id, ids=notify)
    return output
