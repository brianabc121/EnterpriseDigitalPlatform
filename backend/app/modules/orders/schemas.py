from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

Money = Decimal
OrderStatusValue = Literal[
    "draft", "pending_review", "confirmed", "fulfilling", "shipped", "completed", "cancelled"
]
OrderSourceValue = Literal["ai_chat", "copilot", "sidebar", "staff", "api"]
PaymentMethodValue = Literal["online", "cod", "deposit", "credit"]
PaymentStatusValue = Literal["unpaid", "deposit", "partial", "paid", "refunded"]
PaymentChannelValue = Literal["wechat", "alipay", "bank", "cash", "other"]
ReasonValue = Literal["customer_request", "ai_error", "price_adjust", "substitution", "other"]
View = Literal["all", "pending_review", "processing", "receivable", "modified"]


def _money(**kwargs: Any) -> Any:
    return Field(ge=0, max_digits=12, decimal_places=2, **kwargs)


class ReceiverIn(BaseModel):
    """收货信息。修改时不传的项保持不变，空字符串表示清除。"""

    name: str | None = Field(default=None, max_length=32, description="收货人")
    phone: str | None = Field(default=None, max_length=20, description="联系电话")
    address: str | None = Field(default=None, max_length=200, description="收货地址")


class LineIn(BaseModel):
    product_id: UUID | None = Field(
        default=None, description="商品库里的商品；为空时是没有匹配商品库的行（填写说明）"
    )
    quantity: int = Field(ge=1, le=100_000)
    unit_price: Money | None = _money(
        default=None,
        description="成交单价；不传时按商品的建议零售价。改价需要 order:price 权限",
    )
    raw_text: str | None = Field(default=None, max_length=200, description="客户的原话")
    name: str | None = Field(default=None, max_length=128, description="没有匹配商品库时的名称")


class OrderCreate(BaseModel):
    customer_id: UUID
    session_id: UUID | None = None
    items: list[LineIn] = Field(min_length=1, max_length=20)
    discount: Money = _money(default=Decimal("0"), description="整单优惠金额（需要 order:price）")
    receiver: ReceiverIn = Field(default_factory=ReceiverIn)
    payment_method: PaymentMethodValue | None = Field(
        default=None, description="客户选择的收款方式（审核确认时最终确定）"
    )
    expected_at: datetime | None = None
    customer_note: str = Field(default="", max_length=1000, description="客户的要求（客户可见）")
    internal_note: str = Field(default="", max_length=2000, description="内部备注")
    evidence_message_ids: list[UUID] = Field(default_factory=list, max_length=50)
    source: Literal["staff", "copilot", "sidebar"] = "staff"
    submit: bool = Field(default=True, description="直接提交审核；false 时保存为草稿")


class OrderUpdate(BaseModel):
    """修改订单：只传要修改的内容。改价、改商品、改数量时必须选择原因。"""

    version: int = Field(ge=1, description="打开订单时的版本号；订单已被别人修改时返回 409")
    items: list[LineIn] | None = Field(default=None, min_length=1, max_length=20)
    discount: Money | None = _money(default=None)
    receiver: ReceiverIn | None = None
    payment_method: PaymentMethodValue | None = None
    deposit_amount: Money | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    credit_due_date: date | None = None
    expected_at: datetime | None = None
    customer_note: str | None = Field(default=None, max_length=1000)
    internal_note: str | None = Field(default=None, max_length=2000)
    reason: ReasonValue | None = None
    note: str | None = Field(default=None, max_length=500, description="修改说明")
    notify_customer: bool = Field(
        default=False, description="已确认的订单：把修改后的内容告知客户（不需要客户再次确认）"
    )


class OrderConfirmRequest(BaseModel):
    payment_method: PaymentMethodValue
    deposit_amount: Money | None = Field(
        default=None, gt=0, max_digits=12, decimal_places=2, description="预付定金时必填"
    )
    credit_due_date: date | None = Field(default=None, description="暂欠时必填：约定付款日期")
    notify_customer: bool = True
    note: str | None = Field(default=None, max_length=500)


class OrderCancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    notify_customer: bool = True


class ShipRequest(BaseModel):
    shipping_company: str = Field(min_length=1, max_length=64)
    tracking_no: str = Field(min_length=1, max_length=64)
    notify_customer: bool = True


class NotifyFlag(BaseModel):
    notify_customer: bool = True


class PaymentIn(BaseModel):
    kind: Literal["payment", "refund"] = "payment"
    amount: Money = Field(gt=0, max_digits=12, decimal_places=2)
    channel: PaymentChannelValue
    paid_at: datetime | None = Field(default=None, description="不传时为现在")
    reference_no: str | None = Field(default=None, max_length=64, description="流水号")
    proof_url: str | None = Field(
        default=None, max_length=1024, pattern=r"^https?://\S+$", description="凭证图片链接"
    )
    note: str | None = Field(default=None, max_length=500)


class VoidRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class NoticeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class OrderAssignRequest(BaseModel):
    assignee_id: UUID | None = None
    skill_group_id: UUID | None = None


class OrderNotice(BaseModel):
    status: Literal["sent", "manual", "unreachable"]
    channel: str | None
    reason: str | None


class OrderItemOut(BaseModel):
    id: UUID
    product_id: UUID | None
    matched: bool = Field(description="已对应到商品库")
    code: str | None
    name: str
    model: str
    spec: str
    image_url: str | None
    raw_text: str | None
    quantity: int
    list_price: Money | None = Field(description="下单时的建议零售价")
    unit_price: Money | None = Field(description="成交单价；为空表示待定价")
    amount: Money
    cost_price: Money | None = Field(default=None, description="只有有查看成本价的权限时返回")


class OrderPaymentOut(BaseModel):
    id: UUID
    kind: Literal["payment", "refund"]
    amount: Money
    channel: PaymentChannelValue
    paid_at: datetime
    reference_no: str | None
    proof_url: str | None
    note: str | None
    recorded_by_name: str | None
    voided_at: datetime | None
    voided_by_name: str | None
    void_reason: str | None
    created_at: datetime


class OrderOut(BaseModel):
    id: UUID
    no: str
    status: OrderStatusValue
    source: OrderSourceValue
    customer_id: UUID | None
    customer_name: str | None
    session_id: UUID | None
    assignee_id: UUID | None
    assignee_name: str | None
    skill_group_id: UUID | None
    skill_group_name: str | None
    summary: str = Field(description="商品摘要，例如「智能门锁 X1 黑色 × 2」")
    item_count: int
    total: Money
    paid_amount: Money
    refunded_amount: Money
    outstanding: Money = Field(description="未收金额")
    payment_method: PaymentMethodValue | None
    payment_status: PaymentStatusValue
    price_pending: bool = Field(description="有待定价的商品行")
    modified: bool
    ai_error: bool
    credit_due_date: date | None
    receivable_overdue: bool = Field(description="暂欠已过约定付款日期仍未收清")
    version: int
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None


class OrderPage(BaseModel):
    items: list[OrderOut]
    total: int


class OrderCounts(BaseModel):
    pending_review: int = Field(description="待审核")
    processing: int = Field(description="处理中（已确认、处理中、已发货）")
    receivable: int = Field(description="未收清")
    receivable_overdue: int = Field(description="暂欠逾期未收清")


class OrderEventOut(BaseModel):
    id: UUID
    type: str
    actor_type: str
    actor_name: str | None
    payload: dict[str, Any]
    public: bool
    created_at: datetime


class OrderRevisionOut(BaseModel):
    version: int
    kind: Literal["created", "edit", "status", "payment"]
    actor_type: str
    actor_name: str | None
    reason: ReasonValue | None
    note: str | None
    changes: dict[str, Any]
    created_at: datetime


class OrderRevisionDetail(OrderRevisionOut):
    snapshot: dict[str, Any] = Field(description="这个版本的完整内容（收货信息为掩码）")


class OrderRevisionList(BaseModel):
    items: list[OrderRevisionOut]


class LinkedTodo(BaseModel):
    id: UUID
    no: str
    type_name: str
    title: str
    status: str
    assignee_name: str | None


class EvidenceOut(BaseModel):
    id: UUID
    sender_type: str
    text: str
    sent_at: datetime


class OrderAllowed(BaseModel):
    edit: bool
    price: bool
    submit: bool
    confirm: bool
    start: bool
    ship: bool
    complete: bool
    cancel: bool
    payment: bool
    reveal: bool
    assign: bool


class OrderDetail(OrderOut):
    items: list[OrderItemOut]
    payments: list[OrderPaymentOut]
    items_amount: Money
    discount: Money
    deposit_amount: Money | None
    credit_approved_by_name: str | None
    payment_hint: PaymentMethodValue | None = Field(description="客户在对话中提到的付款方式")
    receiver: dict[str, str] = Field(description="收货信息（掩码）")
    missing: list[str] = Field(description="提交或确认前还缺少的信息")
    expected_at: datetime | None
    shipping_company: str | None
    tracking_no: str | None
    submitted_at: datetime | None
    started_at: datetime | None
    shipped_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    customer_note: str
    internal_note: str
    external_no: str | None
    created_by_type: str
    created_by_name: str | None
    evidence: list[EvidenceOut]
    events: list[OrderEventOut]
    revisions: list[OrderRevisionOut]
    todos: list[LinkedTodo]
    tracking_url: str
    tracking_active: bool
    allowed: OrderAllowed
    cost_amount: Money | None = Field(
        default=None, description="成本合计（只有有查看成本价的权限时返回）"
    )


class ReceiverOut(BaseModel):
    receiver: dict[str, str]


class OrderSuggestionLine(BaseModel):
    product_id: UUID | None
    name: str
    spec: str
    quantity: int
    retail_price: Money | None
    raw_text: str | None
    candidates: list[dict[str, Any]] = Field(
        default_factory=list, description="没有唯一匹配时的候选商品"
    )


class OrderExtractRequest(BaseModel):
    session_id: UUID | None = None
    message_ids: list[UUID] = Field(default_factory=list, max_length=50)
    text: str | None = Field(default=None, max_length=4000)


class OrderSuggestion(BaseModel):
    items: list[OrderSuggestionLine]
    receiver: dict[str, str] = Field(description="客户提到的收货信息（明文，保存时加密）")
    payment_hint: PaymentMethodValue | None
    customer_note: str
    evidence_message_ids: list[UUID]


# ---- 客户侧：跟踪页与"我的订单" ----


class TrackingItem(BaseModel):
    name: str
    spec: str
    image_url: str | None
    quantity: int
    unit_price: Money | None
    amount: Money


class TrackingStep(BaseModel):
    key: str
    label: str
    done: bool
    at: datetime | None


class TrackingEvent(BaseModel):
    type: str
    text: str
    created_at: datetime


class OrderTracking(BaseModel):
    no: str
    status: OrderStatusValue
    status_label: str
    steps: list[TrackingStep]
    items: list[TrackingItem]
    items_amount: Money
    discount: Money
    total: Money
    payment_method: str | None = Field(description="收款方式（中文）")
    payment_status: str = Field(description="收款状态（中文）")
    paid_amount: Money
    outstanding: Money
    shipping_company: str | None
    tracking_no: str | None
    receiver: dict[str, str] = Field(description="收货信息（掩码）")
    customer_note: str
    events: list[TrackingEvent]
    created_at: datetime


class VisitorOrder(BaseModel):
    no: str
    status: OrderStatusValue
    status_label: str
    summary: str
    total: Money
    tracking_url: str | None
    created_at: datetime


class VisitorOrderList(BaseModel):
    items: list[VisitorOrder]
