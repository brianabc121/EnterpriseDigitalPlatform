import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.modules.integration.models import Scope, WebhookEventType
from app.modules.orders.schemas import (
    Money,
    OrderNotice,
    OrderStatusValue,
    PaymentChannelValue,
    PaymentMethodValue,
)
from app.modules.products.schemas import Qty, QtyIn
from app.modules.todos.models import Priority

ScopeValue = Literal[
    "products:write",
    "orders:read",
    "orders:write",
    "todos:write",
    "opportunities:read",
    "opportunities:write",
]
EventValue = Literal[
    "order.created",
    "order.updated",
    "order.confirmed",
    "order.status_changed",
    "order.cancelled",
    "order.payment",
    "todo.done",
    "opportunity.created",
    "opportunity.stage_changed",
    "opportunity.won",
    "opportunity.lost",
    "opportunity.assigned",
]
assert set(ScopeValue.__args__) == {s.value for s in Scope}  # type: ignore[attr-defined]
assert set(EventValue.__args__) == {  # type: ignore[attr-defined]
    e.value for e in WebhookEventType if e != WebhookEventType.PING
}


# ---- 接口密钥 ----


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="用途，例如：ERP 订单同步")
    scopes: list[ScopeValue] = Field(min_length=1, description="权限范围")

    @field_validator("scopes")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    display: str = Field(description="edp_<前缀>_••••（完整密钥只在创建时显示一次）")
    scopes: list[str]
    created_by_name: str | None
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="完整的密钥：只显示这一次，请妥善保存")


class ApiKeyList(BaseModel):
    items: list[ApiKeyOut]


# ---- 推送地址 ----


class WebhookEndpointWrite(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    url: str = Field(
        min_length=8, max_length=1024, description="接收推送的地址（生产环境只能是公网 https）"
    )
    events: list[EventValue] = Field(min_length=1, description="订阅的事件")
    enabled: bool = True

    @field_validator("events")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class WebhookEndpointOut(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    events: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime
    pending: int = Field(description="等待推送或重试中的数量")
    dead: int = Field(description="多次失败后放弃（死信）的数量")
    last_success_at: datetime | None
    last_failure_at: datetime | None


class WebhookEndpointCreated(WebhookEndpointOut):
    secret: str = Field(description="签名密钥：只显示这一次，用来校验推送的签名")


class WebhookEndpointList(BaseModel):
    items: list[WebhookEndpointOut]


class WebhookDeliveryOut(BaseModel):
    id: uuid.UUID
    endpoint_id: uuid.UUID
    endpoint_name: str | None = None
    event: str
    status: Literal["pending", "succeeded", "dead"]
    attempts: int
    next_attempt_at: datetime | None
    last_status: int | None
    last_error: str | None
    created_at: datetime
    delivered_at: datetime | None
    body: str | None = Field(default=None, description="推送的内容（只在查看单条时返回）")


class WebhookDeliveryPage(BaseModel):
    items: list[WebhookDeliveryOut]
    total: int


class WebhookTestResult(BaseModel):
    ok: bool
    status: int | None
    error: str | None
    duration_ms: int


# ---- 开放接口：商品 ----


class OpenProductIn(BaseModel):
    """按代码同步商品：代码不存在时新建（需要名称）；已存在时只修改传了的字段。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, max_length=64)
    spec: str | None = Field(default=None, max_length=128)
    category: str | None = Field(default=None, max_length=128)
    image_url: str | None = Field(default=None, max_length=1024, pattern=r"^https?://\S+$")
    retail_price: Money | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    cost_price: Money | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    remark: str | None = Field(default=None, max_length=2000)
    aliases: list[str] | None = Field(default=None, max_length=20)
    status: Literal["on", "off"] | None = None
    kind: Literal["goods", "material"] | None = Field(
        default=None,
        description="类别：goods 成品（默认），material 材料；只在新建时有效，"
        "已有商品的类别不能修改",
    )
    unit: str | None = Field(default=None, max_length=16, description="单位，例如 件、米")
    ready_made: bool | None = Field(
        default=None, description="现货：直接从成品库存发货，不需要加工（材料没有这一项）"
    )
    stock: QtyIn | None = Field(
        default=None,
        description="现有库存（盘点数；成品是整数，材料最多三位小数）；传 null 表示不再管理这个"
        "成品的库存，不传时保持原值",
    )
    stock_alert: QtyIn | None = Field(default=None, description="库存预警值；不传时保持原值")


class OpenProductOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    model: str
    spec: str
    category: str
    image_url: str | None
    retail_price: Money | None
    cost_price: Money | None
    aliases: list[str]
    remark: str
    status: str
    kind: Literal["goods", "material"]
    unit: str
    ready_made: bool
    stock: Qty | None = Field(description="现有库存；为空表示不管理库存")
    stock_available: Qty | None = Field(
        description="可用库存 = 现有 − 占用（成品：要从库存发出的订单；材料：待确认的领料单）"
    )
    stock_alert: Qty | None
    created: bool = Field(description="这次请求新建了商品")
    updated_at: datetime


# ---- 开放接口：订单 ----


class OpenCustomerRef(BaseModel):
    id: uuid.UUID | None
    name: str | None


class OpenOrderItem(BaseModel):
    code: str | None
    name: str
    model: str
    spec: str
    raw_text: str | None = Field(description="没有对应到商品库的商品行：客户的原话")
    quantity: int
    list_price: Money | None
    unit_price: Money | None = Field(description="为空表示待定价")
    amount: Money
    work_status: Literal["pending", "done", "out_of_stock"] = Field(
        default="pending", description="加工进度：待加工、已完成、缺货"
    )
    shortage_qty: int | None = Field(default=None, description="缺货时缺多少；为空表示整行都缺")
    restock_date: date | None = Field(default=None, description="缺货时预计到货的日期")


class OpenPayment(BaseModel):
    kind: Literal["payment", "refund"]
    amount: Money
    channel: str
    paid_at: datetime
    reference_no: str | None
    recorded_by_type: str
    voided: bool


class OpenReceiver(BaseModel):
    name: str | None = None
    phone: str | None = None
    address: str | None = None


class OpenOrder(BaseModel):
    """订单（开放接口与事件推送共用）。收货信息为明文，只给有 orders:read 权限的密钥。"""

    id: uuid.UUID
    no: str
    external_no: str | None
    status: OrderStatusValue
    source: str
    customer: OpenCustomerRef
    items: list[OpenOrderItem]
    items_amount: Money
    discount: Money
    total: Money
    payment_method: PaymentMethodValue | None
    deposit_amount: Money | None
    credit_due_date: date | None
    payment_status: str
    paid_amount: Money
    refunded_amount: Money
    outstanding: Money
    payments: list[OpenPayment]
    receiver: OpenReceiver
    expected_at: datetime | None
    customer_note: str
    shipping_company: str | None
    tracking_no: str | None
    tracking_url: str
    cancel_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
    confirmed_at: datetime | None
    started_at: datetime | None
    processed_at: datetime | None = Field(default=None, description="工人加工完成的时间")
    shortage: bool = Field(default=False, description="有缺货的商品")
    shipped_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None


class OpenOrderPage(BaseModel):
    items: list[OpenOrder]
    next_cursor: str | None = Field(description="还有更多时，下一页请求带上 cursor")


class OpenLineIn(BaseModel):
    code: str | None = Field(default=None, max_length=64, description="商品代码（对应商品库）")
    name: str | None = Field(
        default=None, max_length=200, description="商品库里没有的商品：商品说明（由员工对应）"
    )
    quantity: int = Field(ge=1, le=100_000)
    unit_price: Money | None = Field(
        default=None, ge=0, max_digits=12, decimal_places=2, description="不传时按建议零售价"
    )


class OpenCustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    phone: str | None = Field(default=None, max_length=32, description="按手机号找到已有的客户")


class OpenOrderCreate(BaseModel):
    customer_id: uuid.UUID | None = None
    customer: OpenCustomerIn | None = Field(
        default=None, description="没有 customer_id 时：按手机号找到客户，找不到时新建"
    )
    items: list[OpenLineIn] = Field(min_length=1, max_length=20)
    discount: Money = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    receiver: OpenReceiver = Field(default_factory=OpenReceiver)
    payment_method: PaymentMethodValue | None = None
    deposit_amount: Money | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    credit_due_date: date | None = None
    expected_at: datetime | None = None
    customer_note: str = Field(default="", max_length=1000)
    internal_note: str = Field(default="", max_length=2000)
    external_no: str | None = Field(
        default=None, max_length=64, description="企业系统的订单号：重复创建时返回已有的订单"
    )
    status: Literal["pending_review", "confirmed"] = Field(
        default="pending_review",
        description="pending_review：进入平台审核；confirmed：企业系统已确认（需要收款方式）",
    )
    notify_customer: bool = Field(default=False, description="确认时把确认信息发给客户")


class OpenPaymentIn(BaseModel):
    kind: Literal["payment", "refund"] = "payment"
    amount: Money = Field(gt=0, max_digits=12, decimal_places=2)
    channel: PaymentChannelValue = "bank"
    paid_at: datetime | None = None
    reference_no: str | None = Field(
        default=None, max_length=64, description="流水号：同一个流水号只登记一次"
    )
    note: str | None = Field(default=None, max_length=500)


class OpenStatusUpdate(BaseModel):
    """企业系统回传：确认之后的状态以企业系统为准（2026-09-30 确认）。可以跳过中间状态，
    不能回退；已完成、已取消的订单只能登记收款和退款。"""

    status: Literal["confirmed", "fulfilling", "shipped", "completed", "cancelled"] | None = None
    payment_method: PaymentMethodValue | None = None
    deposit_amount: Money | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    credit_due_date: date | None = None
    shipping_company: str | None = Field(default=None, max_length=64)
    tracking_no: str | None = Field(default=None, max_length=64)
    cancel_reason: str | None = Field(default=None, max_length=500)
    payments: list[OpenPaymentIn] = Field(default_factory=list, max_length=20)
    external_no: str | None = Field(default=None, max_length=64)
    notify_customer: bool = Field(default=True, description="按订单设置里的模板通知客户")


class OpenOrderResult(BaseModel):
    order: OpenOrder
    notice: OrderNotice | None = None


# ---- 开放接口：待办 ----


class OpenTodoCreate(BaseModel):
    type: str = Field(min_length=1, max_length=32, description="待办类型的代码，例如 callback")
    title: str = Field(min_length=1, max_length=128)
    detail: str = Field(default="", max_length=2000)
    fields: dict[str, str] = Field(default_factory=dict)
    customer_id: uuid.UUID | None = None
    order_no: str | None = Field(
        default=None, max_length=64, description="关联的订单：平台订单号或企业系统的订单号"
    )
    priority: Priority | None = None
    due_at: datetime | None = None
    external_ref: str | None = Field(
        default=None, max_length=64, description="企业系统的单号：重复创建时返回已有的待办"
    )


class OpenTodo(BaseModel):
    id: uuid.UUID
    no: str
    type: str
    title: str
    status: str
    assignee_name: str | None
    customer_id: uuid.UUID | None
    order_no: str | None
    due_at: datetime | None
    result: str | None
    external_ref: str | None
    created_at: datetime
    closed_at: datetime | None


# ---- 商机（设计文档 §40.13）----

OpenOpportunityStatus = Literal["suggested", "active", "won", "lost"]


class OpenOpportunityCreate(BaseModel):
    """企业系统创建线索（官网表单、投放线索）：客户已有的用 customer_id，否则按手机号找到或者新建。
    同一客户已经有待确认、跟进中的商机时返回已有的（200）。"""

    customer_id: uuid.UUID | None = None
    customer: OpenCustomerIn | None = Field(
        default=None, description="没有 customer_id 时：按手机号找到客户，找不到时新建"
    )
    name: str | None = Field(default=None, max_length=128, description="不填时按想要什么或客户称呼")
    interest: str | None = Field(default=None, max_length=1000, description="客户想要什么")
    concerns: str | None = Field(default=None, max_length=1000)
    level: Literal["high", "medium", "low"] = "medium"
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    expected_close_at: date | None = None
    owner_username: str | None = Field(
        default=None, max_length=64, description="负责人的用户名；不填时按商机设置分配"
    )


class OpenOpportunityUpdate(BaseModel):
    """修改商机：字段、换到进行中的阶段（阶段代码）、赢单（可带平台订单号）或输单（原因分类代码）。"""

    name: str | None = Field(default=None, max_length=128)
    interest: str | None = Field(default=None, max_length=1000)
    concerns: str | None = Field(default=None, max_length=1000)
    level: Literal["high", "medium", "low"] | None = None
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    expected_close_at: date | None = None
    next_follow_at: date | None = None
    stage: str | None = Field(default=None, max_length=32, description="进行中的阶段的代码")
    status: Literal["won", "lost"] | None = None
    order_no: str | None = Field(default=None, max_length=64, description="赢单关联的平台订单号")
    lost_reason_code: str | None = Field(default=None, max_length=16)
    lost_reason: str | None = Field(default=None, max_length=500)


class OpenOpportunity(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str
    name: str
    status: str
    stage: str = Field(description="阶段代码")
    stage_name: str
    level: str
    source: str
    interest: str | None
    concerns: str | None
    amount: Decimal | None
    probability: int
    expected_close_at: date | None
    next_follow_at: date | None
    owner_username: str | None
    owner_name: str | None
    order_no: str | None
    contract_no: str | None
    lost_reason_code: str | None
    lost_reason: str | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


class OpenOpportunityPage(BaseModel):
    items: list[OpenOpportunity]
    next_cursor: str | None
