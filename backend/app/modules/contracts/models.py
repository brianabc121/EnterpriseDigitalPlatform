"""项目合同管理（设计文档 §34）：分类、模板、合同。"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin

AMOUNT = Numeric(14, 2)


class TemplateStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"  # 停用：不能再用来生成合同


class ContractStatus(StrEnum):
    DRAFT = "draft"
    FINAL = "final"  # 已定稿：正文锁定
    SIGNED = "signed"
    VOID = "void"


STATUS_LABELS: dict[str, str] = {
    ContractStatus.DRAFT: "草稿",
    ContractStatus.FINAL: "已定稿",
    ContractStatus.SIGNED: "已签署",
    ContractStatus.VOID: "已作废",
}


class ContractCategory(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "contract_categories"

    parent_id: Mapped[uuid.UUID | None]
    name: Mapped[str] = mapped_column(String(64))
    sort: Mapped[int] = mapped_column(server_default="0")


class ContractTemplate(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "contract_templates"

    category_id: Mapped[uuid.UUID | None]
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, server_default="")
    # 填写项：[{"name": "交货日期", "hint": "", "default": ""}]，按在正文里出现的先后。
    fields: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    status: Mapped[str] = mapped_column(String(12), server_default=TemplateStatus.ACTIVE.value)
    file_key: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(200))
    used_count: Mapped[int] = mapped_column(server_default="0")
    created_by: Mapped[uuid.UUID | None]
    updated_by: Mapped[uuid.UUID | None]


class Contract(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "contracts"

    no: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    category_id: Mapped[uuid.UUID | None]
    template_id: Mapped[uuid.UUID | None]
    customer_id: Mapped[uuid.UUID | None]
    order_id: Mapped[uuid.UUID | None]
    owner_id: Mapped[uuid.UUID | None]
    status: Mapped[str] = mapped_column(String(12), server_default=ContractStatus.DRAFT.value)
    amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    sign_date: Mapped[date | None]
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    requirement: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, server_default="")
    # 填写项的值（名称 → 值），正文里的 {{名称}} 按它显示和导出。
    field_values: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # AI 起草的依据：知识、模型、Token、费用、提醒（§34.3）。
    ai: Mapped[dict[str, Any] | None]
    void_reason: Mapped[str | None] = mapped_column(Text)
    scan_key: Mapped[str | None] = mapped_column(Text)
    scan_name: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID | None]
    finalized_by: Mapped[uuid.UUID | None]
    finalized_at: Mapped[datetime | None]
    signed_by: Mapped[uuid.UUID | None]
    signed_at: Mapped[datetime | None]
    voided_by: Mapped[uuid.UUID | None]
    voided_at: Mapped[datetime | None]
