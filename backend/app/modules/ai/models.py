import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Double, ForeignKey, ForeignKeyConstraint, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin


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
