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
    is_default: Mapped[bool] = mapped_column(server_default="false")
    enabled: Mapped[bool] = mapped_column(server_default="true")
