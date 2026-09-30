"""状态类指标：持有调度租约的调度进程每 15 秒从数据库和 Redis 读取一次，整体替换快照。

排队长度与最久等待、在线坐席与接待量、AI 接待中的会话、IM 发件箱积压、事件流积压、死信数量、
授权已失效的企业微信企业，以及租户 ID 与短码的对应（edp_tenant_info）。
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.metrics_core import Metric
from sqlalchemy import func, select

from app.context import AppContext
from app.events.bus import DEAD_LETTER_STREAM
from app.modules.conversation.models import ChatSession, ImOp, ImOpStatus, SessionStatus
from app.modules.routing.models import AgentState, AgentStatus
from app.modules.tenancy.models import Tenant, TenantStatus
from app.modules.wecom.models import CorpStatus, WecomCorp
from app.observability.metrics import publish_state, tenant_label

INTERVAL_SECONDS = 15.0


def _family(name: str, documentation: str, labels: list[str]) -> GaugeMetricFamily:
    return GaugeMetricFamily(name, documentation, labels=labels)


async def collect(ctx: AppContext, *, now: datetime | None = None) -> list[Metric]:
    now = now or datetime.now(UTC)
    tenant_info = _family("edp_tenant_info", "租户 ID 与短码（值恒为 1）", ["tenant", "code"])
    queue_length = _family("edp_queue_length", "排队中的会话", ["tenant"])
    queue_oldest = _family(
        "edp_queue_oldest_wait_seconds", "排队最久的会话已等待的时间", ["tenant"]
    )
    ai_sessions = _family("edp_ai_sessions", "AI 接待中的会话", ["tenant"])
    human_sessions = _family("edp_human_sessions", "人工接待中（含转接中）的会话", ["tenant"])
    agents_online = _family("edp_agents_online", "在线（可接新会话）的坐席", ["tenant"])
    agent_capacity = _family("edp_agent_capacity", "在线坐席的接待上限之和", ["tenant"])
    im_pending = _family("edp_im_ops_pending", "待执行的 IM 发件箱操作", ["tenant"])
    im_oldest = _family(
        "edp_im_ops_oldest_pending_seconds", "已到执行时间、最久仍未执行成功的 IM 操作", []
    )
    wecom_invalid = _family("edp_wecom_auth_invalid", "授权已取消的企业微信企业", ["tenant"])
    backlog = _family(
        "edp_event_backlog", "事件流里还没处理完的事件（未读取与未确认）", ["partition", "tenant"]
    )
    dead_letters = _family("edp_dead_letter_size", "死信流里的事件", [])

    async with ctx.db.platform_sessionmaker() as session:
        tenants = (
            await session.execute(
                select(Tenant.id, Tenant.code).where(Tenant.status != TenantStatus.CLOSED)
            )
        ).all()
        for tenant_id, code in tenants:
            tenant_info.add_metric([str(tenant_id), code], 1)

        queued = (
            await session.execute(
                select(ChatSession.tenant_id, func.count(), func.min(ChatSession.queued_at))
                .where(ChatSession.status == SessionStatus.QUEUED)
                .group_by(ChatSession.tenant_id)
            )
        ).all()
        for tenant_id, count, oldest in queued:
            queue_length.add_metric([tenant_label(tenant_id)], count)
            waited = (now - oldest).total_seconds() if oldest is not None else 0.0
            queue_oldest.add_metric([tenant_label(tenant_id)], max(0.0, waited))

        serving = (
            await session.execute(
                select(ChatSession.tenant_id, ChatSession.status, func.count())
                .where(
                    ChatSession.status.in_(
                        (
                            SessionStatus.AI_SERVING,
                            SessionStatus.HUMAN_SERVING,
                            SessionStatus.TRANSFERRING,
                        )
                    )
                )
                .group_by(ChatSession.tenant_id, ChatSession.status)
            )
        ).all()
        human: dict[uuid.UUID, int] = defaultdict(int)
        for tenant_id, status, count in serving:
            if status == SessionStatus.AI_SERVING:
                ai_sessions.add_metric([tenant_label(tenant_id)], count)
            else:
                human[tenant_id] += count
        for tenant_id, count in human.items():
            human_sessions.add_metric([tenant_label(tenant_id)], count)

        agents = (
            await session.execute(
                select(
                    AgentState.tenant_id,
                    func.count(),
                    func.coalesce(func.sum(AgentState.max_concurrency), 0),
                )
                .where(AgentState.status == AgentStatus.ONLINE)
                .group_by(AgentState.tenant_id)
            )
        ).all()
        for tenant_id, count, capacity in agents:
            agents_online.add_metric([tenant_label(tenant_id)], count)
            agent_capacity.add_metric([tenant_label(tenant_id)], int(capacity))

        pending_ops = (
            await session.execute(
                select(ImOp.tenant_id, func.count())
                .where(ImOp.status == ImOpStatus.PENDING)
                .group_by(ImOp.tenant_id)
            )
        ).all()
        for tenant_id, count in pending_ops:
            im_pending.add_metric([tenant_label(tenant_id)], count)
        oldest_due = await session.scalar(
            select(func.min(ImOp.created_at)).where(
                ImOp.status == ImOpStatus.PENDING, ImOp.next_attempt_at <= now
            )
        )
        im_oldest.add_metric(
            [], max(0.0, (now - oldest_due).total_seconds()) if oldest_due else 0.0
        )

        cancelled = (
            await session.execute(
                select(WecomCorp.tenant_id, func.count())
                .where(WecomCorp.status == CorpStatus.CANCELLED)
                .group_by(WecomCorp.tenant_id)
            )
        ).all()
        for tenant_id, count in cancelled:
            wecom_invalid.add_metric([tenant_label(tenant_id)], count)

    for item in await ctx.bus.backlog():
        backlog.add_metric(
            [str(item.partition), tenant_label(item.tenant_id)], item.lag + item.pending
        )
    dead_letters.add_metric([], int(await ctx.redis.xlen(DEAD_LETTER_STREAM)))

    return [
        tenant_info,
        queue_length,
        queue_oldest,
        ai_sessions,
        human_sessions,
        agents_online,
        agent_capacity,
        im_pending,
        im_oldest,
        wecom_invalid,
        backlog,
        dead_letters,
    ]


async def refresh(ctx: AppContext) -> int:
    """读取并替换快照，返回指标族数量（调度任务）。"""
    families = await collect(ctx)
    publish_state(families)
    return len(families)


def clear() -> None:
    """交出调度租约时清空，避免导出过时的状态。"""
    publish_state(())
