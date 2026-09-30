"""分配规则（设计文档 §11.3）：专属坐席优先 → 技能组 → 组内负载最低、空闲最久。"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.channels.models import ChannelAccount
from app.modules.conversation.models import ChatSession, SessionStatus
from app.modules.iam.models import Staff, StaffStatus
from app.modules.routing.models import (
    AgentState,
    AgentStatus,
    RoutingMode,
    RoutingPolicy,
    SkillGroupMember,
)

# 坐席工作台每 30 秒发一次心跳；超过这个时间没有心跳视为断线。
HEARTBEAT_TTL = timedelta(seconds=90)
# 占用坐席并发名额的会话状态。
LOAD_STATUSES = (SessionStatus.HUMAN_SERVING, SessionStatus.TRANSFERRING)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass
class AgentSlot:
    staff_id: uuid.UUID
    display_name: str
    max_concurrency: int
    load: int
    last_assigned_at: datetime | None
    groups: set[uuid.UUID] = field(default_factory=set)

    @property
    def has_capacity(self) -> bool:
        return self.load < self.max_concurrency


def pick_agent(
    agents: dict[uuid.UUID, AgentSlot],
    *,
    owner_id: uuid.UUID | None,
    skill_group_id: uuid.UUID | None,
    previous_id: uuid.UUID | None = None,
) -> tuple[AgentSlot, str] | None:
    """返回 (坐席, 分配依据)；没有可接待的坐席时返回 None。

    分配依据：previous（续接：上次接待的坐席）、owner（归属坐席）、group（技能组）、
    any（未指定技能组，在全部坐席中分配）。
    """
    for preferred, via in ((previous_id, "previous"), (owner_id, "owner")):
        if preferred is None:
            continue
        agent = agents.get(preferred)
        if agent is not None and agent.has_capacity:
            return agent, via
    candidates = [
        a
        for a in agents.values()
        if a.has_capacity and (skill_group_id is None or skill_group_id in a.groups)
    ]
    if not candidates:
        return None
    best = min(
        candidates,
        key=lambda a: (a.load / a.max_concurrency, a.last_assigned_at or _EPOCH, a.staff_id),
    )
    return best, "group" if skill_group_id is not None else "any"


async def available_agents(session: AsyncSession, now: datetime) -> dict[uuid.UUID, AgentSlot]:
    """在线、心跳正常、账号启用的坐席及其当前负载和技能组。"""
    load = (
        select(func.count())
        .select_from(ChatSession)
        .where(
            ChatSession.assignee_id == AgentState.staff_id,
            ChatSession.status.in_(LOAD_STATUSES),
        )
        .correlate(AgentState)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(
            AgentState.staff_id,
            Staff.display_name,
            AgentState.max_concurrency,
            load,
            AgentState.last_assigned_at,
        )
        .join(Staff, and_(Staff.tenant_id == AgentState.tenant_id, Staff.id == AgentState.staff_id))
        .where(
            AgentState.status == AgentStatus.ONLINE,
            AgentState.last_seen_at >= now - HEARTBEAT_TTL,
            Staff.status == StaffStatus.ACTIVE,
        )
    )
    agents = {
        staff_id: AgentSlot(staff_id, name, max_concurrency, count, last_assigned_at)
        for staff_id, name, max_concurrency, count, last_assigned_at in rows
    }
    if agents:
        memberships = await session.execute(
            select(SkillGroupMember.staff_id, SkillGroupMember.skill_group_id).where(
                SkillGroupMember.staff_id.in_(list(agents))
            )
        )
        for staff_id, group_id in memberships:
            agents[staff_id].groups.add(group_id)
    return agents


def fallback_policy() -> RoutingPolicy:
    """租户没有任何路由策略时使用的默认值（与建表默认值一致，不写入数据库）。"""
    return RoutingPolicy(
        name="默认策略",
        is_default=True,
        mode=RoutingMode.HUMAN_FIRST,
        default_skill_group_id=None,
        owner_first=True,
        max_wait_seconds=300,
        idle_close_minutes=30,
        resume_window_minutes=10,
        business_hours=None,
    )


class PolicyResolver:
    """按渠道账号查找路由策略：渠道绑定的策略，否则租户默认策略。结果在对象内缓存。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._by_channel: dict[uuid.UUID, RoutingPolicy] = {}
        self._default: RoutingPolicy | None = None

    async def for_channel(self, channel_account_id: uuid.UUID) -> RoutingPolicy:
        cached = self._by_channel.get(channel_account_id)
        if cached is not None:
            return cached
        policy = await self._session.scalar(
            select(RoutingPolicy)
            .join(ChannelAccount, ChannelAccount.routing_policy_id == RoutingPolicy.id)
            .where(ChannelAccount.id == channel_account_id)
        )
        if policy is None:
            policy = await self.default()
        self._by_channel[channel_account_id] = policy
        return policy

    async def default(self) -> RoutingPolicy:
        if self._default is None:
            self._default = (
                await self._session.scalar(select(RoutingPolicy).where(RoutingPolicy.is_default))
                or fallback_policy()
            )
        return self._default
