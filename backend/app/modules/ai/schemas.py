from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator

Keyword = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]


class CustomIntent(BaseModel):
    """租户自定义的"真实意图"类别（设计文档 §32.7），加进判断的选项。"""

    name: str = Field(min_length=1, max_length=16, description="名称，如 定制尺寸")
    description: str = Field(default="", max_length=60, description="说明，帮助模型判断")

    @field_validator("name", "description")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


def _unique_custom(items: list[CustomIntent]) -> list[CustomIntent]:
    names = [item.name for item in items]
    if any(not name for name in names):
        raise ValueError("自定义意图的名称不能为空")
    if len(set(names)) != len(names):
        raise ValueError("自定义意图的名称不能重复")
    return items


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
    extraction_enabled: bool = Field(description="自动从已结束的会话提炼知识候选（需要大模型）")
    auto_merge_similar: bool = Field(
        description="相似问法证据足够多（3 条以上）时自动并入原问答，不经审核"
    )
    rewrite_enabled: bool = Field(description="检索前改写问题：补全指代、拆分多个问题")
    answer_cache: bool = Field(description="相同的问题直接用之前的回答（知识变化后自动失效）")
    tools_enabled: bool = Field(
        description="允许 AI 调用工具：再次检索、查看客户档案、登记线索、转人工、登记留言"
    )
    segment_replies: bool = Field(description="较长的回答分段发送，发送前显示正在输入")
    intent_enabled: bool = Field(description="判断客户的下单意向和真实意图（坐席工作台显示，§32）")
    intent_in_reply: bool = Field(
        description="AI 回复参考意图判断：回复要求、要人工时转人工、情绪信号、分配意图"
    )
    intent_handoff_stage: int | None = Field(
        description="高意向客户转人工：3 意向明确时、4 准备下单时，为空不转"
    )
    custom_intents: list[CustomIntent] = Field(description="自定义的真实意图类别（最多 10 个）")
    intent_source: Literal["judge", "llm", "none"] = Field(
        description="平台的意图判断：judge 判断模型、llm 大模型的轻量模型、none 没有配置"
    )
    intent_model: str | None = Field(description="判断用的供应商和模型")
    tools_supported: bool = Field(description="当前使用的模型是否支持工具调用")
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
    extraction_enabled: bool | None = None
    auto_merge_similar: bool | None = None
    rewrite_enabled: bool | None = None
    answer_cache: bool | None = None
    tools_enabled: bool | None = None
    segment_replies: bool | None = None
    intent_enabled: bool | None = None
    intent_in_reply: bool | None = None
    intent_handoff_stage: Literal[3, 4] | None = Field(
        default=None, description="3 意向明确时、4 准备下单时转人工；传 null 表示不转"
    )
    custom_intents: list[CustomIntent] | None = Field(default=None, max_length=10)

    @field_validator("custom_intents")
    @classmethod
    def _custom(cls, items: list[CustomIntent] | None) -> list[CustomIntent] | None:
        return None if items is None else _unique_custom(items)


class IntentOption(BaseModel):
    code: str = Field(description="编码：内置的如 price，自定义的是 x:名称")
    label: str
    probability: float


class IntentJudgmentOut(BaseModel):
    """一次意图判断（设计文档 §32.3）。"""

    stage: int = Field(
        ge=0, le=4, description="下单意向：0 没有、1 随便了解、2 有兴趣、3 意向明确、4 准备下单"
    )
    stage_label: str
    stage_probability: float = Field(description="这一阶段的概率")
    purchase_probability: float = Field(description="有下单意向的概率（意向明确与准备下单之和）")
    has_purchase_intent: bool = Field(description="有下单意向（概率不低于 50%）")
    score: float = Field(description="下单意向按概率加权的位置（0–4）")
    distribution: list[float] = Field(description="5 级各自的概率")
    intent: str | None = Field(description="真实意图的编码")
    intent_label: str | None
    intent_probability: float | None
    intents: list[IntentOption] = Field(description="可能的意图（概率最高的前 3 个）")
    concerns: list[IntentOption] = Field(description="客户在意的（价格、质量效果……），最多两个")
    emotion: float | None = Field(description="情绪刻度：0 平静、1 有些着急、2 生气激动")
    emotion_label: str | None
    human_probability: float | None = Field(description="客户在要求人工的概率")
    route: str | None = Field(description="分配意图（路由策略的意图名称）")
    source: Literal["judge", "llm"] = Field(description="judge 判断模型、llm 大模型的轻量模型")
    model: str | None


class IntentHistoryPoint(BaseModel):
    at: datetime
    stage: int
    stage_label: str
    purchase_probability: float
    intent_label: str | None


class SessionIntentOut(IntentJudgmentOut):
    """会话最新的意图判断与这次会话的变化。"""

    session_id: UUID
    message_id: UUID | None = Field(description="判断覆盖到的最后一条客户消息")
    judged_at: datetime
    peak_stage: int | None = Field(description="这次会话到过的最高阶段")
    peak_at: datetime | None
    pending: bool = Field(description="有新的客户消息正在等待判断")
    history: list[IntentHistoryPoint] = Field(description="最近 20 次判断，按时间先后")


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
        "ai_unavailable、quota、product_not_found（连续两次找不到客户要的商品）；回复时为空，"
        "guardrail_retry 表示回复未通过护栏、改发兜底话术，price_probe 表示识别到套价、"
        "reply_blocked 表示回复出现内部价格信息，都改用固定话术"
    )
    reply: str | None
    score: float = Field(description="软信号得分")
    signals: dict[str, Any]
    guard: str | None = Field(
        description="未通过的护栏：empty、too_long、promise、sensitive、bad_output；价格保护："
        "price_probe（套价）、price_internal_term（内部价格口径）、price_cost_amount（成本价金额）"
    )
    knowledge: list[KnowledgeRef]
    intent: IntentJudgmentOut | None = Field(
        default=None, description="试一试：这个问题的意图判断（没有配置判断时为空）"
    )


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


class EvalCaseSet(BaseModel):
    """内置的评测集（如套价话术），可以直接用来评测。"""

    name: str
    cases: list[EvalCase]


class EvalCaseResult(BaseModel):
    question: str
    expect_handoff: bool
    action: str
    reason: str | None
    reply: str | None
    handoff_correct: bool
    answer_correct: bool | None = Field(description="期望回复且给出了关键词时判断；否则为空")
    cost_leak: bool | None = Field(
        default=None,
        description="回复里出现了商品的成本价（开通了订单功能时检查；应当始终为 false）",
    )


class EvalRunOut(BaseModel):
    id: UUID
    cases: int
    answer_accuracy: float | None
    handoff_accuracy: float | None
    cost_leaks: int | None = Field(
        default=None, description="回复里出现了成本价的样例数（设计文档 §25.2 要求为 0）"
    )
    results: list[EvalCaseResult]
    created_at: datetime


class EvalRunList(BaseModel):
    items: list[EvalRunOut]


class OwnLlmOut(BaseModel):
    base_url: str
    chat_model: str
    fast_model: str
    enabled: bool
    api_key_set: bool
    supports_tools: bool = False


class TenantLlmConfig(BaseModel):
    source: str = Field(
        description="tenant 自带密钥、provider 平台指定、default 平台默认、env、none"
    )
    provider_name: str | None
    own: OwnLlmOut | None = Field(description="租户自带的接口配置")


class OwnLlmUpdate(BaseModel):
    """自带大模型接口（OpenAI 兼容）：AI 接待、坐席助手、知识提炼都改用它，费用由租户承担。"""

    base_url: str = Field(min_length=8, max_length=500, pattern=r"^https?://\S+$")
    api_key: str | None = Field(default=None, max_length=500, description="不传表示不修改")
    chat_model: str = Field(min_length=1, max_length=128)
    fast_model: str = Field(default="", max_length=128)
    enabled: bool = True
    supports_tools: bool = Field(default=False, description="模型支持函数调用（tools）")


# ---- 会话小结与坐席助手提醒 ----


class SessionSummaryOut(BaseModel):
    session_id: UUID
    customer_id: UUID
    summary: str
    tags: list[str]
    status: str = Field(description="draft（待确认）、confirmed（已写入客户档案）、discarded")
    generated_at: datetime
    confirmed_by: UUID | None
    confirmed_at: datetime | None


class SummaryConfirm(BaseModel):
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    tags: list[Keyword] = Field(default_factory=list, max_length=5)


class CopilotAlertOut(BaseModel):
    id: UUID
    kind: str = Field(description="negative、escalation、sensitive_info、promise")
    text: str
    staff_id: UUID | None = Field(description="提醒给哪位员工")
    message_id: UUID | None
    created_at: datetime


class CopilotAlertList(BaseModel):
    items: list[CopilotAlertOut]
