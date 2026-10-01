from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.modules.products.schemas import ProductKindValue, Qty, QtyIn, SuggestMatchValue
from app.modules.products.suggest import SuggestField

DocumentKindValue = Literal["requisition", "receipt"]
DocumentStatusValue = Literal["pending", "confirmed", "rejected", "voided"]


class DocumentLineIn(BaseModel):
    product_id: UUID = Field(description="领料单是材料，入库单是成品")
    quantity: QtyIn = Field(description="数量，大于 0；成品只能是整数，材料最多三位小数")
    planned: QtyIn | None = Field(
        default=None, description="按配方或订单算出的建议数量（开单时预填的，只做记录）"
    )


class DocumentIn(BaseModel):
    kind: DocumentKindValue
    order_id: UUID | None = Field(
        default=None,
        description="关联的销售订单（领料单）；入库单关联订单要在加工页“完成加工”时开",
    )
    note: str = Field(default="", max_length=200)
    lines: list[DocumentLineIn] = Field(min_length=1, max_length=100)


class DocumentUpdate(BaseModel):
    """修改后重新提交（待确认、已退回的单据）。"""

    note: str = Field(default="", max_length=200)
    lines: list[DocumentLineIn] = Field(min_length=1, max_length=100)


class ConfirmLineIn(BaseModel):
    id: UUID = Field(description="单据行")
    quantity: QtyIn = Field(description="实际数量；为 0 表示这一行不领（不入库）")


class DocumentConfirm(BaseModel):
    lines: list[ConfirmLineIn] | None = Field(
        default=None, description="仓管按实际数量修改（不传表示按单据上的数量）"
    )


class ReasonIn(BaseModel):
    reason: str = Field(default="", max_length=200)


class DocumentLineOut(BaseModel):
    id: UUID
    product_id: UUID
    code: str | None
    name: str
    spec: str
    unit: str
    planned: Qty | None = Field(description="建议数量（配方用量或订单数量）")
    quantity: Qty
    stock: Qty | None = Field(description="现在的库存（为空表示不管理库存）")
    stock_before: Qty | None = Field(description="确认时的库存")
    stock_after: Qty | None = Field(description="确认后的库存")


class DocumentOut(BaseModel):
    id: UUID
    kind: DocumentKindValue
    kind_label: str
    no: str
    status: DocumentStatusValue
    status_label: str
    order_id: UUID | None
    order_no: str | None
    note: str
    lines: list[DocumentLineOut]
    created_by_name: str | None
    created_at: datetime
    submitted_at: datetime
    confirmed_by_name: str | None
    confirmed_at: datetime | None
    rejected_by_name: str | None
    rejected_at: datetime | None
    reject_reason: str | None
    voided_by_name: str | None
    voided_at: datetime | None
    void_reason: str | None
    short: list[str] = Field(
        description="待确认的领料单里库存不够的材料（确认后库存会是负数；只提示）"
    )
    can_edit: bool = Field(description="可以修改后重新提交（开单人，待确认或已退回）")
    can_confirm: bool = Field(description="可以确认或退回（仓管，待确认）")
    can_void: bool = Field(description="可以作废（开单人或仓管，待确认或已退回）")


class DocumentPage(BaseModel):
    items: list[DocumentOut]
    total: int


class DocumentBrief(BaseModel):
    id: UUID
    kind: DocumentKindValue
    no: str
    status: DocumentStatusValue
    reject_reason: str | None


DraftBasisValue = Literal["recipe", "history"]
DraftItemBasisValue = Literal["recipe", "history", "none", "unmatched"]


class DraftSource(BaseModel):
    """领料单的建议数量是怎么算的：一个商品的用量（§25.17）。"""

    item: str = Field(description="商品（名称和规格）")
    quantity: int = Field(description="要加工的数量")
    unit: str = Field(description="商品的单位")
    per_unit: Qty = Field(description="每件用量（按以往领料估算的四舍五入到三位小数）")
    amount: Qty = Field(description="这个商品要用的数量")
    basis: DraftBasisValue = Field(description="recipe 配方；history 按以往领料估算")
    orders: int | None = Field(description="按以往领料估算时依据的订单数")


class DraftLine(BaseModel):
    product_id: UUID
    code: str | None
    name: str
    spec: str
    unit: str
    kind: ProductKindValue
    planned: Qty | None = Field(description="建议数量")
    quantity: Qty
    stock: Qty | None = Field(description="现有库存")
    available: Qty | None = Field(description="可用库存（材料：现有减去待确认的领料单）")
    sources: list[DraftSource] = Field(
        description="领料单：建议数量是怎么算的（各商品的用量合计，减去已领的就是建议数量）"
    )
    taken: Qty = Field(description="领料单：这个订单已经领过的（待确认和已确认的领料单）")
    estimated: bool = Field(description="领料单：有按以往领料估算的部分")


class DraftItem(BaseModel):
    """领料单：这次加工的商品，以及用量从哪里来（§25.17）。"""

    name: str
    spec: str
    quantity: int
    unit: str
    basis: DraftItemBasisValue = Field(
        description="recipe 按配方；history 按以往领料估算；none 没有配方也没有以往的领料；"
        "unmatched 没有对应到商品库"
    )
    orders: int | None = Field(description="按以往领料估算时依据的订单数")


class DocumentDraft(BaseModel):
    kind: DocumentKindValue
    order_id: UUID | None
    lines: list[DraftLine]
    missing: list[str] = Field(
        description="领料单：既没有配方、也没有以往领料的商品（或没有对应到商品库的订单行），要手动"
        "添加；入库单：没有对应到成品的订单行"
    )
    items: list[DraftItem] = Field(description="领料单：这次加工的商品")
    estimated: list[str] = Field(description="领料单：没有配方、按以往领料估算的商品")
    covered: bool = Field(description="领料单：按配方和估算要领的材料这个订单都已经领了")


class LinkableOrder(BaseModel):
    """开领料单时可以关联的订单（加工中、还没加工完成）。"""

    id: UUID
    no: str
    status_label: str
    worker_name: str | None = Field(description="领取加工的工人")
    items: str = Field(description="需要加工的商品，例如“铝合金窗 × 2、纱窗 × 1”")


class LinkableOrderList(BaseModel):
    items: list[LinkableOrder]


class WarehouseCounts(BaseModel):
    pending_requisitions: int = Field(description="待确认的领料单")
    pending_receipts: int = Field(description="待确认的入库单")
    low_materials: int = Field(description="库存不足的材料")
    low_goods: int = Field(description="库存不足的成品")


class StockItemOut(BaseModel):
    """仓库里的一个商品（没有价格）。"""

    id: UUID
    code: str | None
    name: str
    model: str
    spec: str
    category: str
    unit: str
    kind: ProductKindValue
    ready_made: bool
    status: Literal["on", "off"]
    stock: Qty | None
    stock_reserved: Qty
    stock_available: Qty | None
    stock_alert: Qty | None
    stock_low: bool
    materials: int = Field(description="配方里的材料数（成品）")
    remark: str


class StockItemPage(BaseModel):
    items: list[StockItemOut]
    total: int
    low_stock: int = Field(description="这个类别里库存不足的数量（不受其他筛选条件影响）")


class StockSuggestion(BaseModel):
    """开领料单、入库单时联想的一个商品（§25.16，没有价格）。"""

    item: StockItemOut
    score: float = Field(description="匹配程度（约 0 到 1），最近用过的为 0")
    field: SuggestField | None = Field(description="按哪个字段找到的（同 ProductSuggestion）")
    match: SuggestMatchValue


class StockSuggestions(BaseModel):
    items: list[StockSuggestion]
    recent: bool = Field(description="没有输入关键词：自己最近开单用过的")


class WarehouseSettingsOut(BaseModel):
    confirm_required: bool
    keeper_id: UUID | None = Field(description="设置里指定的仓管")
    keeper_name: str | None
    effective_keeper_id: UUID | None = Field(
        description="实际的仓管：指定的员工；没有指定（或已停用）时是最早创建的工人"
        "（有“仓管”角色的员工时为空，见 by_role）"
    )
    effective_keeper_name: str | None = Field(
        description="实际的仓管的姓名；有“仓管”角色的员工时是他们的姓名（用顿号隔开）"
    )
    fallback: bool = Field(description="没有指定仓管，由最早创建的工人担任")
    by_role: bool = Field(
        default=False, description="没有指定仓管，有“仓管”角色的员工都是仓管（§25.15）"
    )
    can_edit: bool = Field(description="可以修改（有订单设置权限）")


class WarehouseSettingsIn(BaseModel):
    confirm_required: bool = True
    keeper_id: UUID | None = None
