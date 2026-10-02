"""意向客户（设计文档 §35.8）：意向记录和跟进记录。"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class ProspectStatus(StrEnum):
    SUGGESTED = "suggested"  # AI 建议，员工确认后才进名单
    ACTIVE = "active"  # 跟进中
    WON = "won"  # 已成交
    LOST = "lost"  # 已放弃
    DISMISSED = "dismissed"  # 忽略了 AI 的建议（不在列表里显示，30 天内 AI 不再建议）


OPEN_STATUSES = (ProspectStatus.SUGGESTED, ProspectStatus.ACTIVE)


class ProspectLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ProspectSource(StrEnum):
    AI = "ai"
    STAFF = "staff"


class FollowMethod(StrEnum):
    PHONE = "phone"
    WECHAT = "wechat"
    CHAT = "chat"  # 在线会话（客户又来咨询时系统记的也是这个）
    VISIT = "visit"
    OTHER = "other"


STATUS_LABELS: dict[str, str] = {
    ProspectStatus.SUGGESTED: "待确认",
    ProspectStatus.ACTIVE: "跟进中",
    ProspectStatus.WON: "已成交",
    ProspectStatus.LOST: "已放弃",
    ProspectStatus.DISMISSED: "已忽略",
}
LEVEL_LABELS: dict[str, str] = {
    ProspectLevel.HIGH: "高",
    ProspectLevel.MEDIUM: "中",
    ProspectLevel.LOW: "低",
}
METHOD_LABELS: dict[str, str] = {
    FollowMethod.PHONE: "电话",
    FollowMethod.WECHAT: "微信",
    FollowMethod.CHAT: "在线会话",
    FollowMethod.VISIT: "上门",
    FollowMethod.OTHER: "其他",
}


class CustomerProspect(IdMixin, TimestampMixin, TenantMixin, Base):
    """意向记录：一个客户同时最多一条待确认或跟进中的（部分唯一索引）。"""

    __tablename__ = "customer_prospects"

    customer_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(String(12), server_default=ProspectStatus.ACTIVE.value)
    level: Mapped[str] = mapped_column(String(8), server_default=ProspectLevel.MEDIUM.value)
    interest: Mapped[str | None] = mapped_column(Text)
    concerns: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(8), server_default=ProspectSource.STAFF.value)
    # AI 转入时依据的会话。
    session_id: Mapped[uuid.UUID | None]
    follower_id: Mapped[uuid.UUID | None]
    next_follow_at: Mapped[date | None]
    last_followed_at: Mapped[datetime | None]
    follow_count: Mapped[int] = mapped_column(server_default="0")
    # 成交的订单。
    order_id: Mapped[uuid.UUID | None]
    lost_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None]
    closed_by: Mapped[uuid.UUID | None]
    closed_at: Mapped[datetime | None]
    # 开始跟进（转入、重新跟进）的时间：之后确认的订单算成交，之后的会话算"又来咨询"。
    opened_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProspectFollowup(IdMixin, TenantMixin, Base):
    """跟进记录：staff_id 为空的是系统记的（客户又来咨询了），同一会话只记一次。"""

    __tablename__ = "prospect_followups"

    prospect_id: Mapped[uuid.UUID]
    method: Mapped[str] = mapped_column(String(12), server_default=FollowMethod.OTHER.value)
    content: Mapped[str] = mapped_column(Text)
    next_follow_at: Mapped[date | None]
    staff_id: Mapped[uuid.UUID | None]
    session_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
