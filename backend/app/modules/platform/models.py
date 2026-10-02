import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin


class PlatformSetting(Base):
    """平台级表：平台设置（计费宽限期、自助注册、全局敏感词、模型路由等），按键保存。"""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    updated_by: Mapped[uuid.UUID | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class LlmProvider(IdMixin, TimestampMixin, Base):
    """平台级表：大模型供应商（OpenAI 兼容接口）。接口密钥加密保存。"""

    __tablename__ = "llm_providers"

    name: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(Text)
    api_key_enc: Mapped[str] = mapped_column(Text, server_default="")
    chat_model: Mapped[str] = mapped_column(String(128))
    fast_model: Mapped[str] = mapped_column(String(128), server_default="")
    embed_model: Mapped[str] = mapped_column(String(128), server_default="")
    embed_dim: Mapped[int] = mapped_column(server_default="1024")
    send_dimensions: Mapped[bool] = mapped_column(server_default="false")
    # 每千 tokens 的价格（分），用于估算成本：{"input": 0.1, "output": 0.2}。
    prices: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # 重排序模型（与对话、向量模型同一个接口地址）；能力标签：{"tools": true, ...}。
    rerank_model: Mapped[str] = mapped_column(String(128), server_default="")
    capabilities: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    is_default: Mapped[bool] = mapped_column(server_default="false")
    enabled: Mapped[bool] = mapped_column(server_default="true")
    # 接口类型：openai（OpenAI 兼容：对话、向量、重排序）或 typesafe（判断模型 Jev，设计文档 §32.2，
    # chat_model 是判断模型的名称，只用于"意图判断"场景）。
    protocol: Mapped[str] = mapped_column(String(16), server_default="openai")


class PromptTemplate(IdMixin, Base):
    """平台级表：提示词版本（设计文档 §11.5）。每个场景最多一个启用的版本，没有时用内置提示词。"""

    __tablename__ = "prompt_templates"

    key: Mapped[str] = mapped_column(String(32))
    version: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(server_default="false")
    created_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
