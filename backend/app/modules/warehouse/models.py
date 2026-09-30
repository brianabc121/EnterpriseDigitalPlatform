"""仓库单据（设计文档 §25.13）：领料单（扣减材料库存）和入库单（增加成品库存）。

工人开单后等仓管确认（仓管开的单、或者设置为不需要确认时直接生效）；确认时才修改库存并写库存
记录。仓管可以退回（写明原因，开单人修改后重新提交）；还没确认的单据可以作废。已确认的单据不能
修改，数量有出入时通过盘点调整。
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import ForeignKeyConstraint, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin
from app.modules.products.models import QTY


class DocumentKind(StrEnum):
    REQUISITION = "requisition"  # 领料单：材料出库
    RECEIPT = "receipt"  # 入库单：成品入库


KIND_LABELS: dict[str, str] = {DocumentKind.REQUISITION: "领料单", DocumentKind.RECEIPT: "入库单"}
PREFIXES: dict[str, str] = {DocumentKind.REQUISITION: "LL", DocumentKind.RECEIPT: "RK"}


class DocumentStatus(StrEnum):
    PENDING = "pending"  # 待仓管确认
    CONFIRMED = "confirmed"  # 已确认（已修改库存）
    REJECTED = "rejected"  # 仓管退回，开单人修改后重新提交
    VOIDED = "voided"  # 已作废


STATUS_LABELS: dict[str, str] = {
    DocumentStatus.PENDING: "待确认",
    DocumentStatus.CONFIRMED: "已确认",
    DocumentStatus.REJECTED: "已退回",
    DocumentStatus.VOIDED: "已作废",
}
# 还没有生效、开单人可以修改或作废的状态。
OPEN = (DocumentStatus.PENDING, DocumentStatus.REJECTED)


class StockDocument(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "stock_documents"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "order_id"], ["orders.tenant_id", "orders.id"]),
    )

    kind: Mapped[str] = mapped_column(String(12))
    no: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(12), server_default=DocumentStatus.PENDING.value)
    # 关联的销售订单（加工时开的单）；为空表示仓库直接开的单（例如备货生产）。
    order_id: Mapped[uuid.UUID | None]
    note: Mapped[str] = mapped_column(Text, server_default="")
    created_by: Mapped[uuid.UUID | None]
    submitted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    confirmed_by: Mapped[uuid.UUID | None]
    confirmed_at: Mapped[datetime | None]
    rejected_by: Mapped[uuid.UUID | None]
    rejected_at: Mapped[datetime | None]
    reject_reason: Mapped[str | None] = mapped_column(Text)
    voided_by: Mapped[uuid.UUID | None]
    voided_at: Mapped[datetime | None]
    void_reason: Mapped[str | None] = mapped_column(Text)


class StockDocumentLine(IdMixin, TenantMixin, Base):
    """单据的一行：商品信息是开单时的快照；planned 是按配方（领料）或订单（入库）算出的建议数量，
    stock_before / stock_after 是确认时库存的变化。"""

    __tablename__ = "stock_document_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["stock_documents.tenant_id", "stock_documents.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
    )

    document_id: Mapped[uuid.UUID]
    product_id: Mapped[uuid.UUID]
    code: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    spec: Mapped[str] = mapped_column(String(128), server_default="")
    unit: Mapped[str] = mapped_column(String(16), server_default="")
    planned: Mapped[Decimal | None] = mapped_column(QTY)
    quantity: Mapped[Decimal] = mapped_column(QTY)
    stock_before: Mapped[Decimal | None] = mapped_column(QTY)
    stock_after: Mapped[Decimal | None] = mapped_column(QTY)
    sort: Mapped[int] = mapped_column(server_default="0")
