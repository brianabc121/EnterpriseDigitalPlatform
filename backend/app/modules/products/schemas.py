from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.modules.products.service import normalize_category, split_aliases

Money = Decimal
ProductStatusValue = Literal["on", "off"]


class ProductOut(BaseModel):
    id: UUID
    code: str | None
    name: str
    model: str
    spec: str
    category: str
    image_url: str | None
    retail_price: Money | None
    cost_price: Money | None = Field(
        default=None, description="成本价：只有有 product:view_cost 权限时返回"
    )
    cost_visible: bool = Field(description="是否返回了成本价（有查看成本价的权限）")
    remark: str
    aliases: list[str]
    status: ProductStatusValue
    created_at: datetime
    updated_at: datetime


class ProductPage(BaseModel):
    items: list[ProductOut]
    total: int


class ProductCandidate(BaseModel):
    product: ProductOut
    score: float = Field(description="相关度（0 到 1），代码或型号完全相同时为 1")


class ProductSearchResult(BaseModel):
    items: list[ProductCandidate]


class ProductWrite(BaseModel):
    code: str | None = Field(default=None, max_length=64, description="企业的商品编码，租户内唯一")
    name: str = Field(min_length=1, max_length=128)
    model: str = Field(default="", max_length=64)
    spec: str = Field(default="", max_length=128)
    category: str = Field(default="", max_length=128, description="多级用 / 分隔")
    image_url: str | None = Field(
        default=None, max_length=1024, pattern=r"^https?://\S+$", description="图片链接"
    )
    retail_price: Money | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    cost_price: Money | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
        description="成本价：不传、或者没有查看成本价的权限时保持原值",
    )
    remark: str = Field(default="", max_length=2000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    status: ProductStatusValue = "on"

    @field_validator("code")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        return (value or "").strip() or None

    @field_validator("name", "model", "spec", "remark")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        return normalize_category(value)

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, value: list[str]) -> list[str]:
        return split_aliases(value)


class ProductUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=200, description="文件名（.xlsx 或 .csv）")
    content_base64: str = Field(
        min_length=1, max_length=14_000_000, description="文件内容（base64），文件最大 10 MB"
    )


class ImportRowOut(BaseModel):
    row: int = Field(description="表格里的行号（表头是第 1 行）")
    values: dict[str, str] = Field(description="这一行的内容（没有查看成本价的权限时不含成本价）")
    problems: list[str]
    action: Literal["create", "update", "skip"] = Field(description="新增、更新已有商品或跳过")
    product_id: UUID | None = None
    result: Literal["create", "update", "skip"] | None = Field(
        default=None, description="确认导入后的实际结果"
    )


class ProductImportOut(BaseModel):
    id: UUID
    file_name: str
    status: Literal["preview", "done", "cancelled"]
    total: int
    invalid: int = Field(description="有问题（将跳过）的行数")
    will_create: int
    will_update: int
    created: int
    updated: int
    skipped: int
    rows: list[ImportRowOut]
    created_at: datetime
    applied_at: datetime | None


class ProductImportSummary(BaseModel):
    id: UUID
    file_name: str
    status: Literal["preview", "done", "cancelled"]
    total: int
    created: int
    updated: int
    skipped: int
    created_at: datetime


class ProductImportList(BaseModel):
    items: list[ProductImportSummary]


class ProductGapOut(BaseModel):
    id: UUID
    term: str
    sample: str
    count: int
    first_seen_at: datetime
    last_seen_at: datetime


class ProductGapList(BaseModel):
    items: list[ProductGapOut]


class CategoryList(BaseModel):
    items: list[str]
