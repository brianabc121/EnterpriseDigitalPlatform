"""盈利报表的接口模型（设计文档 §30）。金额都是元，保留两位小数；比例是百分数（一位小数）。"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

Money = Decimal
EntryKindValue = Literal["expense", "income"]
Dimension = Literal["product", "customer", "assignee", "source", "channel", "order"]
SortKey = Literal["profit", "revenue", "margin"]
Direction = Literal["desc", "asc"]


class PeriodOut(BaseModel):
    start: date
    end: date


class CategoryAmount(BaseModel):
    category: str
    amount: Money
    count: int = Field(description="笔数")


class Statement(BaseModel):
    """一个期间的利润表（§30.3）。"""

    period: PeriodOut
    orders: int = Field(description="期间内确认的订单（不含已取消）")
    revenue: Money = Field(description="销售收入：订单合计")
    cost: Money = Field(description="销售成本：商品行数量 × 成本价，缺成本价的按 0")
    gross_profit: Money
    gross_margin: float | None = Field(description="毛利率（%）；没有收入时为空")
    other_income: Money = Field(description="其他收入：收支登记里的收入")
    expenses: Money = Field(description="费用：收支登记里的支出")
    net_profit: Money = Field(description="净利润 = 毛利 + 其他收入 − 费用")
    net_margin: float | None = Field(description="净利率（%）；没有收入时为空")
    income_by_category: list[CategoryAmount]
    expense_by_category: list[CategoryAmount]


class CostGapProduct(BaseModel):
    product_id: UUID | None = Field(description="没有对应到商品库的商品行为空")
    name: str
    code: str | None
    lines: int
    revenue: Money = Field(description="这些商品行的收入（优惠已分摊）")


class CostGap(BaseModel):
    """成本缺失：没有成本价的商品行按 0 计算，毛利偏高。"""

    lines: int
    revenue: Money
    products: list[CostGapProduct] = Field(description="按涉及的收入排前 10")


class ProfitSummary(BaseModel):
    timezone: str
    current: Statement
    previous: Statement = Field(description="上期")
    last_year: Statement = Field(description="去年同期")
    collected: Money = Field(description="回款：期间内登记的收款减退款（不参与利润）")
    uncollected: Money = Field(description="本期订单未收：期间内确认的订单现在还没收的金额")
    cost_gap: CostGap


class MonthRow(BaseModel):
    month: str = Field(description="月份，例如 2026-10")
    start: date
    end: date
    orders: int
    revenue: Money
    cost: Money
    gross_profit: Money
    gross_margin: float | None
    other_income: Money
    expenses: Money
    net_profit: Money


class ProfitTrend(BaseModel):
    timezone: str
    months: list[MonthRow]


class BreakdownRow(BaseModel):
    key: str
    label: str
    detail: str = Field(
        default="", description="补充说明：商品的代码型号规格、客户的公司、订单的客户"
    )
    orders: int
    revenue: Money
    cost: Money
    gross_profit: Money
    gross_margin: float | None
    share: float | None = Field(description="占总毛利（%）；总毛利不大于 0 时为空")
    missing_cost: int = Field(description="没有成本价的商品行")
    quantity: int | None = Field(default=None, description="销量（按商品）")
    avg_price: Money | None = Field(default=None, description="平均售价（按商品）")
    unit_cost: Money | None = Field(default=None, description="单位成本（按商品）")
    order_id: UUID | None = Field(default=None, description="订单（按订单）")
    confirmed_on: date | None = Field(default=None, description="确认日期（按订单）")
    assignee_name: str | None = Field(default=None, description="处理人（按订单）")


class ProfitBreakdown(BaseModel):
    by: Dimension
    total: int = Field(description="行数")
    gross_profit: Money = Field(description="期间的总毛利")
    items: list[BreakdownRow]


class EntryIn(BaseModel):
    kind: EntryKindValue
    category: str = Field(min_length=1, max_length=20)
    amount: Money = Field(gt=0, max_digits=10, decimal_places=2)
    occurred_on: date
    note: str = Field(default="", max_length=200)
    recurring: bool = Field(default=False, description="每月固定")

    @field_validator("category", "note")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class EntryOut(BaseModel):
    id: UUID
    kind: EntryKindValue
    kind_label: str
    category: str
    amount: Money
    occurred_on: date
    note: str
    recurring: bool
    copied_from: UUID | None
    created_by_name: str | None
    updated_by_name: str | None
    created_at: datetime
    updated_at: datetime


class CategoryTotal(BaseModel):
    kind: EntryKindValue
    category: str
    amount: Money
    count: int


class RecurringPending(BaseModel):
    """上个月标了"每月固定"、还没登记到这个月的收支。"""

    month: str = Field(description="要登记到的月份，例如 2026-10")
    count: int
    expense: Money
    income: Money
    categories: list[str]


class EntryPage(BaseModel):
    items: list[EntryOut]
    total: int
    expense_total: Money
    income_total: Money
    by_category: list[CategoryTotal]
    recurring: RecurringPending | None = Field(
        description="期间结束日所在的月（不晚于本月）还没登记的每月固定收支；没有时为空"
    )


class CopyRecurringIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="登记到哪个月，例如 2026-10")


class CopyRecurringResult(BaseModel):
    created: int
    items: list[EntryOut]


class CategoryOptions(BaseModel):
    expense: list[str] = Field(description="常用的支出类别，后面是本企业用过的")
    income: list[str]


class ExportIn(BaseModel):
    start: date
    end: date
    shift: int | None = Field(default=None, ge=1, le=24, description="上期往前移几个月")
