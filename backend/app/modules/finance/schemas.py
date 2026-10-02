"""应收账款的接口模型（设计文档 §28）。"""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.modules.orders.schemas import (
    Money,
    OrderStatusValue,
    PaymentChannelValue,
    PaymentMethodValue,
    PaymentStatusValue,
)

View = Literal["open", "overdue", "due_today", "due_soon", "not_due", "promised", "promise_overdue"]
Bucket = Literal["current", "d1_30", "d31_60", "d61_90", "d90_plus"]
Sort = Literal["due", "outstanding", "age"]
CustomerSort = Literal["outstanding", "overdue"]

BUCKETS: tuple[Bucket, ...] = ("current", "d1_30", "d31_60", "d61_90", "d90_plus")
BUCKET_LABELS: dict[str, str] = {
    "current": "未到期",
    "d1_30": "逾期 1–30 天",
    "d31_60": "逾期 31–60 天",
    "d61_90": "逾期 61–90 天",
    "d90_plus": "逾期 90 天以上",
}


class CollectionTodoOut(BaseModel):
    id: UUID
    no: str
    status: str
    assignee_id: UUID | None
    assignee_name: str | None


class ReceivableOut(BaseModel):
    id: UUID
    no: str
    status: OrderStatusValue
    customer_id: UUID | None
    customer_name: str | None
    customer_company: str | None
    assignee_id: UUID | None
    assignee_name: str | None
    summary: str = Field(description="商品摘要")
    payment_method: PaymentMethodValue | None
    payment_status: PaymentStatusValue
    total: Money
    paid_amount: Money
    refunded_amount: Money
    outstanding: Money = Field(description="未收金额")
    confirmed_at: datetime | None
    due_date: date | None = Field(
        description="到期日（设计文档 §28.3）；为空表示还没到期（例如尾款等加工完成）"
    )
    overdue_days: int = Field(description="逾期天数；没有逾期为 0")
    age_days: int = Field(description="账龄：确认到今天的天数")
    bucket: Bucket = Field(description="账龄分段（按逾期天数）")
    promise_date: date | None = Field(description="客户承诺的付款日")
    promise_overdue: bool = Field(description="承诺付款日已过仍未收清")
    followed_up_at: datetime | None = Field(description="最近一次跟进的时间")
    follow_up_note: str | None = Field(description="最近一次跟进的备注")
    collection_todo: CollectionTodoOut | None = Field(description="未完成的催收待办")


class ReceivablePage(BaseModel):
    items: list[ReceivableOut]
    total: int


class AmountCount(BaseModel):
    amount: Money
    count: int


class BucketOut(BaseModel):
    bucket: Bucket
    label: str
    amount: Money
    count: int


class ReceivableSummary(BaseModel):
    today: date = Field(description="租户时区的今天")
    open: AmountCount = Field(description="全部未收清")
    overdue: AmountCount
    due_today: AmountCount
    due_soon: AmountCount = Field(description="7 天内到期（不含今天）")
    received_this_month: Money = Field(description="本月登记的收款减退款")
    buckets: list[BucketOut]


class CustomerReceivableOut(BaseModel):
    customer_id: UUID
    customer_name: str
    company: str | None
    outstanding: Money
    overdue: Money
    orders: int = Field(description="未收清的订单数")
    earliest_due: date | None
    max_overdue_days: int
    last_paid_at: datetime | None
    followed_up_at: datetime | None
    follow_up_note: str | None


class CustomerReceivablePage(BaseModel):
    items: list[CustomerReceivableOut]
    total: int


class RecentPaymentOut(BaseModel):
    id: UUID
    order_id: UUID
    order_no: str
    customer_id: UUID | None
    customer_name: str | None
    kind: Literal["payment", "refund"]
    amount: Money
    channel: PaymentChannelValue
    paid_at: datetime
    recorded_by_name: str | None
    created_at: datetime


class RecentPaymentList(BaseModel):
    items: list[RecentPaymentOut]


class FollowupIn(BaseModel):
    promise_date: date | None = Field(
        default=None, description="客户承诺的付款日；传 null 清除，不传表示不修改"
    )
    note: str = Field(default="", max_length=500)


class CollectIn(BaseModel):
    assignee_id: UUID | None = Field(
        default=None,
        description="催收待办交给谁；不填时交给订单处理人（没有处理人时按催收类型的分派规则）",
    )


class StatementLine(BaseModel):
    date: date
    kind: Literal["order", "payment", "refund"]
    order_id: UUID
    order_no: str
    description: str = Field(description="订单：商品摘要；收款、退款：渠道和流水号")
    amount: Money
    balance: Money = Field(description="这一行之后的未收余额")


class CustomerStatement(BaseModel):
    customer_id: UUID
    customer_name: str
    company: str | None
    period_from: date
    period_to: date
    opening: Money = Field(description="期初未收")
    orders_amount: Money = Field(description="期间内确认的订单合计")
    received: Money
    refunded: Money
    closing: Money = Field(description="期末未收")
    lines: list[StatementLine]
    open_orders: list[ReceivableOut] = Field(description="目前未收清的订单")
    generated_at: datetime
    generated_by: str


class ReceivableFilters(BaseModel):
    view: View = "open"
    bucket: Bucket | None = None
    payment_method: PaymentMethodValue | None = None
    assignee_id: UUID | None = None
    customer_id: UUID | None = None
    q: str | None = Field(default=None, max_length=100)


class StaffOption(BaseModel):
    id: UUID
    name: str


class StaffOptionList(BaseModel):
    items: list[StaffOption] = Field(description="启用状态的员工（筛选处理人、指定催收人）")
