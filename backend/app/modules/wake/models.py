"""AI 唤醒（设计文档 §33）：每次唤醒的记录、巡检发现的问题。"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class RunKind(StrEnum):
    HOURLY = "hourly"  # 每小时检查（工作时间内）
    DAILY = "daily"  # 每日巡检（全部检查项和 AI 简报）
    KB = "kb"  # 知识库整理


RUN_KIND_LABELS: dict[str, str] = {
    RunKind.HOURLY: "每小时检查",
    RunKind.DAILY: "每日巡检",
    RunKind.KB: "知识库整理",
}


class RunTrigger(StrEnum):
    SCHEDULE = "schedule"  # 按时间表
    EVENT = "event"  # 规章制度有变化
    MANUAL = "manual"  # 立即唤醒
    CONTINUE = "continue"  # 知识库整理超出上限，接着上次的继续


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"  # 醒来时已经关闭了唤醒，或者套餐不再包含


class WakeRun(IdMixin, TenantMixin, Base):
    """一次唤醒。定时的同一类型、同一时段（小时、日期、周）只有一条；其他触发的时段是随机串。"""

    __tablename__ = "wake_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "kind", "slot", name="uq_wake_runs_slot"),
    )

    kind: Mapped[str] = mapped_column(String(8))
    trigger: Mapped[str] = mapped_column(String(12))
    slot: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(12), server_default=RunStatus.QUEUED.value)
    not_before: Mapped[datetime] = mapped_column(server_default=func.now())
    lease_until: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(server_default="0")
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    # 每日巡检：检查项、发现、新问题、已消除、通知人数；知识库整理：整理报告（§33.7.2）。
    # 两者都有大模型调用次数和费用。
    stats: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    # 每日巡检的 AI 简报。
    summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Severity(StrEnum):
    INFO = "info"  # 提示
    WARNING = "warning"  # 注意
    CRITICAL = "critical"  # 严重


SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
SEVERITY_LABELS: dict[str, str] = {
    Severity.INFO: "提示",
    Severity.WARNING: "注意",
    Severity.CRITICAL: "严重",
}


class FindingStatus(StrEnum):
    OPEN = "open"  # 待处理
    IGNORED = "ignored"  # 已忽略（到期后仍然存在就重新打开）
    RESOLVED = "resolved"  # 已消除（检查不再发现，或者负责人标了已处理）


class WakeFinding(IdMixin, TimestampMixin, TenantMixin, Base):
    """巡检发现的问题（§33.4）：同一个检查项、同一个对象只有一条（fingerprint）。"""

    __tablename__ = "wake_findings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "fingerprint", name="uq_wake_findings_fingerprint"),
    )

    check_code: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(16))
    severity: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(8), server_default=FindingStatus.OPEN.value)
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(String(16))
    entity_id: Mapped[uuid.UUID | None]
    data: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    assignee_ids: Mapped[list[uuid.UUID]] = mapped_column(server_default="{}")
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    seen_count: Mapped[int] = mapped_column(server_default="1")
    notified_at: Mapped[datetime | None]
    notified_severity: Mapped[str | None] = mapped_column(String(8))
    escalated_at: Mapped[datetime | None]
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[uuid.UUID | None]
    resolve_note: Mapped[str | None] = mapped_column(Text)
    ignored_by: Mapped[uuid.UUID | None]
    ignored_until: Mapped[datetime | None]
    ignore_note: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None]


class WakeCheckState(Base):
    """每个检查项上次检查时涉及的表的变化编号（增量更新索引，§33.9）、口径数字的摘要和下一个可能
    出现新问题的时刻。数据没变、口径没改、时间也没到时，这个检查项跳过。"""

    __tablename__ = "wake_check_state"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), primary_key=True, server_default=text("app_current_tenant()")
    )
    check_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    data_seq: Mapped[int] = mapped_column(BigInteger, server_default="0")
    params: Mapped[str] = mapped_column(String(64), server_default="")
    next_due_at: Mapped[datetime | None]
    checked_at: Mapped[datetime] = mapped_column(server_default=func.now())
