"""商机（设计文档 §40）：阶段、商机记录和时间线。

意向客户（§35）升级为商机：原来的意向记录表（customer_prospects）改名为 opportunities，加了名称、
阶段、预计金额、预计成交日等字段；跟进记录表改名为 opportunity_activities，成为时间线——员工记的跟进
和系统记的事件（会话、订单、合同、收款、待办）排在一起。
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class OpportunityStatus(StrEnum):
    SUGGESTED = "suggested"  # AI 建议，员工确认后才进名单（阶段是第一个进行中的阶段）
    ACTIVE = "active"  # 跟进中（进行中的阶段）
    WON = "won"  # 赢单
    LOST = "lost"  # 输单
    DISMISSED = "dismissed"  # 忽略了 AI 的建议（不在列表里显示，30 天内 AI 不再建议）


OPEN_STATUSES = (OpportunityStatus.SUGGESTED, OpportunityStatus.ACTIVE)


class OpportunityLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class OpportunitySource(StrEnum):
    AI = "ai"
    STAFF = "staff"


class FollowMethod(StrEnum):
    PHONE = "phone"
    WECHAT = "wechat"
    CHAT = "chat"  # 在线会话（客户又来咨询时系统记的也是这个）
    VISIT = "visit"
    OTHER = "other"


class StageKind(StrEnum):
    OPEN = "open"  # 进行中
    WON = "won"  # 赢单
    LOST = "lost"  # 输单


class ActivityKind(StrEnum):
    """时间线上一条动态的种类（设计文档 §40.7）。"""

    FOLLOWUP = "followup"  # 员工记的跟进
    NOTE = "note"  # 备注
    CREATED = "created"  # 转入（员工、AI）、再开一个商机
    STAGE = "stage"  # 换阶段（包括赢单、输单、重新跟进）
    OWNER = "owner"  # 换负责人
    FIELD = "field"  # 改预计金额、预计成交日
    SESSION = "session"  # 客户又来咨询
    TODO = "todo"  # 关联的待办
    ORDER = "order"  # 订单
    CONTRACT = "contract"  # 合同
    PAYMENT = "payment"  # 收款
    AI = "ai"  # AI 整理、AI 小结


STATUS_LABELS: dict[str, str] = {
    OpportunityStatus.SUGGESTED: "待确认",
    OpportunityStatus.ACTIVE: "跟进中",
    OpportunityStatus.WON: "赢单",
    OpportunityStatus.LOST: "输单",
    OpportunityStatus.DISMISSED: "已忽略",
}
LEVEL_LABELS: dict[str, str] = {
    OpportunityLevel.HIGH: "高",
    OpportunityLevel.MEDIUM: "中",
    OpportunityLevel.LOW: "低",
}
METHOD_LABELS: dict[str, str] = {
    FollowMethod.PHONE: "电话",
    FollowMethod.WECHAT: "微信",
    FollowMethod.CHAT: "在线会话",
    FollowMethod.VISIT: "上门",
    FollowMethod.OTHER: "其他",
}
KIND_LABELS: dict[str, str] = {
    ActivityKind.FOLLOWUP: "跟进",
    ActivityKind.NOTE: "备注",
    ActivityKind.CREATED: "转入",
    ActivityKind.STAGE: "阶段",
    ActivityKind.OWNER: "负责人",
    ActivityKind.FIELD: "修改",
    ActivityKind.SESSION: "咨询",
    ActivityKind.TODO: "待办",
    ActivityKind.ORDER: "订单",
    ActivityKind.CONTRACT: "合同",
    ActivityKind.PAYMENT: "收款",
    ActivityKind.AI: "AI",
}


@dataclass(frozen=True)
class StageSpec:
    code: str
    name: str
    kind: StageKind
    probability: int
    stale_days: int | None
    color: str


# 开通企业和迁移时写入的默认阶段（设计文档 §40.4）。代码是稳定的标识，设置里的自动推进按代码指向
# 阶段；企业改名、增删阶段时代码不变，新增的阶段用随机代码。
DEFAULT_STAGES: tuple[StageSpec, ...] = (
    StageSpec("new", "新线索", StageKind.OPEN, 10, 3, "#909399"),
    StageSpec("contacted", "已沟通", StageKind.OPEN, 30, 7, "#409eff"),
    StageSpec("quoted", "已报价", StageKind.OPEN, 60, 7, "#e6a23c"),
    StageSpec("negotiating", "谈判中", StageKind.OPEN, 80, 7, "#f56c6c"),
    StageSpec("won", "赢单", StageKind.WON, 100, None, "#67c23a"),
    StageSpec("lost", "输单", StageKind.LOST, 0, None, "#c0c4cc"),
)
# 默认的输单原因分类（商机设置里可以改）。
DEFAULT_LOST_REASONS: tuple[tuple[str, str], ...] = (
    ("price", "价格"),
    ("competitor", "竞品"),
    ("no_need", "需求消失"),
    ("no_response", "没有回应"),
    ("other", "其他"),
)


class PipelineStage(IdMixin, TimestampMixin, TenantMixin, Base):
    """租户的商机阶段：按 position 排列；赢单、输单各一个，不能删。"""

    __tablename__ = "pipeline_stages"

    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(32))
    position: Mapped[int] = mapped_column(server_default="0")
    kind: Mapped[str] = mapped_column(String(8), server_default=StageKind.OPEN.value)
    # 成交概率（%），报表的加权金额用它；商机可以单独覆盖。
    probability: Mapped[int] = mapped_column(server_default="0")
    # 在这个阶段待了多少天没有动态算停滞；为空时不算。
    stale_days: Mapped[int | None]
    color: Mapped[str | None] = mapped_column(String(16))


class Opportunity(IdMixin, TimestampMixin, TenantMixin, Base):
    """商机：一个客户同时最多一条待确认或跟进中的（部分唯一索引）。"""

    __tablename__ = "opportunities"

    customer_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(String(128), server_default="")
    stage_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(String(12), server_default=OpportunityStatus.ACTIVE.value)
    level: Mapped[str] = mapped_column(String(8), server_default=OpportunityLevel.MEDIUM.value)
    interest: Mapped[str | None] = mapped_column(Text)
    concerns: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(8), server_default=OpportunitySource.STAFF.value)
    # AI 转入时依据的会话。
    session_id: Mapped[uuid.UUID | None]
    # 负责人（原来的跟进人）。
    owner_id: Mapped[uuid.UUID | None]
    next_follow_at: Mapped[date | None]
    last_followed_at: Mapped[datetime | None]
    follow_count: Mapped[int] = mapped_column(server_default="0")
    # 预计金额（人填的估计，订单确认时没填的取订单合计）和预计成交日。
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    expected_close_at: Mapped[date | None]
    # 成交概率（%）：为空时用阶段的。
    probability: Mapped[int | None]
    stage_entered_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_activity_at: Mapped[datetime | None]
    # 关联商品：[{"product_id": "...", "name": "...", "quantity": 1}]。
    products: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")
    # 看板里同一列的先后。
    position: Mapped[int] = mapped_column(server_default="0")
    # 成交的订单、合同。
    order_id: Mapped[uuid.UUID | None]
    contract_id: Mapped[uuid.UUID | None]
    lost_reason_code: Mapped[str | None] = mapped_column(String(16))
    lost_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None]
    closed_by: Mapped[uuid.UUID | None]
    closed_at: Mapped[datetime | None]
    # 开始跟进（转入、重新跟进）的时间：之后确认的订单算成交，之后的会话算"又来咨询"。
    opened_at: Mapped[datetime] = mapped_column(server_default=func.now())


class OpportunityActivity(IdMixin, TenantMixin, Base):
    """时间线：员工记的跟进、备注和系统记的事件。staff_id 为空的是系统记的；同一会话的"又来咨询"
    只记一次。"""

    __tablename__ = "opportunity_activities"

    opportunity_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(String(24), server_default=ActivityKind.FOLLOWUP.value)
    title: Mapped[str | None] = mapped_column(String(200))
    method: Mapped[str] = mapped_column(String(12), server_default=FollowMethod.OTHER.value)
    content: Mapped[str | None] = mapped_column(Text)
    next_follow_at: Mapped[date | None]
    # 事件的细节：前后的阶段、金额、关联对象的编号等。
    properties: Mapped[dict[str, Any]] = mapped_column(server_default="{}")
    linked_type: Mapped[str | None] = mapped_column(String(24))
    linked_id: Mapped[uuid.UUID | None]
    staff_id: Mapped[uuid.UUID | None]
    session_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
