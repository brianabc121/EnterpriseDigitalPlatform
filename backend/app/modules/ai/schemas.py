from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

Keyword = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]


class AiSettingsOut(BaseModel):
    enabled: bool = Field(description="启用 AI 接待（路由策略为 AI 优先的渠道生效）")
    bot_name: str
    persona: str | None = Field(description="语气与风格的补充说明，会放进系统提示")
    handoff_threshold: float = Field(description="软信号得分达到这个值时转人工（默认 0.6）")
    max_turns: int = Field(description="AI 接待超过这么多轮仍未解决时计入转人工信号")
    relevance_threshold: float = Field(description="知识相关度低于这个值视为知识缺失")
    handoff_keywords: list[str] = Field(description="除内置词外，客户说这些词时立即转人工")
    sensitive_keywords: list[str] = Field(
        description="除内置词外的敏感词：客户提到时转人工，回复里出现时不发送"
    )
    llm_configured: bool = Field(description="平台是否配置了大模型")
    embeddings_configured: bool = Field(description="平台是否配置了向量模型（语义检索）")
    monthly_quota: int | None = Field(description="每月 AI 回复条数上限（套餐额度），空为不限")
    used_this_month: int


class AiSettingsUpdate(BaseModel):
    enabled: bool | None = None
    bot_name: str | None = Field(default=None, min_length=1, max_length=32)
    persona: str | None = Field(default=None, max_length=500)
    handoff_threshold: float | None = Field(default=None, gt=0, le=2)
    max_turns: int | None = Field(default=None, ge=1, le=50)
    relevance_threshold: float | None = Field(default=None, ge=0, le=1)
    handoff_keywords: list[Keyword] | None = Field(default=None, max_length=50)
    sensitive_keywords: list[Keyword] | None = Field(default=None, max_length=200)


class KnowledgeRef(BaseModel):
    item_id: UUID
    title: str
    score: float


class AiTestRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class AiOutcome(BaseModel):
    """一次 AI 判定：回复或转人工，以及依据。"""

    action: Literal["reply", "handoff"]
    reason: str | None = Field(
        description="转人工原因：customer_request、sensitive、vip、model_request、score、guardrail、"
        "ai_unavailable、quota；回复时为空，guardrail_retry 表示回复未通过护栏、改发兜底话术"
    )
    reply: str | None
    score: float = Field(description="软信号得分")
    signals: dict[str, Any]
    guard: str | None = Field(
        description="未通过的护栏：empty、too_long、promise、sensitive、bad_output"
    )
    knowledge: list[KnowledgeRef]


class AiDecisionOut(AiOutcome):
    id: UUID
    question: str
    created_at: datetime


class AiDecisionList(BaseModel):
    items: list[AiDecisionOut]


class SuggestionList(BaseModel):
    suggestions: list[str]
    knowledge: list[KnowledgeRef]


class EvalCase(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    expect_handoff: bool = Field(default=False, description="期望转人工")
    expect_keywords: list[Keyword] = Field(
        default_factory=list,
        max_length=10,
        description="期望回复中包含的关键词（全部包含才算正确）",
    )


class EvalRequest(BaseModel):
    cases: list[EvalCase] = Field(min_length=1, max_length=100)


class EvalCaseResult(BaseModel):
    question: str
    expect_handoff: bool
    action: str
    reason: str | None
    reply: str | None
    handoff_correct: bool
    answer_correct: bool | None = Field(description="期望回复且给出了关键词时判断；否则为空")


class EvalRunOut(BaseModel):
    id: UUID
    cases: int
    answer_accuracy: float | None
    handoff_accuracy: float | None
    results: list[EvalCaseResult]
    created_at: datetime


class EvalRunList(BaseModel):
    items: list[EvalRunOut]
