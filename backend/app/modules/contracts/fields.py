"""内置填写项（设计文档 §34.2）：由系统按数据填写，不交给 AI。"""

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.contracts.settings import ContractSettings
from app.modules.customer import sensitive
from app.modules.customer.models import Customer
from app.modules.iam.principal import Principal
from app.modules.orders.models import Order, OrderItem, PaymentMethod

NO = "合同编号"
SIGN_DATE = "签订日期"
PARTY = {
    "我方名称": "name",
    "我方地址": "address",
    "我方电话": "phone",
    "我方税号": "tax_no",
    "我方开户行": "bank",
    "我方账号": "account",
    "我方代表": "representative",
}
CUSTOMER_NAME = "客户名称"
CUSTOMER_CONTACT = "客户联系人"
CUSTOMER_PHONE = "客户电话"
ORDER_NO = "订单编号"
ITEMS = "标的清单"
AMOUNT = "合同金额"
AMOUNT_UPPER = "合同金额大写"
PAYMENT = "付款方式"
BUILTIN: tuple[str, ...] = (
    NO,
    SIGN_DATE,
    *PARTY,
    CUSTOMER_NAME,
    CUSTOMER_CONTACT,
    CUSTOMER_PHONE,
    ORDER_NO,
    ITEMS,
    AMOUNT,
    AMOUNT_UPPER,
    PAYMENT,
)
# 跟着关联的客户、订单的内置填写项：换了客户或订单时重新填写。
CUSTOMER_FIELDS: tuple[str, ...] = (CUSTOMER_NAME, CUSTOMER_CONTACT, CUSTOMER_PHONE)
ORDER_FIELDS: tuple[str, ...] = (ORDER_NO, ITEMS, AMOUNT, AMOUNT_UPPER, PAYMENT)
# 页面上内置填写项的说明。
BUILTIN_HINTS: dict[str, str] = {
    NO: "合同的编号",
    SIGN_DATE: "签订日期（没有时是生成的日期）",
    **{name: f"合同设置里的我方信息：{name[2:]}" for name in PARTY},
    CUSTOMER_NAME: "关联的客户：公司名优先",
    CUSTOMER_CONTACT: "关联的客户的称呼",
    CUSTOMER_PHONE: "客户的手机号（有查看手机号权限时填写）",
    ORDER_NO: "关联的订单编号",
    ITEMS: "订单的商品、规格、数量、单价和金额",
    AMOUNT: "订单金额",
    AMOUNT_UPPER: "订单金额的大写",
    PAYMENT: "订单的付款方式",
}
PAYMENT_LABELS: dict[str, str] = {
    PaymentMethod.ONLINE: "在线支付，收清全款后开始处理",
    PaymentMethod.COD: "货到付款",
    PaymentMethod.DEPOSIT: "预付定金，余款按约定支付",
    PaymentMethod.CREDIT: "暂欠，按约定日期付款",
}

_DIGITS = "零壹贰叁肆伍陆柒捌玖"
_UNITS = ("", "拾", "佰", "仟")
_SECTIONS = ("", "万", "亿", "万亿")
CENT = Decimal("0.01")


def _section(number: int) -> str:
    """0–9999 的大写（不带单位"万""亿"），中间的零只写一个。"""
    out = ""
    zero = False
    for position in range(3, -1, -1):
        digit = number // 10**position % 10
        if digit == 0:
            zero = bool(out)
            continue
        if zero:
            out += "零"
            zero = False
        out += _DIGITS[digit] + _UNITS[position]
    return out


def rmb_upper(value: Decimal) -> str:
    """人民币金额大写：12000 → 人民币壹万贰仟元整；1205.5 → 人民币壹仟贰佰零伍元伍角。"""
    amount = value.quantize(CENT, rounding=ROUND_HALF_UP)
    negative = amount < 0
    cents_total = int(abs(amount) * 100)
    yuan, cents = divmod(cents_total, 100)
    jiao, fen = divmod(cents, 10)
    text = ""
    if yuan:
        sections: list[int] = []
        while yuan:
            yuan, rest = divmod(yuan, 10000)
            sections.append(rest)
        parts: list[str] = []
        need_zero = False
        for index in range(len(sections) - 1, -1, -1):
            number = sections[index]
            if number == 0:
                need_zero = bool(parts)
                continue
            if parts and (need_zero or number < 1000):
                parts.append("零")
            parts.append(_section(number) + _SECTIONS[index])
            need_zero = False
        text = "".join(parts) + "元"
    if not jiao and not fen:
        text = (text or "零元") + "整"
    else:
        if jiao:
            text += _DIGITS[jiao] + "角"
        elif text:
            text += "零"
        if fen:
            text += _DIGITS[fen] + "分"
    return ("负" if negative else "") + "人民币" + text


def money_text(value: Decimal) -> str:
    return f"{value.quantize(CENT, rounding=ROUND_HALF_UP):,.2f}"


def _number(value: Decimal | int | None) -> str:
    if value is None:
        return ""
    return f"{Decimal(value).normalize():f}" if isinstance(value, Decimal) else str(value)


@dataclass
class Facts:
    """生成合同时取到的数据：内置填写项的值，以及交给大模型的订单摘要（不含敏感信息）。"""

    values: dict[str, str] = field(default_factory=dict)
    order_summary: str = ""
    customer_name: str = ""
    amount: Decimal | None = None


def items_table(items: list[OrderItem], total: Decimal, discount: Decimal) -> str:
    lines = [
        "| 序号 | 名称 | 型号规格 | 数量 | 单价（元） | 金额（元） |",
        "|---|---|---|---|---|---|",
    ]
    for index, item in enumerate(items, start=1):
        spec = " ".join(part for part in (item.model, item.spec) if part)
        price = money_text(item.unit_price) if item.unit_price is not None else "待定"
        lines.append(
            f"| {index} | {item.name} | {spec} | {item.quantity} | {price} | "
            f"{money_text(item.amount)} |"
        )
    if discount:
        lines.append(f"| | 优惠 | | | | -{money_text(discount)} |")
    lines.append(f"| | 合计 | | | | {money_text(total)} |")
    return "\n".join(lines)


async def reveal_phone(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    customer: Customer,
    values: dict[str, str],
    *,
    ip: str | None = None,
) -> None:
    """正文里用到 {{客户电话}} 时填写客户的手机号：员工要有"查看手机号"权限，每次记一条审计。"""
    if not principal.has(Permission.CUSTOMER_VIEW_SENSITIVE) or not customer.phone_enc:
        return
    phone, _ = await sensitive.reveal(ctx.keys, customer)
    if not phone or phone == sensitive.UNREADABLE:
        return
    values[CUSTOMER_PHONE] = phone
    record_audit(
        session,
        action="customer.view_sensitive",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(customer.id),
        detail={"fields": ["phone"], "for": "contract"},
        ip=ip,
    )


async def collect(
    session: AsyncSession,
    principal: Principal,
    settings: ContractSettings,
    *,
    no: str,
    sign_date: date,
    customer: Customer | None,
    order: Order | None,
) -> Facts:
    """内置填写项的值（客户的手机号另由 reveal_phone 填写）。"""
    facts = Facts()
    values = facts.values
    values[NO] = no
    values[SIGN_DATE] = f"{sign_date.year} 年 {sign_date.month} 月 {sign_date.day} 日"
    party = settings.party.model_dump()
    for name, key in PARTY.items():
        values[name] = str(party.get(key) or "")
    if not values["我方名称"]:
        values["我方名称"] = principal.tenant_name
    if customer is not None:
        facts.customer_name = (customer.company or customer.display_name).strip()
        values[CUSTOMER_NAME] = facts.customer_name
        values[CUSTOMER_CONTACT] = customer.display_name
    if order is not None:
        items = list(
            (
                await session.scalars(
                    select(OrderItem)
                    .where(OrderItem.order_id == order.id)
                    .order_by(OrderItem.sort, OrderItem.id)
                )
            ).all()
        )
        facts.amount = order.total
        values[ORDER_NO] = order.no
        values[ITEMS] = items_table(items, order.total, order.discount)
        values[AMOUNT] = money_text(order.total)
        values[AMOUNT_UPPER] = rmb_upper(order.total)
        if order.payment_method:
            values[PAYMENT] = PAYMENT_LABELS.get(order.payment_method, order.payment_method)
        lines = [f"订单 {order.no}，合计 {money_text(order.total)} 元"]
        if order.discount:
            lines[0] += f"（已优惠 {money_text(order.discount)} 元）"
        for item in items:
            spec = " ".join(part for part in (item.model, item.spec) if part)
            price = money_text(item.unit_price) if item.unit_price is not None else "待定"
            lines.append(
                f"- {item.name}{f'（{spec}）' if spec else ''} × {_number(item.quantity)}，"
                f"单价 {price} 元，金额 {money_text(item.amount)} 元"
            )
        if order.payment_method:
            lines.append(f"付款方式：{values.get(PAYMENT, '')}")
        facts.order_summary = "\n".join(lines)
    return facts
