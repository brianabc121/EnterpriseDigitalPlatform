import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Metric(StrEnum):
    """用量指标。累计类指标按日求和；快照类指标取当天最后一次汇总时的值。"""

    MESSAGES_IN = "messages_in"  # 客户消息
    AGENT_MESSAGES = "agent_messages"  # 坐席消息
    BOT_MESSAGES = "bot_messages"  # 智能客服消息（P3）
    SESSIONS = "sessions"  # 新会话
    HUMAN_SESSIONS = "human_sessions"  # 分配给坐席的会话
    NEW_CUSTOMERS = "new_customers"  # 新客户
    ACTIVE_AGENTS = "active_agents"  # 当天接待过或发过消息的坐席
    FILE_BYTES = "file_bytes"  # 聊天中发送的图片和文件
    AI_SESSIONS = "ai_sessions"  # 由 AI 接待的会话（P3）
    AI_HANDOFFS = "ai_handoffs"  # AI 接待中转人工的次数
    LLM_TOKENS = "llm_tokens"  # 大模型调用消耗的 tokens（输入加输出）
    SEATS = "seats"  # 启用的员工账号（快照）
    CHANNELS = "channels"  # 启用的接入渠道（快照）


SNAPSHOT_METRICS = frozenset({Metric.SEATS, Metric.CHANNELS})
# 取当天的最大值而不是求和：活跃坐席数按日去重，跨天求和没有意义。
MAX_METRICS = frozenset({Metric.ACTIVE_AGENTS})

METRIC_LABELS: dict[Metric, tuple[str, str]] = {
    Metric.MESSAGES_IN: ("客户消息", "条"),
    Metric.AGENT_MESSAGES: ("坐席消息", "条"),
    Metric.BOT_MESSAGES: ("智能客服消息", "条"),
    Metric.SESSIONS: ("新会话", "个"),
    Metric.HUMAN_SESSIONS: ("人工接待会话", "个"),
    Metric.NEW_CUSTOMERS: ("新客户", "个"),
    Metric.ACTIVE_AGENTS: ("活跃坐席", "人"),
    Metric.FILE_BYTES: ("聊天文件", "字节"),
    Metric.AI_SESSIONS: ("AI 接待会话", "个"),
    Metric.AI_HANDOFFS: ("AI 转人工", "次"),
    Metric.LLM_TOKENS: ("大模型 tokens", "个"),
    Metric.SEATS: ("员工账号", "个"),
    Metric.CHANNELS: ("接入渠道", "个"),
}


class UsageDaily(Base):
    """租户每日用量（设计文档 §7.3）。由调度进程汇总，租户只读。"""

    __tablename__ = "usage_daily"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    day: Mapped[date] = mapped_column(primary_key=True)
    metric: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
