"""商品库（设计文档 §25.2）：商品、Excel 导入记录、商品缺口；库存与库存记录（§25.12）；
成品和材料、配方（§25.13）。

成本价（cost_price）和备注（remark）只给有 product:view_cost 权限的员工；AI 用到的商品数据
由 service.public_fields 按白名单组装，永远不含这两项。材料只用于生产：不给 AI，也不能下单。
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin
from app.db.types import Vector
from app.modules.kb.models import EMBED_DIM

MONEY = Numeric(12, 2)
# 库存数量：最多三位小数（材料可以是 2.5 米；成品只用整数）。
QTY = Numeric(14, 3)


class ProductKind(StrEnum):
    GOODS = "goods"  # 成品：可以销售
    MATERIAL = "material"  # 材料：只用于生产（领料），不给 AI、不能下单


KIND_LABELS: dict[str, str] = {ProductKind.GOODS: "成品", ProductKind.MATERIAL: "材料"}


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
    # 类别（成品或材料，创建后不能修改）、单位；现货：直接从成品库存发货，不需要加工（§25.13）。
    kind: Mapped[str] = mapped_column(String(12), server_default=ProductKind.GOODS.value)
    unit: Mapped[str] = mapped_column(String(16), server_default="")
    ready_made: Mapped[bool] = mapped_column(server_default="false")
    # 现有库存（为空表示不管理库存，可以是负数）和预警值（§25.12）。只通过 products.stock 修改，
    # 每次变化都写库存记录。材料总是管理库存。
    stock: Mapped[Decimal | None] = mapped_column(QTY)
    stock_alert: Mapped[Decimal | None] = mapped_column(QTY)
    # 关键词检索的词项（名称、别名、型号、规格、分类、代码）和稠密向量（有向量模型时）。
    terms: Mapped[list[str]] = mapped_column(server_default="{}")
    # 开单时联想的检索键（§25.16）：各字段统一写法后的内容，名称和俗称的全拼与拼音首字母。
    search_key: Mapped[str] = mapped_column(Text, server_default="")
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
    # "库存"列的算法：set 盘点（表格里的数就是现有库存），add 入库（加到现有库存上）。
    stock_mode: Mapped[str] = mapped_column(String(8), server_default="set")


class ProductGap(IdMixin, TenantMixin, Base):
    """商品缺口：客户问到、商品库里匹配不到的商品，按规范化后的说法汇总。"""

    __tablename__ = "product_gaps"

    term: Mapped[str] = mapped_column(String(128))
    sample: Mapped[str] = mapped_column(Text, server_default="")
    count: Mapped[int] = mapped_column(server_default="1")
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    resolved_at: Mapped[datetime | None]


class StockKind(StrEnum):
    IMPORT_SET = "import_set"  # 导入表格：盘点
    IMPORT_ADD = "import_add"  # 导入表格：入库
    ADJUST_SET = "adjust_set"  # 手动盘点
    ADJUST_ADD = "adjust_add"  # 手动入库
    ADJUST_REMOVE = "adjust_remove"  # 手动出库（损耗、自用等）
    UNTRACK = "untrack"  # 不再管理这个商品的库存
    ORDER_OUT = "order_out"  # 订单发货（或没有发货环节的完成）出库
    ORDER_RETURN = "order_return"  # 已出库的订单被取消，退回库存
    API_SET = "api_set"  # 企业系统同步
    REQUISITION = "requisition"  # 领料单确认：扣减材料库存
    RECEIPT = "receipt"  # 入库单确认：增加成品库存


class StockMovement(IdMixin, TenantMixin, Base):
    """库存记录：每一次库存变化和变化前后的数量（只追加）。stock_before 为空表示原来不管理库存，
    stock_after 为空表示之后不再管理。"""

    __tablename__ = "stock_movements"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "product_id"], ["products.tenant_id", "products.id"], ondelete="CASCADE"
        ),
    )

    product_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(String(16))
    delta: Mapped[Decimal] = mapped_column(QTY)
    stock_before: Mapped[Decimal | None] = mapped_column(QTY)
    stock_after: Mapped[Decimal | None] = mapped_column(QTY)
    order_id: Mapped[uuid.UUID | None]
    import_id: Mapped[uuid.UUID | None]
    # 领料单、入库单（§25.13）。
    document_id: Mapped[uuid.UUID | None]
    note: Mapped[str] = mapped_column(Text, server_default="")
    actor_type: Mapped[str] = mapped_column(String(8))
    actor_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProductMaterial(IdMixin, TenantMixin, Base):
    """配方（§25.13）：每一件成品用多少材料。开领料单时按订单数量乘配方用量预填。"""

    __tablename__ = "product_materials"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "product_id"], ["products.tenant_id", "products.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["tenant_id", "material_id"], ["products.tenant_id", "products.id"]),
    )

    product_id: Mapped[uuid.UUID]
    material_id: Mapped[uuid.UUID]
    quantity: Mapped[Decimal] = mapped_column(QTY)
    sort: Mapped[int] = mapped_column(server_default="0")
