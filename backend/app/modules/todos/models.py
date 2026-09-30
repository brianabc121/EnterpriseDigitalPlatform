"""待办（设计文档 §24）：待办类型、待办、待办动态、会话后解析记录。"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Double, ForeignKeyConstraint, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class TodoStatus(StrEnum):
    PENDING = "pending"  # 待确认：AI 生成，人工确认后才进入待办列表
    OPEN = "open"  # 待处理
    IN_PROGRESS = "in_progress"  # 处理中
    WAITING = "waiting"  # 等待客户（暂停计时）
    DONE = "done"
    CANCELLED = "cancelled"
    REJECTED = "rejected"  # 驳回（只有待确认的可以驳回）


# 待办列表里未完成的状态；再加上待确认就是全部未结束的待办。
ACTIVE = (TodoStatus.OPEN, TodoStatus.IN_PROGRESS, TodoStatus.WAITING)
UNFINISHED = (TodoStatus.PENDING, *ACTIVE)
# 计时中的状态（等待客户时暂停）。
TIMED = (TodoStatus.OPEN, TodoStatus.IN_PROGRESS)
STATUS_LABELS: dict[str, str] = {
    TodoStatus.PENDING: "待确认",
    TodoStatus.OPEN: "待处理",
    TodoStatus.IN_PROGRESS: "处理中",
    TodoStatus.WAITING: "等待客户",
    TodoStatus.DONE: "已完成",
    TodoStatus.CANCELLED: "已取消",
    TodoStatus.REJECTED: "已驳回",
}


class TodoSource(StrEnum):
    AI_CHAT = "ai_chat"  # AI 接待中调用 create_todo
    AI_SUMMARY = "ai_summary"  # 会话结束后解析
    ZONE = "zone"  # 数据与智能专区的待办候选
    COPILOT = "copilot"  # 坐席在工作台由 AI 预填后保存
    SIDEBAR = "sidebar"  # 员工在企业微信侧边栏保存
    STAFF = "staff"  # 员工手工新建
    VISITOR = "visitor"  # 访客自己提交的留言
    RULE = "rule"  # 系统规则：非工作时间、排队超时转留言，订单审核与催收
    API = "api"  # 企业系统通过开放接口创建


# AI 生成的待办先进入待确认页（2026-09-30 确认）；其余直接进入待办列表。
AI_SOURCES = frozenset({TodoSource.AI_CHAT, TodoSource.AI_SUMMARY, TodoSource.ZONE})
SOURCE_LABELS: dict[str, str] = {
    TodoSource.AI_CHAT: "AI 接待",
    TodoSource.AI_SUMMARY: "会话后解析",
    TodoSource.ZONE: "专区",
    TodoSource.COPILOT: "工作台",
    TodoSource.SIDEBAR: "侧边栏",
    TodoSource.STAFF: "员工新建",
    TodoSource.VISITOR: "访客留言",
    TodoSource.RULE: "系统规则",
    TodoSource.API: "企业系统",
}


class Priority(StrEnum):
    URGENT = "urgent"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


PRIORITY_ORDER = (Priority.LOW, Priority.NORMAL, Priority.HIGH, Priority.URGENT)
PRIORITY_LABELS: dict[str, str] = {
    Priority.URGENT: "紧急",
    Priority.HIGH: "高",
    Priority.NORMAL: "普通",
    Priority.LOW: "低",
}


def raise_priority(priority: str) -> str:
    """提高一级（最高为紧急）。"""
    index = PRIORITY_ORDER.index(Priority(priority))
    return PRIORITY_ORDER[min(index + 1, len(PRIORITY_ORDER) - 1)].value


class RejectReason(StrEnum):
    NOT_REAL = "not_real"
    DUPLICATE = "duplicate"
    WRONG_INFO = "wrong_info"
    OTHER = "other"


REJECT_LABELS: dict[str, str] = {
    RejectReason.NOT_REAL: "不是真实需求",
    RejectReason.DUPLICATE: "重复",
    RejectReason.WRONG_INFO: "信息有误",
    RejectReason.OTHER: "其他",
}


class ActorType(StrEnum):
    AI = "ai"
    STAFF = "staff"
    SYSTEM = "system"
    API = "api"
    VISITOR = "visitor"


class NotifyReason(StrEnum):
    """待发送的提醒（调度进程发送站内信和企业微信应用消息）。"""

    PENDING = "pending"  # 新的待确认，提醒确认人
    ASSIGNED = "assigned"  # 进入待办列表并分派，提醒处理人
    NUDGED = "nudged"  # 客户催促
    REOPENED = "reopened"  # 客户再次提出，重新打开


class FieldType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    OPTION = "option"
    PHONE = "phone"
    EMAIL = "email"
    ADDRESS = "address"
    FILE = "file"


class TodoType(IdMixin, TimestampMixin, TenantMixin, Base):
    """待办类型：字段、给 AI 的说明、分派规则、时限、提醒与升级、AI 登记开关和话术。

    fields：[{key, label, type, required, sensitive, options}]；
    assign_rule：{steps: [session_agent | owner | channel_group | skill_group | staff],
    skill_group_id, staff_id, group_mode: least_loaded | pool}。
    预置类型（preset）不能删除，只能停用；系统类型（system）由系统规则生成，不能开启 AI 登记。
    """

    __tablename__ = "todo_types"

    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(32))
    ai_hint: Mapped[str] = mapped_column(Text, server_default="")
    examples: Mapped[list[str]] = mapped_column(server_default="{}")
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    assign_rule: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    priority: Mapped[str] = mapped_column(String(8), server_default=Priority.NORMAL.value)
    sla_response_minutes: Mapped[int | None]
    sla_resolve_minutes: Mapped[int | None]
    sla_resolve_days: Mapped[int | None]
    remind_before_minutes: Mapped[int] = mapped_column(server_default="120")
    escalate_after_minutes: Mapped[int] = mapped_column(server_default="240")
    ai_enabled: Mapped[bool] = mapped_column(server_default="true")
    handoff: Mapped[bool] = mapped_column(server_default="false")
    notify_supervisor: Mapped[bool] = mapped_column(server_default="false")
    promise_text: Mapped[str] = mapped_column(Text, server_default="")
    done_template: Mapped[str] = mapped_column(Text, server_default="")
    preset: Mapped[bool] = mapped_column(server_default="false")
    system: Mapped[bool] = mapped_column(server_default="false")
    enabled: Mapped[bool] = mapped_column(server_default="true")
    sort: Mapped[int] = mapped_column(server_default="0")


class Todo(IdMixin, TimestampMixin, TenantMixin, Base):
    """待办。待确认时 assignee_id 是确认人；分到技能组待认领池时 assignee_id 为空。"""

    __tablename__ = "todos"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "type_id"], ["todo_types.tenant_id", "todo_types.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "customer_id"],
            ["customers.tenant_id", "customers.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"]),
        ForeignKeyConstraint(["tenant_id", "assignee_id"], ["staff.tenant_id", "staff.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "skill_group_id"], ["skill_groups.tenant_id", "skill_groups.id"]
        ),
    )

    no: Mapped[str] = mapped_column(String(24))
    type_id: Mapped[uuid.UUID]
    title: Mapped[str] = mapped_column(String(100))
    detail: Mapped[str] = mapped_column(Text, server_default="")
    # 字段的值；敏感字段保存为 {"enc": 密文, "masked": 掩码}。
    fields: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    customer_id: Mapped[uuid.UUID | None]
    session_id: Mapped[uuid.UUID | None]
    order_id: Mapped[uuid.UUID | None]
    source: Mapped[str] = mapped_column(String(12))
    confidence: Mapped[float | None] = mapped_column(Double)
    evidence_message_ids: Mapped[list[uuid.UUID]] = mapped_column(server_default="{}")
    priority: Mapped[str] = mapped_column(String(8), server_default=Priority.NORMAL.value)
    status: Mapped[str] = mapped_column(String(12), server_default=TodoStatus.OPEN.value)
    assignee_id: Mapped[uuid.UUID | None]
    skill_group_id: Mapped[uuid.UUID | None]
    # 最近一次把待办分派或转交给别人的员工（"我分派的"）。
    assigned_by: Mapped[uuid.UUID | None]
    expected_at: Mapped[datetime | None]
    due_at: Mapped[datetime | None]
    respond_due_at: Mapped[datetime | None]
    first_response_at: Mapped[datetime | None]
    confirmed_at: Mapped[datetime | None]
    confirmed_by: Mapped[uuid.UUID | None]
    closed_at: Mapped[datetime | None]
    paused_at: Mapped[datetime | None]
    result: Mapped[str | None] = mapped_column(Text)
    reject_reason: Mapped[str | None] = mapped_column(String(12))
    close_note: Mapped[str | None] = mapped_column(Text)
    progress_note: Mapped[str | None] = mapped_column(Text)
    nudge_count: Mapped[int] = mapped_column(server_default="0")
    dedupe_key: Mapped[str | None] = mapped_column(String(64))
    # 企业系统通过开放接口创建时它自己的单号（推送"待办完成"时带回）。
    external_ref: Mapped[str | None] = mapped_column(String(64))
    created_by_type: Mapped[str] = mapped_column(String(8))
    created_by: Mapped[uuid.UUID | None]
    notify_reason: Mapped[str | None] = mapped_column(String(12))
    notified_at: Mapped[datetime | None]
    pending_remind_at: Mapped[datetime | None]
    remind_at: Mapped[datetime | None]
    overdue_notified_at: Mapped[datetime | None]
    escalate_at: Mapped[datetime | None]
    escalated_at: Mapped[datetime | None]


class TodoEvent(IdMixin, TenantMixin, Base):
    """待办动态（只追加）。"""

    __tablename__ = "todo_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "todo_id"], ["todos.tenant_id", "todos.id"], ondelete="CASCADE"
        ),
    )

    todo_id: Mapped[uuid.UUID]
    type: Mapped[str] = mapped_column(String(24))
    actor_type: Mapped[str] = mapped_column(String(8))
    actor_id: Mapped[uuid.UUID | None]
    payload: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TodoExtraction(TenantMixin, Base):
    """会话后解析的记录：每个会话解析一次。"""

    __tablename__ = "todo_extractions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "session_id"], ["sessions.tenant_id", "sessions.id"], ondelete="CASCADE"
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(8))
    created: Mapped[int] = mapped_column(server_default="0")
    skipped: Mapped[int] = mapped_column(server_default="0")
    extracted_at: Mapped[datetime] = mapped_column(server_default=func.now())
