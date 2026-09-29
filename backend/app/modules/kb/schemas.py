from datetime import date, datetime
from typing import Annotated, Any, Literal
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
    must_read: bool = Field(default=False, description="必读：发布或更新后坐席需要确认已读")
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
    must_read: bool | None = None


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
    must_read: bool
    likes: int = Field(description="员工评价为有用的人数")
    dislikes: int = Field(description="员工评价为没用的人数")
    archived_at: datetime | None
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


class KbVersionOut(BaseModel):
    version: int
    change: str = Field(description="created 首次发布、updated 修改、restored 回滚、merged 合并")
    note: str | None
    title: str
    content: str
    questions: list[str]
    category: str
    visibility: str
    valid_from: datetime | None
    valid_to: datetime | None
    must_read: bool
    published_by: UUID | None
    published_by_name: str | None
    created_at: datetime


class KbVersionList(BaseModel):
    items: list[KbVersionOut]


class KbFeedEvent(BaseModel):
    item_id: UUID
    kind: str
    title: str
    version: int
    change: str
    note: str | None
    must_read: bool = Field(description="这条知识目前是必读")
    read: bool = Field(description="我已确认当前版本（非必读时总为 true）")
    created_at: datetime


class KbFeed(BaseModel):
    """知识动态（设计 §12.6）：待我确认的必读知识，以及最近发布和更新的知识。"""

    must_read: list[KbFeedEvent]
    events: list[KbFeedEvent]


class KbReader(BaseModel):
    staff_id: UUID
    display_name: str
    read_at: datetime | None


class KbReadStats(BaseModel):
    version: int
    total: int = Field(description="需要确认的员工数（有接待权限的在职员工）")
    confirmed: int
    rate: float | None
    readers: list[KbReader] = Field(description="已确认的在前，未确认的在后")


class KbFeedbackRequest(BaseModel):
    value: Literal[-1, 0, 1] = Field(description="1 有用，-1 没用，0 取消评价")


class KbFeedbackOut(BaseModel):
    likes: int
    dislikes: int
    mine: int


class KbEvidenceLine(BaseModel):
    role: str
    text: str


class KbEvidence(BaseModel):
    """一段证据对话（提炼时已脱敏）。"""

    session_id: UUID
    seen_at: datetime
    question: str | None = None
    lines: list[KbEvidenceLine]


class KbCandidateOut(BaseModel):
    id: UUID
    kind: str = Field(description="new 新问题、similar 相似问法、conflict 答案冲突、gap 知识缺口")
    status: str = Field(description="pending、approved、merged、rejected")
    question: str
    answer: str | None
    category: str
    target_item_id: UUID | None = Field(description="相似或冲突时对应的已有知识")
    target_title: str | None
    similarity: float | None = Field(description="与已有知识的相关度")
    confidence: float | None = Field(description="提炼时模型给出的把握")
    time_sensitive: bool = Field(description="活动、价格等会过期的信息")
    occurrences: int = Field(description="出现次数（相似的候选聚为一类）")
    recent: int = Field(description="最近 7 天出现的次数")
    variants: list[str] = Field(description="出现过的问法")
    first_seen_at: datetime
    last_seen_at: datetime
    review_note: str | None
    reviewed_at: datetime | None
    reviewed_by_name: str | None
    result_item_id: UUID | None
    model: str | None
    prompt_version: str | None


class KbCandidatePage(BaseModel):
    items: list[KbCandidateOut]
    total: int
    pending: dict[str, int] = Field(description="各类待审候选的数量")


class KbCandidateDetail(KbCandidateOut):
    evidence: list[KbEvidence]
    target: KbItemOut | None
    similar: list[KbSearchHit] = Field(description="相似的已有知识")


class KbCandidateApprove(BaseModel):
    """通过（可以先编辑）。新问题、缺口新建为问答；相似问法并入原问答；冲突用答案更新原问答。"""

    question: str | None = Field(default=None, min_length=1, max_length=500)
    answer: str | None = Field(default=None, min_length=1, max_length=50_000)
    category: str | None = Field(default=None, max_length=64)
    visibility: Literal["public", "agent"] | None = Field(
        default=None, description="新建时的可见范围；agent 为仅坐席可见"
    )


class KbCandidateMerge(BaseModel):
    item_id: UUID = Field(description="合并到的已有知识")
    answer: str | None = Field(
        default=None, min_length=1, max_length=50_000, description="同时替换原知识的答案"
    )


class KbCandidateReject(BaseModel):
    reason: str = Field(min_length=1, max_length=500, description="驳回理由（用于改进提炼）")


class KbReasonCount(BaseModel):
    reason: str
    count: int


class KbItemStat(BaseModel):
    item_id: UUID
    title: str
    count: int = Field(description="被引用次数，或评价为没用的人数")


class KbMetrics(BaseModel):
    """知识运营指标（设计 §12.7）。时间范围内的统计；缺口、长期未命中为当前数量。"""

    start: date
    end: date
    timezone: str
    ai_replies: int = Field(description="AI 回复的轮数")
    knowledge_hit_rate: float | None = Field(description="知识命中率：AI 回复时引用了知识的比例")
    handoff_reasons: list[KbReasonCount] = Field(description="AI 转人工的原因分布")
    gaps_open: int = Field(description="待处理的知识缺口")
    gaps_new: int
    gaps_closed: int
    gap_close_hours: float | None = Field(description="缺口从首次出现到处理的平均小时数")
    candidates_new: int
    candidates_reviewed: int
    candidates_accepted: int = Field(description="通过或合并的候选")
    pass_rate: float | None = Field(description="候选通过率")
    suggestions: int = Field(description="坐席请求 AI 建议的次数")
    suggestions_adopted: int = Field(description="坐席采用 AI 建议发出的消息")
    adoption_rate: float | None
    stale_items: int = Field(description="长期未命中的知识")
    top_items: list[KbItemStat] = Field(description="AI 引用最多的知识")
    disliked_items: list[KbItemStat] = Field(description="评价为没用最多的知识")


class KbDigestOut(BaseModel):
    week_start: date
    data: dict[str, Any]
    created_at: datetime


class KbDigestList(BaseModel):
    items: list[KbDigestOut]


class KbDigestRequest(BaseModel):
    week_start: date | None = Field(default=None, description="这一周中的任意一天，默认本周")
