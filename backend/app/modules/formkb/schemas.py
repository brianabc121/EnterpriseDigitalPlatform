from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.modules.products.schemas import ProductKindValue, Qty

KindValue = Literal["alias", "usage", "companion"]
FormValue = Literal["order", "requisition", "receipt"]
StatusValue = Literal["observing", "active", "disabled"]
SourceValue = Literal["learned", "manual"]
ReviewValue = Literal["activate", "conflict", "recipe"]
EventValue = Literal["created", "updated", "confirmed"]
SubmissionStatusValue = Literal["pending", "done", "failed"]
UsageBasisValue = Literal["estimate", "extra", "deviation", "recipe"]
PickMatchValue = Literal[
    "exact", "prefix", "contains", "pinyin", "similar", "recent", "learned", "companion"
]
ActionValue = Literal[
    "created",  # 学到新的（观察中）
    "strengthened",  # 加强：多了一次依据
    "activated",  # 开始生效
    "updated",  # 学到的值变了
    "deactivated",  # 回到观察中
    "review",  # 标"待确认"
    "hit",  # 开单时用到
    "added",  # 手工添加
    "edited",  # 手工修改（变为固定）
    "enabled",
    "disabled",
    "locked",
    "unlocked",
    "confirmed",  # 确认生效
    "adopted",  # 换成学到的
    "kept",  # 保持不变
    "recipe",  # 更新配方
]

Text = Annotated[str, Field(max_length=64)]
UsageIn = Annotated[Decimal, Field(gt=0, le=100_000, max_digits=12, decimal_places=3)]


class EntryTrace(BaseModel):
    """明细行是怎么录入的（§25.18 叫法的证据）：录入行选中商品时的输入、之前没找到的输入，或者
    订单里客户的说法（对应到商品库）。只用于学习，不保存在单据上。"""

    query: Text | None = Field(default=None, description="选中商品时录入行里的输入")
    missed: Text | None = Field(
        default=None,
        description="同一次录入里之前没找到的输入（换了说法才找到）；对应到商品库时是客户的说法",
    )
    via: Literal["suggest", "batch", "map"] = Field(
        default="suggest", description="suggest 录入行；batch 批量选择；map 对应到商品库"
    )
    match: PickMatchValue | None = Field(
        default=None, description="选中的候选是怎么找到的（learned 学到的叫法，companion 常一起开）"
    )
    rank: int | None = Field(
        default=None, ge=0, le=100, description="选中的是第几个候选（从 0 开始）"
    )


class ProductBrief(BaseModel):
    id: UUID
    code: str | None
    name: str
    spec: str
    unit: str
    kind: ProductKindValue


class FormKbEntryOut(BaseModel):
    id: UUID
    kind: KindValue
    form: FormValue | None = Field(description="搭配：哪种表单；叫法适用于所有表单（为空）")
    text: str = Field(description="叫法：统一写法后的文字")
    label: str = Field(description="叫法：员工或客户当时的写法")
    product: ProductBrief = Field(description="叫法对应的商品；用量的成品；搭配的商品")
    related: ProductBrief | None = Field(description="用量的材料；搭配里常一起开的商品")
    value: Qty | None = Field(description="用量：每件用多少；搭配：一起出现的比例")
    status: StatusValue
    source: SourceValue
    locked: bool = Field(description="固定：学习不会改动（手工添加或改过的）")
    review: ReviewValue | None = Field(
        description="待确认：activate 达到生效条件等确认；conflict 学到的和固定的不一致；"
        "recipe 实际用量和配方不一致"
    )
    review_note: str | None
    evidence: int = Field(description="依据：几次（叫法、搭配是几张单据，用量是几个订单）")
    share: float | None = Field(description="叫法：选这个商品的比例；搭配：一起出现的比例")
    basis: UsageBasisValue | None = Field(
        description="用量：estimate 没有配方时的估算；extra 配方里没有、常补领的材料；"
        "deviation 和配方不一致；recipe 和配方一致"
    )
    recipe: Qty | None = Field(description="用量：配方里每件的用量（有配方时）")
    sentence: str = Field(description="写成一句话的知识")
    hits: int
    last_hit_at: datetime | None
    learned_at: datetime | None
    updated_at: datetime


class FormKbEntryPage(BaseModel):
    items: list[FormKbEntryOut]
    total: int


class FormKbEvidence(BaseModel):
    at: datetime
    form: FormValue
    record_id: UUID | None
    record_no: str
    actor_name: str | None
    detail: str = Field(description="这次的内容（例如：输入「大窗」选了铝合金窗；每樘 6.5 米）")


class FormKbLogOut(BaseModel):
    at: datetime
    action: ActionValue
    note: str
    actor_name: str | None = Field(description="操作人；系统学习时为空")
    record_no: str | None = Field(description="哪次提交（单号）引起的")


class FormKbEntryDetail(FormKbEntryOut):
    evidence_items: list[FormKbEvidence]
    log: list[FormKbLogOut]
    can_apply_recipe: bool = Field(description="可以把学到的用量写进配方（需要维护商品库的权限）")


class FormKbEntryCreate(BaseModel):
    kind: KindValue
    form: FormValue | None = Field(default=None, description="搭配：哪种表单（默认订单）")
    text: Text | None = Field(default=None, description="叫法：输入的文字")
    product_id: UUID = Field(description="叫法对应的商品；用量的成品；搭配的商品")
    related_id: UUID | None = Field(default=None, description="用量的材料；搭配里常一起开的商品")
    value: UsageIn | None = Field(default=None, description="用量：每件用多少")


class FormKbEntryUpdate(BaseModel):
    """修改（改过的知识变为固定）。"""

    text: Text | None = None
    product_id: UUID | None = None
    value: UsageIn | None = None


class FormKbConfirm(BaseModel):
    decision: Literal["activate", "adopt", "keep"] = Field(
        description="activate 确认生效；adopt 换成学到的；keep 保持不变"
    )


class FormKbResult(BaseModel):
    entry_id: UUID | None
    kind: KindValue
    action: ActionValue
    text: str


class FormKbSubmissionOut(BaseModel):
    id: UUID
    form: FormValue
    event: EventValue
    record_id: UUID
    record_no: str
    actor_name: str | None
    status: SubmissionStatusValue
    created_at: datetime
    processed_at: datetime | None
    result: list[FormKbResult]


class FormKbSubmissionPage(BaseModel):
    items: list[FormKbSubmissionOut]
    total: int


class FormKbSummary(BaseModel):
    active: int
    observing: int
    review: int
    disabled: int
    pending: int = Field(description="还没判断的学习记录")


class FormKbSettingsOut(BaseModel):
    auto_activate: bool
    learn_aliases: bool
    learn_usage: bool
    learn_companions: bool
    can_edit: bool


class FormKbSettingsIn(BaseModel):
    auto_activate: bool
    learn_aliases: bool
    learn_usage: bool
    learn_companions: bool
