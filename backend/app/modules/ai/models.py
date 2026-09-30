import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin
from app.db.types import Vector

EMBED_DIM = 1024


class AiSettings(Base):
    """租户的 AI 接待设置。没有记录时按默认值、不启用。"""

    __tablename__ = "ai_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), primary_key=True, server_default=text("app_current_tenant()")
    )
    enabled: Mapped[bool] = mapped_column(server_default="false")
    bot_name: Mapped[str] = mapped_column(String(32), server_default="智能客服")
    persona: Mapped[str | None] = mapped_column(Text)
    handoff_threshold: Mapped[float] = mapped_column(Double, server_default="0.6")
    max_turns: Mapped[int] = mapped_column(server_default="8")
    relevance_threshold: Mapped[float] = mapped_column(Double, server_default="0.55")
    handoff_keywords: Mapped[list[str]] = mapped_column(server_default="{}")
    sensitive_keywords: Mapped[list[str]] = mapped_column(server_default="{}")
    extraction_enabled: Mapped[bool] = mapped_column(server_default="true")
    auto_merge_similar: Mapped[bool] = mapped_column(server_default="false")
    # 平台运营给租户指定的供应商；租户自带的接口（base_url、api_key_enc、chat_model、fast_model）。
    llm_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("llm_providers.id", ondelete="SET NULL")
    )
    byo_llm: Mapped[dict[str, Any] | None]
    # 问题改写、语义缓存、工具调用、长回答分段发送（设计文档 §11.1、§11.5）。
    rewrite_enabled: Mapped[bool] = mapped_column(server_default="true")
    answer_cache: Mapped[bool] = mapped_column(server_default="true")
    tools_enabled: Mapped[bool] = mapped_column(server_default="false")
    segment_replies: Mapped[bool] = mapped_column(server_default="true")
    # 平台给租户设置的大模型并发上限；为空时用平台默认值（EDP_LLM_TENANT_CONCURRENCY）。
    llm_concurrency: Mapped[int | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class AiSessionState(TenantMixin, Base):
    """AI 接待中的会话：待回复时间与判定用的计数。"""

    __tablename__ = "ai_session_states"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"], ondelete="CASCADE"
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    due_at: Mapped[datetime | None]
    turns: Mapped[int] = mapped_column(server_default="0")
    guard_failures: Mapped[int] = mapped_column(server_default="0")
    repeats: Mapped[int] = mapped_column(server_default="0")
    last_question: Mapped[str | None] = mapped_column(Text)
    answered_until: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class DecisionAction(StrEnum):
    REPLY = "reply"
    HANDOFF = "handoff"


class AiDecision(IdMixin, TenantMixin, Base):
    """一次 AI 回复或转人工的判定留痕（设计文档 §11.2）。"""

    __tablename__ = "ai_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"], ondelete="CASCADE"
        ),
    )

    session_id: Mapped[uuid.UUID]
    question: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(12))
    reason: Mapped[str | None] = mapped_column(String(32))
    score: Mapped[float] = mapped_column(Double, server_default="0")
    signals: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    reply: Mapped[str | None] = mapped_column(Text)
    knowledge: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LlmCall(IdMixin, TenantMixin, Base):
    """一次大模型调用的记账。"""

    __tablename__ = "llm_calls"

    scene: Mapped[str] = mapped_column(String(24))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_tokens: Mapped[int] = mapped_column(server_default="0")
    completion_tokens: Mapped[int] = mapped_column(server_default="0")
    latency_ms: Mapped[int] = mapped_column(server_default="0")
    status: Mapped[str] = mapped_column(String(12))
    error: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[uuid.UUID | None]
    # 按供应商价格估算的费用（分）；租户自带接口密钥的调用不计。
    cost: Mapped[float] = mapped_column(Double, server_default="0")
    # 使用的提示词版本，如 reply@3、reply@builtin。
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AiEvalRun(IdMixin, TenantMixin, Base):
    """一次评测：用例、结果与指标。"""

    __tablename__ = "ai_eval_runs"

    created_by: Mapped[uuid.UUID | None]
    cases: Mapped[int]
    answer_accuracy: Mapped[float | None] = mapped_column(Double)
    handoff_accuracy: Mapped[float | None] = mapped_column(Double)
    results: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AiSuggestion(IdMixin, TenantMixin, Base):
    """坐席助手的一次建议，用于统计采纳率。"""

    __tablename__ = "ai_suggestions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"], ondelete="CASCADE"
        ),
    )

    session_id: Mapped[uuid.UUID]
    staff_id: Mapped[uuid.UUID | None]
    suggestions: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AiAnswerCache(IdMixin, TenantMixin, Base):
    """语义缓存：同一个问题（向量相似）直接用之前通过了护栏的回答。知识或 AI 设置变化时清空。"""

    __tablename__ = "ai_answer_cache"

    question: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    answer: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Double)
    knowledge: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")
    hits: Mapped[int] = mapped_column(server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]


class AiMessageFeedback(TenantMixin, Base):
    """访客对一条 AI 回答的评价（有用 1 / 没用 -1），同时记到所依据的知识上。"""

    __tablename__ = "ai_message_feedback"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"], ondelete="CASCADE"
        ),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    session_id: Mapped[uuid.UUID]
    value: Mapped[int] = mapped_column(SmallInteger)
    item_ids: Mapped[list[uuid.UUID]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AlertKind(StrEnum):
    NEGATIVE = "negative"  # 客户情绪负面
    ESCALATION = "escalation"  # 情绪持续升级
    SENSITIVE_INFO = "sensitive_info"  # 客户发来身份证号、银行卡号等敏感信息
    PROMISE = "promise"  # 坐席使用了承诺类话术


class CopilotAlert(IdMixin, TenantMixin, Base):
    """坐席助手的实时提醒（设计文档 §11.4），留痕用于质检。"""

    __tablename__ = "copilot_alerts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"], ondelete="CASCADE"
        ),
    )

    session_id: Mapped[uuid.UUID]
    staff_id: Mapped[uuid.UUID | None]
    message_id: Mapped[uuid.UUID | None]
    kind: Mapped[str] = mapped_column(String(24))
    detail: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SummaryStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    DISCARDED = "discarded"


class SessionSummary(TenantMixin, Base):
    """会话小结：人工会话结束时生成，坐席确认后写入客户档案（设计文档 §11.4）。"""

    __tablename__ = "session_summaries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            ondelete="CASCADE",
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    customer_id: Mapped[uuid.UUID]
    summary: Mapped[str] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(server_default="{}")
    status: Mapped[str] = mapped_column(String(12), server_default=SummaryStatus.DRAFT.value)
    generated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    confirmed_by: Mapped[uuid.UUID | None]
    confirmed_at: Mapped[datetime | None]


class SecurityEventKind(StrEnum):
    PRICE_PROBE = "price_probe"  # 客户套问成本价、底价，或诱导 AI 越权（用固定话术答复）
    REPLY_BLOCKED = "reply_blocked"  # AI 的回复出现内部价格口径或成本价金额，已拦截


class AiSecurityEvent(IdMixin, TenantMixin, Base):
    """AI 安全事件（设计文档 §25.2）：套价识别与回复拦截，留痕并计入指标。"""

    __tablename__ = "ai_security_events"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"]),
        ForeignKeyConstraint(["tenant_id", "customer_id"], ["customers.tenant_id", "customers.id"]),
    )

    kind: Mapped[str] = mapped_column(String(16))
    session_id: Mapped[uuid.UUID | None]
    customer_id: Mapped[uuid.UUID | None]
    detail: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
