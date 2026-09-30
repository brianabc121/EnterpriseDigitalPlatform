"""用量计量（设计文档 §7.3）：按日汇总每个租户的用量，供租户管理员和平台运营查看。

调度进程每 10 分钟重算当天和前一天（结果幂等覆盖，迟到的消息也会计入）；历史日期可以用
`python -m app.cli usage-rollup --day YYYY-MM-DD` 补算。日期按 EDP_USAGE_TIMEZONE 划分。
"""

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Numeric, and_, case, delete, func, select, union
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.dates import day_bounds, today
from app.db.session import Database
from app.modules.ai.models import AiDecision, DecisionAction, LlmCall
from app.modules.channels.models import ChannelAccount, ChannelStatus
from app.modules.conversation.models import ChatSession, Message, SenderType, SessionEvent
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff, StaffStatus
from app.modules.orders.models import Order
from app.modules.tenancy.models import Tenant
from app.modules.todos.models import AI_SOURCES, Todo
from app.modules.usage.models import (
    MAX_METRICS,
    METRIC_LABELS,
    SNAPSHOT_METRICS,
    Metric,
    UsageDaily,
)
from app.modules.usage.schemas import (
    TenantUsageList,
    TenantUsageOut,
    UsageDayOut,
    UsageMetricOut,
    UsageReport,
)

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 30
_FILE_TYPES = ("image", "file")


@dataclass
class RollupReport:
    days: int = 0
    tenants: int = 0
    errors: int = 0


# ---- 汇总 ----


async def compute(
    session: AsyncSession, tenant_id: uuid.UUID, day: date, tz: ZoneInfo, *, snapshots: bool
) -> dict[Metric, int]:
    """计算一个租户一天的用量。snapshots 为 true 时同时取当前的员工数和渠道数。"""
    start, end = day_bounds(day, tz)
    values: dict[Metric, int] = {}

    size = Message.content["size"]
    file_bytes = case(
        (
            and_(Message.content_type.in_(_FILE_TYPES), func.jsonb_typeof(size) == "number"),
            size.astext.cast(Numeric),
        ),
        else_=0,
    )
    in_day = and_(Message.tenant_id == tenant_id, Message.sent_at >= start, Message.sent_at < end)
    rows = await session.execute(
        select(Message.sender_type, func.count(), func.coalesce(func.sum(file_bytes), 0))
        .where(in_day)
        .group_by(Message.sender_type)
    )
    by_sender = {SenderType.CUSTOMER: Metric.MESSAGES_IN, SenderType.AGENT: Metric.AGENT_MESSAGES}
    by_sender[SenderType.BOT] = Metric.BOT_MESSAGES
    total_bytes = Decimal(0)
    for sender_type, messages, nbytes in rows:
        metric = by_sender.get(sender_type)
        if metric is not None:
            values[metric] = messages
        total_bytes += nbytes
    values[Metric.FILE_BYTES] = int(total_bytes)

    async def count(statement: Any) -> int:
        return int(await session.scalar(statement) or 0)

    def created(column: Any, tenant_column: Any) -> Any:
        return select(func.count()).where(tenant_column == tenant_id, column >= start, column < end)

    values[Metric.SESSIONS] = await count(created(ChatSession.created_at, ChatSession.tenant_id))
    values[Metric.HUMAN_SESSIONS] = await count(
        created(ChatSession.assigned_at, ChatSession.tenant_id)
    )
    values[Metric.NEW_CUSTOMERS] = await count(created(Customer.created_at, Customer.tenant_id))
    agents = union(
        select(Message.sender_id).where(
            in_day, Message.sender_type == SenderType.AGENT, Message.sender_id.is_not(None)
        ),
        select(ChatSession.assignee_id).where(
            ChatSession.tenant_id == tenant_id,
            ChatSession.assigned_at >= start,
            ChatSession.assigned_at < end,
            ChatSession.assignee_id.is_not(None),
        ),
    ).subquery()
    values[Metric.ACTIVE_AGENTS] = await count(select(func.count()).select_from(agents))

    values[Metric.AI_SESSIONS] = await count(
        created(SessionEvent.created_at, SessionEvent.tenant_id).where(
            SessionEvent.type == "ai_serving"
        )
    )
    values[Metric.AI_HANDOFFS] = await count(
        created(AiDecision.created_at, AiDecision.tenant_id).where(
            AiDecision.action == DecisionAction.HANDOFF
        )
    )
    in_calls = (
        LlmCall.tenant_id == tenant_id,
        LlmCall.created_at >= start,
        LlmCall.created_at < end,
    )
    values[Metric.LLM_TOKENS] = await count(
        select(func.coalesce(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens), 0)).where(
            *in_calls
        )
    )
    values[Metric.LLM_COST] = await count(
        select(func.round(func.coalesce(func.sum(LlmCall.cost), 0))).where(*in_calls)
    )

    # AI 生成的待办和 AI 提交的订单（设计文档 §7.3、§24.8、§25.7）。
    values[Metric.AI_TODOS] = await count(
        created(Todo.created_at, Todo.tenant_id).where(Todo.source.in_(AI_SOURCES))
    )
    values[Metric.AI_ORDERS] = await count(
        created(Order.submitted_at, Order.tenant_id).where(Order.created_by_type == "ai")
    )

    if snapshots:
        values[Metric.SEATS] = await count(
            select(func.count()).where(
                Staff.tenant_id == tenant_id, Staff.status == StaffStatus.ACTIVE
            )
        )
        values[Metric.CHANNELS] = await count(
            select(func.count()).where(
                ChannelAccount.tenant_id == tenant_id,
                ChannelAccount.status == ChannelStatus.ACTIVE,
            )
        )
    return values


async def store(
    session: AsyncSession, tenant_id: uuid.UUID, day: date, values: dict[Metric, int]
) -> None:
    """覆盖写入一天的用量；为 0 的指标删除（没有记录即为 0）。"""
    nonzero = {metric: value for metric, value in values.items() if value}
    if nonzero:
        statement = insert(UsageDaily).values(
            [
                {"tenant_id": tenant_id, "day": day, "metric": metric.value, "value": value}
                for metric, value in nonzero.items()
            ]
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[UsageDaily.tenant_id, UsageDaily.day, UsageDaily.metric],
                set_={"value": statement.excluded.value, "updated_at": func.now()},
            )
        )
    zero = [metric.value for metric, value in values.items() if not value]
    if zero:
        await session.execute(
            delete(UsageDaily).where(
                UsageDaily.tenant_id == tenant_id,
                UsageDaily.day == day,
                UsageDaily.metric.in_(zero),
            )
        )


async def rollup_day(
    db: Database, day: date, tz: ZoneInfo, *, now: datetime | None = None
) -> RollupReport:
    """汇总所有租户某一天的用量。快照类指标只在汇总当天时更新。"""
    report = RollupReport(days=1)
    _, end = day_bounds(day, tz)
    snapshots = day == today(tz, now)
    async with db.platform_sessionmaker() as session:
        tenant_ids = (await session.scalars(select(Tenant.id).where(Tenant.created_at < end))).all()
    for tenant_id in tenant_ids:
        report.tenants += 1
        try:
            async with db.platform_sessionmaker() as session:
                values = await compute(session, tenant_id, day, tz, snapshots=snapshots)
                await store(session, tenant_id, day, values)
                await session.commit()
        except Exception:
            report.errors += 1
            logger.exception("usage rollup failed for tenant %s on %s", tenant_id, day)
    return report


async def run_usage_rollup(ctx: AppContext, *, now: datetime | None = None) -> RollupReport:
    """调度任务：重算当天和前一天（零点前后的消息可能晚到）。"""
    tz = ZoneInfo(ctx.settings.usage_timezone)
    current = today(tz, now)
    total = RollupReport()
    for day in (current - timedelta(days=1), current):
        report = await rollup_day(ctx.db, day, tz, now=now)
        total.days += report.days
        total.tenants += report.tenants
        total.errors += report.errors
    return total


# ---- 查询 ----


def _metrics() -> list[UsageMetricOut]:
    def kind(metric: Metric) -> str:
        if metric in SNAPSHOT_METRICS:
            return "snapshot"
        return "max" if metric in MAX_METRICS else "sum"

    return [
        UsageMetricOut(key=m.value, label=label, unit=unit, kind=kind(m))
        for m, (label, unit) in METRIC_LABELS.items()
    ]


def _totals(rows: list[tuple[date, str, int]]) -> dict[str, int]:
    """累计类求和，活跃坐席取最大值，快照类取范围内最后一天的值。"""
    totals: dict[str, int] = {m.value: 0 for m in Metric}
    latest: dict[str, date] = {}
    for day, metric, value in rows:
        if metric in SNAPSHOT_METRICS:
            if metric not in latest or day > latest[metric]:
                latest[metric] = day
                totals[metric] = value
        elif metric in MAX_METRICS:
            totals[metric] = max(totals.get(metric, 0), value)
        elif metric in totals:
            totals[metric] += value
    return totals


async def usage_report(
    session: AsyncSession, tenant_id: uuid.UUID, start: date, end: date, tz: ZoneInfo
) -> UsageReport:
    rows = (
        await session.execute(
            select(UsageDaily.day, UsageDaily.metric, UsageDaily.value, UsageDaily.updated_at)
            .where(UsageDaily.tenant_id == tenant_id, UsageDaily.day.between(start, end))
            .order_by(UsageDaily.day)
        )
    ).all()
    by_day: dict[date, dict[str, int]] = defaultdict(dict)
    for day, metric, value, _ in rows:
        by_day[day][metric] = value
    days = [
        UsageDayOut(day=start + timedelta(days=i), values=by_day.get(start + timedelta(days=i), {}))
        for i in range((end - start).days + 1)
    ]
    return UsageReport(
        start=start,
        end=end,
        timezone=tz.key,
        metrics=_metrics(),
        days=days,
        totals=_totals([(day, metric, value) for day, metric, value, _ in rows]),
        updated_at=max((updated for *_, updated in rows), default=None),
    )


async def tenants_usage(
    session: AsyncSession, start: date, end: date, tz: ZoneInfo
) -> TenantUsageList:
    """平台运营：各租户在范围内的用量合计（在数据库里汇总）。"""
    in_range = UsageDaily.day.between(start, end)
    totals: dict[uuid.UUID, dict[str, int]] = defaultdict(lambda: {m.value: 0 for m in Metric})
    aggregated = await session.execute(
        select(
            UsageDaily.tenant_id,
            UsageDaily.metric,
            func.sum(UsageDaily.value),
            func.max(UsageDaily.value),
        )
        .where(in_range, UsageDaily.metric.not_in([m.value for m in SNAPSHOT_METRICS]))
        .group_by(UsageDaily.tenant_id, UsageDaily.metric)
    )
    for tenant_id, metric, total, peak in aggregated:
        totals[tenant_id][metric] = int(peak if metric in MAX_METRICS else total)
    snapshots = await session.execute(
        select(UsageDaily.tenant_id, UsageDaily.metric, UsageDaily.value)
        .where(in_range, UsageDaily.metric.in_([m.value for m in SNAPSHOT_METRICS]))
        .order_by(UsageDaily.tenant_id, UsageDaily.metric, UsageDaily.day.desc())
        .ext(distinct_on(UsageDaily.tenant_id, UsageDaily.metric))
    )
    for tenant_id, metric, value in snapshots:
        totals[tenant_id][metric] = value
    tenants = (await session.scalars(select(Tenant).order_by(Tenant.created_at))).all()
    return TenantUsageList(
        start=start,
        end=end,
        timezone=tz.key,
        metrics=_metrics(),
        items=[
            TenantUsageOut(
                tenant_id=t.id, code=t.code, name=t.name, status=t.status, totals=totals[t.id]
            )
            for t in tenants
        ],
    )
