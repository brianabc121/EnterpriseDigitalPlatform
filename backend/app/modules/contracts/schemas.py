import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.modules.contracts.settings import ContractSettings

ContractStatusValue = Literal["draft", "final", "signed", "void"]
# 列表的状态页签：快到期、已到期是已签署的合同按结束日期算出来的。
ContractView = Literal["all", "draft", "final", "signed", "expiring", "expired", "void"]
ContractTemplateStatusValue = Literal["active", "disabled"]
TemplateFileType = Literal[".docx", ".pdf", ".md", ".markdown", ".txt"]

Name = Field(min_length=1, max_length=64)


class ContractCategoryOut(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    sort: int
    templates: int = Field(description="直接在这个分类下的模板数")
    contracts: int = Field(description="直接在这个分类下的合同数（看得到的）")


class ContractCategoryList(BaseModel):
    items: list[ContractCategoryOut]
    max_depth: int


class ContractCategoryCreate(BaseModel):
    name: str = Name
    parent_id: uuid.UUID | None = None
    sort: int | None = Field(default=None, ge=0, le=100_000)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("名称不能为空")
        return value


class ContractCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    parent_id: uuid.UUID | None = Field(
        default=None, description="移到这个上级下面（null 为第一级）"
    )
    sort: int | None = Field(default=None, ge=0, le=100_000)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("名称不能为空")
        return value


class ContractTemplateField(BaseModel):
    """模板的填写项：名称和正文里的 {{名称}} 对应。"""

    name: str = Field(min_length=1, max_length=40)
    hint: str = Field(default="", max_length=200, description="填写说明（AI 起草时也会参考）")
    default: str = Field(default="", max_length=500, description="默认值")
    builtin: bool = Field(default=False, description="内置填写项：由系统按数据填写")


class ContractTemplateSummary(BaseModel):
    id: uuid.UUID
    category_id: uuid.UUID | None
    category_path: str
    name: str
    description: str | None
    status: ContractTemplateStatusValue
    field_count: int
    used_count: int
    file_name: str | None
    created_by: uuid.UUID | None
    created_by_name: str | None
    updated_at: datetime
    can_edit: bool


class ContractTemplateOut(ContractTemplateSummary):
    body: str
    fields: list[ContractTemplateField]


class ContractTemplatePage(BaseModel):
    items: list[ContractTemplateSummary]
    total: int


class ContractTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    category_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=1000)
    body: str = Field(default="", max_length=200_000)
    fields: list[ContractTemplateField] = Field(default_factory=list, max_length=100)


class ContractTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    category_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=1000)
    body: str | None = Field(default=None, max_length=200_000)
    fields: list[ContractTemplateField] | None = Field(default=None, max_length=100)
    status: ContractTemplateStatusValue | None = None


class ContractTemplateUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1, description="文件内容（base64），最大 10 MB")
    name: str | None = Field(default=None, max_length=128, description="模板名称（默认取文件名）")
    category_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=1000)


class ContractTemplateUploadOut(BaseModel):
    template: ContractTemplateOut
    detected: list[str] = Field(description="上传时把空白识别成的填写项")


class ContractKnowledgeRef(BaseModel):
    item_id: uuid.UUID
    title: str
    version: int | None = None
    policy: bool = False
    used: bool = Field(default=False, description="AI 说起草时用到了它")


class ContractAiInfo(BaseModel):
    """AI 起草的依据（§34.3）。"""

    model: str | None = None
    knowledge: list[ContractKnowledgeRef] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, description="需要人确认的地方")
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = Field(default=0, description="费用（分）")
    generated_at: datetime | None = None
    requirement: str | None = None


class ContractSummary(BaseModel):
    id: uuid.UUID
    no: str
    title: str
    category_id: uuid.UUID | None
    category_path: str
    template_id: uuid.UUID | None
    template_name: str | None
    customer_id: uuid.UUID | None
    customer_name: str | None
    order_id: uuid.UUID | None
    order_no: str | None
    owner_id: uuid.UUID | None
    owner_name: str | None
    status: ContractStatusValue
    expiry: Literal["expiring", "expired"] | None = Field(
        default=None, description="已签署的合同：快到期或已到期"
    )
    amount: Decimal | None
    sign_date: date | None
    start_date: date | None
    end_date: date | None
    missing: list[str] = Field(description="还没填的填写项")
    ai_generated: bool
    created_at: datetime
    updated_at: datetime


class ContractOut(ContractSummary):
    body: str
    field_values: dict[str, str]
    fields: list[ContractTemplateField] = Field(description="正文里的填写项（内置的标出来）")
    requirement: str | None
    ai: ContractAiInfo | None
    void_reason: str | None
    scan_name: str | None
    created_by: uuid.UUID | None
    created_by_name: str | None
    finalized_at: datetime | None
    signed_at: datetime | None
    voided_at: datetime | None
    can_edit: bool = Field(description="可以修改正文和填写项（草稿，自己负责或有管理权限）")
    can_manage: bool = Field(description="可以定稿、签署、作废（自己负责或有管理权限）")


class ContractPage(BaseModel):
    items: list[ContractSummary]
    total: int
    counts: dict[str, int] = Field(description="各状态页签的数量")


class ContractCreate(BaseModel):
    """新建合同：空白或者按模板（只填内置填写项）。"""

    title: str | None = Field(default=None, max_length=200)
    category_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    body: str | None = Field(default=None, max_length=200_000)


class ContractGenerate(BaseModel):
    """AI 生成合同（§34.3）。"""

    requirement: str = Field(min_length=4, max_length=4000, description="需求")
    title: str | None = Field(default=None, max_length=200)
    category_id: uuid.UUID | None = None
    template_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None


class ContractUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    category_id: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    start_date: date | None = None
    end_date: date | None = None
    body: str | None = Field(default=None, max_length=200_000)
    field_values: dict[str, str] | None = None

    @field_validator("field_values")
    @classmethod
    def _values(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if len(value) > 200:
            raise ValueError("填写项太多")
        return {k.strip()[:40]: v[:5000] for k, v in value.items() if k.strip()}


class ContractScanFile(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1, description="扫描件（PDF、JPG、PNG），最大 20 MB")


class ContractSign(BaseModel):
    sign_date: date
    start_date: date | None = None
    end_date: date | None = None
    scan: ContractScanFile | None = None


class ContractVoid(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ContractSaveAsTemplate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    category_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=1000)


class ContractSettingsOut(BaseModel):
    settings: ContractSettings
    builtin: list[ContractTemplateField] = Field(description="内置填写项和说明")


def as_values(raw: dict[str, Any] | None) -> dict[str, str]:
    return {str(k): str(v) for k, v in (raw or {}).items() if v is not None}
