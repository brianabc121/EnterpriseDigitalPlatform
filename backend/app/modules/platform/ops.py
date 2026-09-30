"""运营后台（设计文档 §7.5）：系统健康、平台审计、各租户的渠道授权状态。"""

import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.db.partitions import partition_status
from app.events.bus import DEAD_LETTER_STREAM
from app.modules.ai.models import AiDecision, DecisionAction, LlmCall
from app.modules.audit.models import AuditLog
from app.modules.channels.models import ChannelAccount, ChannelStatus
from app.modules.conversation.models import ChatSession, ImOp, ImOpStatus, Message
from app.modules.iam.models import Staff
from app.modules.platform.schemas import (
    ChannelOverview,
    ComponentHealth,
    HealthReport,
    PlatformAuditList,
    PlatformAuditOut,
    TenantChannels,
    WecomBinding,
)
from app.modules.tenancy.models import PlatformUser, Tenant, TenantStatus
from app.modules.wecom.models import (
    CorpStatus,
    KfAccountStatus,
    WecomCorp,
    WecomKfAccount,
    WecomMember,
)

SCHEDULER_LEASE = "edp:scheduler:lease"
# 最近这么久的大模型调用失败率超过阈值时显示为降级。
LLM_WINDOW = timedelta(minutes=15)
LLM_ERROR_RATE = 0.2
LLM_MIN_CALLS = 5
# 到期超过这么久仍未执行的 IM 操作视为积压。
OUTBOX_LAG = timedelta(minutes=5)
EVENT_BACKLOG = 1000


def utcnow() -> datetime:
    return datetime.now(UTC)


async def _probe(
    key: str, name: str, check: Callable[[], Awaitable[str | None]]
) -> ComponentHealth:
    started = time.monotonic()
    try:
        detail = await check()
    except Exception as exc:
        return ComponentHealth(
            key=key, name=name, status="down", detail=f"{type(exc).__name__}: {exc}"[:300]
        )
    return ComponentHealth(
        key=key,
        name=name,
        status="ok",
        detail=detail,
        latency_ms=int((time.monotonic() - started) * 1000),
    )


async def health(ctx: AppContext) -> HealthReport:
    now = utcnow()
    components: list[ComponentHealth] = []

    async def database() -> str | None:
        async with ctx.db.platform_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return None

    async def redis() -> str | None:
        await ctx.redis.ping()
        return None

    async def openim() -> str | None:
        await ctx.im.ping()
        return None

    async def storage() -> str | None:
        await ctx.storage.ping()
        return None

    components.append(await _probe("database", "数据库", database))
    components.append(await _probe("redis", "Redis", redis))
    components.append(await _probe("openim", "OpenIM", openim))
    components.append(await _probe("storage", "对象存储", storage))

    metrics: dict[str, int | float] = {}
    async with ctx.db.platform_sessionmaker() as session:
        components.append(await _llm(ctx, session, now, metrics))
        components.append(await _outbox(session, now, metrics))
        components.append(await _partitions(session, metrics))
        metrics.update(await _business(session, now))
    components.append(await _wecom(ctx))
    components.append(await _clamav(ctx))
    components.extend(await _processes(ctx, metrics))

    statuses = {c.status for c in components}
    database_down = components[0].status == "down"
    overall = "down" if database_down else "degraded" if statuses & {"down", "degraded"} else "ok"
    return HealthReport(status=overall, checked_at=now, components=components, metrics=metrics)


async def _llm(
    ctx: AppContext, session: AsyncSession, now: datetime, metrics: dict[str, int | float]
) -> ComponentHealth:
    if not await ctx.llms.any_enabled():
        return ComponentHealth(key="llm", name="大模型", status="disabled", detail="没有配置供应商")
    total, errors, latency = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(LlmCall.status != "ok"),
                func.coalesce(func.avg(LlmCall.latency_ms).filter(LlmCall.status == "ok"), 0),
            ).where(LlmCall.created_at >= now - LLM_WINDOW)
        )
    ).one()
    metrics["llm_calls_15m"] = int(total)
    metrics["llm_errors_15m"] = int(errors)
    detail = f"最近 15 分钟 {total} 次调用，失败 {errors} 次"
    status = "ok"
    if total >= LLM_MIN_CALLS and errors / total > LLM_ERROR_RATE:
        status = "degraded"
    return ComponentHealth(
        key="llm",
        name="大模型",
        status=status,
        detail=detail,
        latency_ms=int(latency) if total else None,
    )


async def _outbox(
    session: AsyncSession, now: datetime, metrics: dict[str, int | float]
) -> ComponentHealth:
    lagging, failed = (
        await session.execute(
            select(
                func.count().filter(
                    ImOp.status == ImOpStatus.PENDING, ImOp.next_attempt_at < now - OUTBOX_LAG
                ),
                func.count().filter(
                    ImOp.status == ImOpStatus.FAILED, ImOp.created_at >= now - timedelta(days=1)
                ),
            )
        )
    ).one()
    metrics["outbox_lagging"] = int(lagging)
    metrics["outbox_failed_24h"] = int(failed)
    status = "degraded" if lagging or failed else "ok"
    return ComponentHealth(
        key="outbox",
        name="IM 发件箱",
        status=status,
        detail=f"积压 {lagging} 个，24 小时内失败 {failed} 个",
    )


async def _partitions(session: AsyncSession, metrics: dict[str, int | float]) -> ComponentHealth:
    status = await partition_status(session)
    metrics["message_partitions_ahead"] = status.months_ahead
    metrics["message_default_rows"] = status.default_rows
    healthy = status.months_ahead >= 1 and status.default_rows == 0
    return ComponentHealth(
        key="partitions",
        name="消息分区",
        status="ok" if healthy else "degraded",
        detail=(
            f"已建好之后 {status.months_ahead} 个月的分区，默认分区里有 {status.default_rows} 条"
        ),
    )


async def _wecom(ctx: AppContext) -> ComponentHealth:
    if ctx.wecom is None:
        return ComponentHealth(
            key="wecom", name="企业微信", status="disabled", detail="没有配置服务商"
        )
    try:
        ticket = await ctx.wecom.suite_ticket()
    except Exception as exc:
        return ComponentHealth(key="wecom", name="企业微信", status="down", detail=str(exc)[:300])
    if not ticket:
        return ComponentHealth(
            key="wecom",
            name="企业微信",
            status="degraded",
            detail="还没有收到 suite_ticket（检查指令回调地址）",
        )
    return ComponentHealth(key="wecom", name="企业微信", status="ok", detail="suite_ticket 正常")


async def _clamav(ctx: AppContext) -> ComponentHealth:
    if ctx.clamav is None:
        return ComponentHealth(
            key="clamav", name="病毒扫描", status="disabled", detail="没有配置 ClamAV"
        )
    if await ctx.clamav.ping():
        return ComponentHealth(key="clamav", name="病毒扫描", status="ok")
    return ComponentHealth(
        key="clamav", name="病毒扫描", status="down", detail="clamd 无响应，附件暂不扫描"
    )


async def _processes(ctx: AppContext, metrics: dict[str, int | float]) -> list[ComponentHealth]:
    try:
        scheduler = bool(await ctx.redis.exists(SCHEDULER_LEASE))
        workers = 0
        for partition in range(ctx.bus.partitions):
            workers += bool(await ctx.redis.exists(ctx.bus.lease_key(partition)))
        pending = sum(item.lag + item.pending for item in await ctx.bus.backlog())
        dead = int(await ctx.redis.xlen(DEAD_LETTER_STREAM))
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"[:300]
        return [
            ComponentHealth(key="worker", name="实时消费进程", status="down", detail=detail),
            ComponentHealth(key="scheduler", name="调度进程", status="down", detail=detail),
        ]
    metrics["event_pending"] = pending
    metrics["dead_letters"] = dead
    worker_status: Literal["ok", "degraded", "down"] = "ok" if workers else "down"
    if worker_status == "ok" and (dead or pending > EVENT_BACKLOG):
        worker_status = "degraded"
    return [
        ComponentHealth(
            key="worker",
            name="实时消费进程",
            status=worker_status,
            detail=(
                f"{workers}/{ctx.bus.partitions} 个分区有消费者，待处理 {pending} 个事件，"
                f"死信 {dead} 个"
            ),
        ),
        ComponentHealth(
            key="scheduler",
            name="调度进程",
            status="ok" if scheduler else "down",
            detail="持有调度租约" if scheduler else "没有调度进程在运行",
        ),
    ]


async def _business(session: AsyncSession, now: datetime) -> dict[str, int | float]:
    hour = now - timedelta(hours=1)
    tenants = await session.scalar(select(func.count()).where(Tenant.status == TenantStatus.ACTIVE))
    open_sessions, queued = (
        await session.execute(
            select(
                func.count().filter(ChatSession.status != "closed"),
                func.count().filter(ChatSession.status == "queued"),
            )
        )
    ).one()
    messages = await session.scalar(select(func.count()).where(Message.sent_at >= hour))
    ai_replies = await session.scalar(
        select(func.count()).where(
            AiDecision.action == DecisionAction.REPLY, AiDecision.created_at >= hour
        )
    )
    return {
        "active_tenants": int(tenants or 0),
        "open_sessions": int(open_sessions),
        "queued_sessions": int(queued),
        "messages_1h": int(messages or 0),
        "ai_replies_1h": int(ai_replies or 0),
    }


# ---- 平台审计 ----


async def audit_logs(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None = None,
    action: str | None = None,
    actor_type: str | None = None,
    start: datetime | None = None,
    before: datetime | None = None,
    limit: int = 50,
) -> PlatformAuditList:
    statement = (
        select(AuditLog, Tenant.code)
        .outerjoin(Tenant, Tenant.id == AuditLog.tenant_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit + 1)
    )
    if tenant_id is not None:
        statement = statement.where(AuditLog.tenant_id == tenant_id)
    if action:
        statement = statement.where(
            or_(AuditLog.action == action, AuditLog.action.startswith(f"{action}."))
        )
    if actor_type:
        statement = statement.where(AuditLog.actor_type == actor_type)
    if start is not None:
        statement = statement.where(AuditLog.created_at >= start)
    if before is not None:
        statement = statement.where(AuditLog.created_at < before)
    rows = (await session.execute(statement)).all()
    page, more = rows[:limit], len(rows) > limit
    platform_ids = {a.actor_id for a, _ in page if a.actor_type == "platform" and a.actor_id}
    staff_ids = {a.actor_id for a, _ in page if a.actor_type == "staff" and a.actor_id}
    names: dict[uuid.UUID, str] = {}
    if platform_ids:
        names.update(
            (
                await session.execute(
                    select(PlatformUser.id, PlatformUser.display_name).where(
                        PlatformUser.id.in_(platform_ids)
                    )
                )
            ).all()
        )
    if staff_ids:
        names.update(
            (
                await session.execute(
                    select(Staff.id, Staff.display_name).where(Staff.id.in_(staff_ids))
                )
            ).all()
        )
    items = [
        PlatformAuditOut(
            id=a.id,
            tenant_id=a.tenant_id,
            tenant_code=code,
            actor_type=a.actor_type,
            actor_id=a.actor_id,
            actor_name=names.get(a.actor_id) if a.actor_id else None,
            action=a.action,
            resource_type=a.resource_type,
            resource_id=a.resource_id,
            detail=a.detail or {},
            ip=a.ip,
            created_at=a.created_at,
        )
        for a, code in page
    ]
    return PlatformAuditList(items=items, next_before=page[-1][0].created_at if more else None)


# ---- 渠道授权状态 ----


def _last_sync(state: dict[str, Any]) -> tuple[datetime | None, list[str]]:
    latest = None
    errors = []
    for target, entry in (state or {}).items():
        if not isinstance(entry, dict):
            continue
        at = entry.get("at")
        if isinstance(at, str):
            try:
                moment = datetime.fromisoformat(at)
            except ValueError:
                moment = None
            if moment and (latest is None or moment > latest):
                latest = moment
        if entry.get("error"):
            errors.append(f"{target}: {entry['error']}")
    return latest, errors


async def channel_overview(ctx: AppContext, session: AsyncSession) -> ChannelOverview:
    tenants = (
        await session.scalars(
            select(Tenant).where(Tenant.status != TenantStatus.CLOSED).order_by(Tenant.created_at)
        )
    ).all()
    counts: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    disabled: dict[uuid.UUID, int] = defaultdict(int)
    rows = await session.execute(
        select(
            ChannelAccount.tenant_id, ChannelAccount.type, ChannelAccount.status, func.count()
        ).group_by(ChannelAccount.tenant_id, ChannelAccount.type, ChannelAccount.status)
    )
    for tenant_id, kind, status, n in rows:
        if status == ChannelStatus.ACTIVE:
            counts[tenant_id][kind] = counts[tenant_id].get(kind, 0) + n
        else:
            disabled[tenant_id] += n
    # 每个租户取一条授权记录：有效的优先，其次是最近授权的。
    corps: dict[uuid.UUID, WecomCorp] = {}
    ordered = await session.scalars(
        select(WecomCorp).order_by(
            WecomCorp.tenant_id,
            case((WecomCorp.status == CorpStatus.ACTIVE, 0), else_=1),
            WecomCorp.authorized_at.desc(),
        )
    )
    for row in ordered.all():
        corps.setdefault(row.tenant_id, row)
    kf = dict(
        (
            await session.execute(
                select(WecomKfAccount.tenant_id, func.count())
                .where(WecomKfAccount.status == KfAccountStatus.ACTIVE)
                .group_by(WecomKfAccount.tenant_id)
            )
        ).all()
    )
    members = dict(
        (
            await session.execute(
                select(WecomMember.tenant_id, func.count()).group_by(WecomMember.tenant_id)
            )
        ).all()
    )
    items = []
    for tenant in tenants:
        corp = corps.get(tenant.id)
        binding = None
        if corp is not None:
            last, errors = _last_sync(corp.sync_state)
            binding = WecomBinding(
                corp_id=corp.corp_id,
                corp_name=corp.corp_name,
                status=corp.status,
                authorized_at=corp.authorized_at,
                cancelled_at=corp.cancelled_at,
                kf_accounts=int(kf.get(tenant.id, 0)),
                members=int(members.get(tenant.id, 0)),
                last_sync_at=last,
                sync_errors=errors,
            )
        items.append(
            TenantChannels(
                tenant_id=tenant.id,
                code=tenant.code,
                name=tenant.name,
                status=tenant.status,
                channels=counts.get(tenant.id, {}),
                disabled_channels=disabled.get(tenant.id, 0),
                wecom=binding,
            )
        )
    return ChannelOverview(wecom_configured=ctx.wecom is not None, items=items)
