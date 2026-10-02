"""个人待办（设计文档 §27.2）。与 §24 的客户待办并列：客户待办回答"客户要什么"，个人待办回答
"我今天要做什么"。"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class TaskStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


STATUS_LABELS: dict[str, str] = {
    TaskStatus.OPEN: "待办",
    TaskStatus.DONE: "已完成",
    TaskStatus.CANCELLED: "已取消",
}


class TaskSource(StrEnum):
    SELF = "self"  # 自己新建
    ASSIGNED = "assigned"  # 别人交办
    ASSISTANT = "assistant"  # 对 AI 助理说"提醒我……"
    SYSTEM = "system"  # 系统生成


SOURCE_LABELS: dict[str, str] = {
    TaskSource.SELF: "自己新建",
    TaskSource.ASSIGNED: "交办",
    TaskSource.ASSISTANT: "AI 助理",
    TaskSource.SYSTEM: "系统",
}


class TaskPriority(StrEnum):
    URGENT = "urgent"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


PRIORITY_LABELS: dict[str, str] = {
    TaskPriority.URGENT: "紧急",
    TaskPriority.HIGH: "高",
    TaskPriority.NORMAL: "普通",
    TaskPriority.LOW: "低",
}


class StaffTask(IdMixin, TimestampMixin, TenantMixin, Base):
    """一条个人待办。owner_id 是这件事的主人；created_by 是新建或交办的人。"""

    __tablename__ = "staff_tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "no"),
        ForeignKeyConstraint(
            ["tenant_id", "owner_id"], ["staff.tenant_id", "staff.id"], ondelete="CASCADE"
        ),
    )

    no: Mapped[str] = mapped_column(String(24))
    owner_id: Mapped[uuid.UUID]
    title: Mapped[str] = mapped_column(String(100))
    note: Mapped[str] = mapped_column(Text, server_default="")
    priority: Mapped[str] = mapped_column(String(8), server_default=TaskPriority.NORMAL.value)
    status: Mapped[str] = mapped_column(String(12), server_default=TaskStatus.OPEN.value)
    source: Mapped[str] = mapped_column(String(12), server_default=TaskSource.SELF.value)
    due_at: Mapped[datetime | None]
    # 截止前多少分钟提醒（新建时按租户设置填好，这样调度进程不用再查设置）。
    remind_before_minutes: Mapped[int | None]
    reminded_at: Mapped[datetime | None]
    overdue_notified_at: Mapped[datetime | None]
    # 关联的控制台页面（某条客户待办、订单、单据）。
    link: Mapped[str | None] = mapped_column(Text)
    done_note: Mapped[str | None] = mapped_column(Text)
    done_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None]
