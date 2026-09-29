from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, model_validator

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
Kind = Literal["faq", "doc"]
Visibility = Literal["public", "agent", "admin"]


class KbItemCreate(BaseModel):
    kind: Kind = "faq"
    title: str = Field(min_length=1, max_length=500, description="FAQ 的标准问，或文档标题")
    content: str = Field(min_length=1, max_length=50_000, description="FAQ 的答案，或文档正文")
    questions: list[Question] = Field(
        default_factory=list, max_length=50, description="FAQ 的相似问法"
    )
    category: str = Field(default="", max_length=64)
    tags: list[Tag] = Field(default_factory=list, max_length=20)
    visibility: Visibility = Field(
        default="public", description="public：对客 AI 与坐席可用；agent：仅坐席；admin：仅管理员"
    )
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    publish: bool = Field(default=False, description="创建后立即发布（需要 kb:publish）")

    @model_validator(mode="after")
    def _check(self) -> "KbItemCreate":
        if self.valid_from and self.valid_to and self.valid_from >= self.valid_to:
            raise ValueError("失效时间必须晚于生效时间")
        return self


class KbItemUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    content: str | None = Field(default=None, min_length=1, max_length=50_000)
    questions: list[Question] | None = Field(default=None, max_length=50)
    category: str | None = Field(default=None, max_length=64)
    tags: list[Tag] | None = Field(default=None, max_length=20)
    visibility: Visibility | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class KbItemOut(BaseModel):
    id: UUID
    kind: str
    title: str
    content: str
    questions: list[str]
    category: str
    tags: list[str]
    status: str = Field(description="draft、published 或 archived")
    visibility: str
    valid_from: datetime | None
    valid_to: datetime | None
    source: str
    version: int
    hits: int = Field(description="被 AI 或坐席引用的次数")
    last_hit_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class KbItemPage(BaseModel):
    items: list[KbItemOut]
    total: int


class KbSearchHit(BaseModel):
    item_id: UUID
    kind: str
    title: str
    text: str = Field(description="FAQ 的答案，或文档中最相关的片段")
    score: float = Field(description="相关度（0 到 1）：语义相似度与关键词覆盖率中较高的一个")
    dense: float | None = Field(description="语义相似度；没有配置向量模型时为空")
    lexical: float = Field(description="关键词覆盖率")


class KbSearchResult(BaseModel):
    items: list[KbSearchHit]


class KbImportRequest(BaseModel):
    csv: str = Field(
        min_length=1,
        max_length=2_000_000,
        description="CSV 文本（首行为表头）：标准问、答案必填；相似问用 | 分隔；分类可选",
    )
    publish: bool = Field(default=False, description="导入后立即发布（需要 kb:publish）")


class KbImportResult(BaseModel):
    created: int
    errors: list[str] = Field(description="无法导入的行及原因")
