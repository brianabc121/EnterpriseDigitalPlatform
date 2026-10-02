"""盈利报表（设计文档 §30.6）：订单以外的费用和其他收入（收支登记）。

收入、成本和毛利直接从订单算，不建表；这里只有需要手工登记的收支。
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin

MONEY = Numeric(12, 2)


class EntryKind(StrEnum):
    EXPENSE = "expense"  # 支出（费用）
    INCOME = "income"  # 其他收入


KIND_LABELS: dict[str, str] = {EntryKind.EXPENSE: "支出", EntryKind.INCOME: "收入"}

# 常用类别（§30.4），登记时可以选，也可以自己写。
DEFAULT_CATEGORIES: dict[str, tuple[str, ...]] = {
    EntryKind.EXPENSE: (
        "工资社保",
        "房租物业",
        "水电网络",
        "物流快递",
        "广告推广",
        "平台和软件",
        "办公用品",
        "设备维修",
        "差旅交通",
        "税费",
        "其他",
    ),
    EntryKind.INCOME: ("废料收入", "补贴", "利息", "其他"),
}


class ProfitEntry(IdMixin, TimestampMixin, TenantMixin, Base):
    """一笔费用或其他收入，按发生日期计入盈利报表。"""

    __tablename__ = "profit_entries"

    kind: Mapped[str] = mapped_column(String(8))
    category: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    occurred_on: Mapped[date]
    note: Mapped[str] = mapped_column(Text, server_default="")
    # 每月固定：可以一键登记到下个月（§30.4）；copied_from 是从哪一笔复制来的，同一笔只复制一次。
    recurring: Mapped[bool] = mapped_column(server_default="false")
    copied_from: Mapped[uuid.UUID | None]
    created_by: Mapped[uuid.UUID | None]
    updated_by: Mapped[uuid.UUID | None]
