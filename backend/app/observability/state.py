"""状态类指标：持有调度租约的调度进程每 15 秒从数据库和 Redis 读取一次，整体替换快照。

排队长度与最久等待、在线坐席与接待量、AI 接待中的会话、IM 发件箱积压、事件流积压、死信数量、
授权已失效的企业微信企业、消息分区（提前建好的月数、默认分区里的行数）、待办与订单的积压
（待确认、逾期、最久未认领、待审核、逾期应收）、向企业系统推送的失败，以及租户 ID 与短码的
对应（edp_tenant_info）。
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.metrics_core import Metric
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.partitions import partition_status
from app.events.bus import DEAD_LETTER_STREAM
from app.modules.conversation.models import ChatSession, ImOp, ImOpStatus, SessionStatus
from app.modules.integration.models import DeliveryStatus, WebhookDelivery
from app.modules.orders.models import Order, OrderStatus, PaymentMethod
from app.modules.routing.models import AgentState, AgentStatus
from app.modules.tenancy.models import Tenant, TenantStatus
from app.modules.todos.models import ACTIVE, UNFINISHED, Todo, TodoStatus
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
    partitions_ahead = _family("edp_message_partitions_ahead", "本月之后已经建好的消息月份分区", [])
    default_rows = _family(
        "edp_message_default_partition_rows", "落进默认分区的消息（没有对应月份的分区）", []
    )
    todos_pending = _family("edp_todos_pending", "待确认的待办（AI 生成）", ["tenant"])
    todos_overdue = _family("edp_todos_overdue", "已过截止时间仍未完成的待办", ["tenant"])
    todos_unclaimed = _family(
        "edp_todos_unclaimed_oldest_seconds", "待认领的待办里最久的已等待时间", ["tenant"]
    )
    orders_review = _family("edp_orders_pending_review", "待审核的订单", ["tenant"])
    orders_review_oldest = _family(
        "edp_orders_pending_review_oldest_seconds", "待审核的订单里最久的已等待时间", ["tenant"]
    )
    receivable_overdue = _family(
        "edp_orders_receivable_overdue", "暂欠逾期仍未收清的订单", ["tenant"]
    )
    webhooks = _family(
        "edp_webhook_deliveries",
        "向企业系统的推送（state：retrying 失败后等待重试，dead 已停止重试）",
        ["tenant", "state"],
    )

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

        await _todos_and_orders(
            session,
            now,
            todos_pending,
            todos_overdue,
            todos_unclaimed,
            orders_review,
            orders_review_oldest,
            receivable_overdue,
            webhooks,
        )

        partitions = await partition_status(session)
        partitions_ahead.add_metric([], partitions.months_ahead)
        default_rows.add_metric([], partitions.default_rows)

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
        partitions_ahead,
        default_rows,
        todos_pending,
        todos_overdue,
        todos_unclaimed,
        orders_review,
        orders_review_oldest,
        receivable_overdue,
        webhooks,
    ]


async def _todos_and_orders(
    session: AsyncSession,
    now: datetime,
    todos_pending: GaugeMetricFamily,
    todos_overdue: GaugeMetricFamily,
    todos_unclaimed: GaugeMetricFamily,
    orders_review: GaugeMetricFamily,
    orders_review_oldest: GaugeMetricFamily,
    receivable_overdue: GaugeMetricFamily,
    webhooks: GaugeMetricFamily,
) -> None:
    """待确认积压、逾期待办、最久未认领、待审核订单积压、逾期应收、推送失败（设计文档 §19.3）。"""
    rows = await session.execute(
        select(
            Todo.tenant_id,
            func.count().filter(Todo.status == TodoStatus.PENDING),
            func.count().filter(Todo.status.in_(UNFINISHED), Todo.due_at < now),
            func.min(Todo.created_at).filter(Todo.status.in_(ACTIVE), Todo.assignee_id.is_(None)),
        )
        .where(Todo.status.in_(UNFINISHED))
        .group_by(Todo.tenant_id)
    )
    for tenant_id, pending, overdue, oldest in rows:
        label = [tenant_label(tenant_id)]
        todos_pending.add_metric(label, pending)
        todos_overdue.add_metric(label, overdue)
        if oldest is not None:
            todos_unclaimed.add_metric(label, max(0.0, (now - oldest).total_seconds()))
    reviews = await session.execute(
        select(Order.tenant_id, func.count(), func.min(Order.submitted_at))
        .where(Order.status == OrderStatus.PENDING_REVIEW)
        .group_by(Order.tenant_id)
    )
    for tenant_id, count, submitted in reviews:
        orders_review.add_metric([tenant_label(tenant_id)], count)
        waited = (now - submitted).total_seconds() if submitted is not None else 0.0
        orders_review_oldest.add_metric([tenant_label(tenant_id)], max(0.0, waited))
    receivables = await session.execute(
        select(Order.tenant_id, func.count())
        .where(
            Order.payment_method == PaymentMethod.CREDIT.value,
            Order.status.in_(
                (
                    OrderStatus.CONFIRMED,
                    OrderStatus.FULFILLING,
                    OrderStatus.SHIPPED,
                    OrderStatus.COMPLETED,
                )
            ),
            Order.credit_due_date < now.date(),
            Order.total > Order.paid_amount - Order.refunded_amount,
        )
        .group_by(Order.tenant_id)
    )
    for tenant_id, count in receivables:
        receivable_overdue.add_metric([tenant_label(tenant_id)], count)
    deliveries = await session.execute(
        select(
            WebhookDelivery.tenant_id,
            func.count().filter(
                WebhookDelivery.status == DeliveryStatus.PENDING, WebhookDelivery.attempts > 0
            ),
            func.count().filter(WebhookDelivery.status == DeliveryStatus.DEAD),
        )
        .where(
            (WebhookDelivery.status == DeliveryStatus.DEAD)
            | ((WebhookDelivery.status == DeliveryStatus.PENDING) & (WebhookDelivery.attempts > 0))
        )
        .group_by(WebhookDelivery.tenant_id)
    )
    for tenant_id, retrying, dead in deliveries:
        webhooks.add_metric([tenant_label(tenant_id), "retrying"], retrying)
        webhooks.add_metric([tenant_label(tenant_id), "dead"], dead)


async def refresh(ctx: AppContext) -> int:
    """读取并替换快照，返回指标族数量（调度任务）。"""
    families = await collect(ctx)
    publish_state(families)
    return len(families)


def clear() -> None:
    """交出调度租约时清空，避免导出过时的状态。"""
    publish_state(())
