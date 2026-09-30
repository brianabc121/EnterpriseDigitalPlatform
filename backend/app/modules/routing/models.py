import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TenantMixin, TimestampMixin


class SkillGroup(IdMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "skill_groups"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), UniqueConstraint("tenant_id", "name"))

    name: Mapped[str] = mapped_column(String(64))
    # 排队超过 overflow_after_seconds 仍没有分配时，改由备用技能组接待（设计文档 §11.3）。
    overflow_group_id: Mapped[uuid.UUID | None]
    overflow_after_seconds: Mapped[int] = mapped_column(server_default="0")


class SkillGroupMember(TenantMixin, Base):
    """技能组成员。组长（is_lead）可以查看和转接本组会话、查看组员的客户。"""

    __tablename__ = "skill_group_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "skill_group_id"], ["skill_groups.tenant_id", "skill_groups.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "staff_id"], ["staff.tenant_id", "staff.id"]),
    )

    skill_group_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    staff_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    is_lead: Mapped[bool] = mapped_column(server_default="false")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RoutingMode(StrEnum):
    AI_FIRST = "ai_first"
    HUMAN_FIRST = "human_first"


class RoutingPolicy(IdMixin, TimestampMixin, TenantMixin, Base):
    """路由策略。渠道账号引用一套策略；没有引用时用租户的默认策略。"""

    __tablename__ = "routing_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "default_skill_group_id"],
            ["skill_groups.tenant_id", "skill_groups.id"],
        ),
    )

    name: Mapped[str] = mapped_column(String(64))
    is_default: Mapped[bool] = mapped_column(server_default="false")
    mode: Mapped[str] = mapped_column(String(16), server_default=RoutingMode.HUMAN_FIRST.value)
    default_skill_group_id: Mapped[uuid.UUID | None]
    # 客户有归属坐席且该坐席可接待时，直接分配给归属坐席。
    owner_first: Mapped[bool] = mapped_column(server_default="true")
    # 排队超过这个时间转为留言。
    max_wait_seconds: Mapped[int] = mapped_column(server_default="300")
    # 人工接待中的会话超过这个时间没有新消息，自动结束。
    idle_close_minutes: Mapped[int] = mapped_column(server_default="30")
    # 会话结束后这么多分钟内客户再来咨询，优先分配给上次接待的坐席；0 表示关闭。
    resume_window_minutes: Mapped[int] = mapped_column(server_default="10")
    # 为空表示全天服务；否则形如 {"tz": "Asia/Shanghai", "days": {"1": [["09:00", "18:00"]]}}。
    business_hours: Mapped[dict[str, Any] | None]
    # 排队优先级：带这些标签的客户（VIP）排在最前；urgent_first 时投诉、情绪激动的客户次之。
    priority_tags: Mapped[list[str]] = mapped_column(server_default="{VIP}")
    urgent_first: Mapped[bool] = mapped_column(server_default="true")
    # 排队期间 AI 继续回答客户的其他问题（需要启用 AI 接待）。
    ai_while_queued: Mapped[bool] = mapped_column(server_default="false")
    # 按意图分配：[{"intent": "售后", "keywords": ["退货"], "skill_group_id": "..."}]。
    intent_routes: Mapped[list[Any]] = mapped_column(JSONB, server_default="[]")


class AgentStatus(StrEnum):
    ONLINE = "online"
    BUSY = "busy"  # 不接新会话
    AWAY = "away"  # 小休，不接新会话
    OFFLINE = "offline"


class AgentState(TenantMixin, Base):
    __tablename__ = "agent_states"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "staff_id"], ["staff.tenant_id", "staff.id"]),
    )

    staff_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(16), server_default=AgentStatus.OFFLINE.value)
    max_concurrency: Mapped[int] = mapped_column(server_default="5")
    status_changed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime | None]
    last_assigned_at: Mapped[datetime | None]
    # 员工的 IM 用户已注册，并与租户系统用户互为好友（能收到信令）。
    im_ready_at: Mapped[datetime | None]
