"""商品库（设计文档 §25.2）：商品、Excel 导入记录、商品缺口。

成本价（cost_price）和备注（remark）只给有 product:view_cost 权限的员工；AI 用到的商品数据
由 service.public_fields 按白名单组装，永远不含这两项。
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin
from app.db.types import Vector
from app.modules.kb.models import EMBED_DIM

MONEY = Numeric(12, 2)


class ProductStatus(StrEnum):
    ON = "on"  # 上架：AI 可以推荐和下单
    OFF = "off"  # 下架


class Product(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "products"

    # 企业的商品编码，租户内唯一；再次导入时按代码更新。
    code: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(64), server_default="")
    spec: Mapped[str] = mapped_column(String(128), server_default="")
    # 多级用"/"分隔，例如"家电/空调"。
    category: Mapped[str] = mapped_column(String(128), server_default="")
    image_url: Mapped[str | None] = mapped_column(String(1024))
    cost_price: Mapped[Decimal | None] = mapped_column(MONEY)
    retail_price: Mapped[Decimal | None] = mapped_column(MONEY)
    remark: Mapped[str] = mapped_column(Text, server_default="")
    aliases: Mapped[list[str]] = mapped_column(server_default="{}")
    status: Mapped[str] = mapped_column(String(8), server_default=ProductStatus.ON.value)
    # 关键词检索的词项（名称、别名、型号、规格、分类、代码）和稠密向量（有向量模型时）。
    terms: Mapped[list[str]] = mapped_column(server_default="{}")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    created_by: Mapped[uuid.UUID | None]
    updated_by: Mapped[uuid.UUID | None]


class ImportStatus(StrEnum):
    PREVIEW = "preview"  # 已校验，等待确认
    DONE = "done"
    CANCELLED = "cancelled"


class ProductImport(IdMixin, TenantMixin, Base):
    """Excel 导入：上传后逐行校验并预览，确认后才写入商品库。rows 是每一行的解析结果。"""

    __tablename__ = "product_imports"

    file_name: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(12), server_default=ImportStatus.PREVIEW.value)
    rows: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    total: Mapped[int] = mapped_column(server_default="0")
    invalid: Mapped[int] = mapped_column(server_default="0")
    created: Mapped[int] = mapped_column(server_default="0")
    updated: Mapped[int] = mapped_column(server_default="0")
    skipped: Mapped[int] = mapped_column(server_default="0")
    created_by: Mapped[uuid.UUID | None]
    applied_by: Mapped[uuid.UUID | None]
    applied_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProductGap(IdMixin, TenantMixin, Base):
    """商品缺口：客户问到、商品库里匹配不到的商品，按规范化后的说法汇总。"""

    __tablename__ = "product_gaps"

    term: Mapped[str] = mapped_column(String(128))
    sample: Mapped[str] = mapped_column(Text, server_default="")
    count: Mapped[int] = mapped_column(server_default="1")
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    resolved_at: Mapped[datetime | None]
