import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class PlanStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"  # 不再用于新订阅，已有订阅不受影响


class Plan(IdMixin, TimestampMixin, Base):
    """平台级表：套餐（设计文档 §7.3）。价格单位为分/月。

    limits 缺少的键表示不限；features 缺少的键表示包含（新增的功能开关对老套餐默认开放）。
    overage.policy：degrade 超出额度后停用（AI 转人工）；warn 继续使用、按 ai_reply_price
    （分/条）计入账单。
    """

    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    price_monthly: Mapped[int] = mapped_column(server_default="0")
    limits: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    features: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    overage: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    trial_days: Mapped[int] = mapped_column(server_default="0")
    public: Mapped[bool] = mapped_column(server_default="false")
    status: Mapped[str] = mapped_column(String(16), server_default=PlanStatus.ACTIVE.value)
    sort: Mapped[int] = mapped_column(server_default="0")


class SubscriptionStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    EXPIRED = "expired"  # 到期未续费（调度任务标记；宽限期后停用租户）
    CANCELLED = "cancelled"  # 被新订阅取代（升级、降级、试用转正式）或被运营取消


class Subscription(IdMixin, TenantMixin, TimestampMixin, Base):
    """租户的订阅：最近创建的一条决定当前套餐。取消或被取代时 period_end 截止到当天。"""

    __tablename__ = "subscriptions"

    plan_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(String(16))
    period_start: Mapped[date]
    period_end: Mapped[date]
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None]


class InvoiceStatus(StrEnum):
    ISSUED = "issued"
    PAID = "paid"
    VOID = "void"


class Invoice(IdMixin, TenantMixin, TimestampMixin, Base):
    """月度账单（一期只展示，线下收款后由运营标记已收款）。金额单位为分。"""

    __tablename__ = "invoices"

    number: Mapped[str] = mapped_column(String(64), unique=True)
    period_start: Mapped[date]
    period_end: Mapped[date]
    plan_id: Mapped[uuid.UUID | None]
    plan_name: Mapped[str] = mapped_column(String(64), server_default="")
    items: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    amount: Mapped[int]
    status: Mapped[str] = mapped_column(String(16), server_default=InvoiceStatus.ISSUED.value)
    issued_at: Mapped[datetime] = mapped_column(server_default=func.now())
    paid_at: Mapped[datetime | None]
    note: Mapped[str | None] = mapped_column(Text)
