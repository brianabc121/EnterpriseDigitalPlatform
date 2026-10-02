"""AI 唤醒的调度与执行（设计文档 §33.2、§33.4–§33.6）。

- dispatch（调度进程每分钟）：按每个租户的设置和工作时间登记到期的唤醒——工作时间内的每小时检查、
  每日巡检、每周的知识库整理；同一类型、同一时段只登记一次（时段是小时、日期或周）。
- run_due（实时消费进程每几秒）：领取排队的唤醒（SKIP LOCKED，租约 15 分钟；进程中断、租约过期的
  重新排队，最多 3 次）并执行：
  - 每小时检查、每日巡检：先读增量更新索引（tenant_data_index，一次查询），检查项读的表都没有变化、
    口径没改、也没到它登记的时刻的跳过（§33.9）；其余的运行 → 更新问题（新出现、仍然存在、已消除、
    重新打开）→ 给负责人各发一条通知 → 每日巡检时升级超过几天没处理的问题，并由 AI 给管理员写简报；
    手动"立即唤醒"不跳过；
  - 知识库整理：kb/align.py。
- purge（调度进程每小时）：删除 90 天前的唤醒记录和已消除的问题。

通知在提交之后经统一出口发出（站内信、企业微信、AI 助理，§27.3.3）。
"""

import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.context import AppContext
from app.core.ids import new_id
from app.core.permissions import Permission
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway, pii, prompts
from app.modules.billing.entitlements import entitlements
from app.modules.changes import service as changes
from app.modules.iam.models import Staff, StaffStatus
from app.modules.kb import align
from app.modules.notifications import push
from app.modules.notifications import service as notifications
from app.modules.routing.hours import DEFAULT_TZ, in_business_hours, is_business_day
from app.modules.routing.models import RoutingPolicy
from app.modules.security.models import TenantSetting
from app.modules.tenancy.models import Tenant, TenantStatus
from app.modules.todos import assign, sla
from app.modules.wake import checks as registry
from app.modules.wake import settings as wake_settings
from app.modules.wake import state as check_state
from app.modules.wake.models import (
    SEVERITY_LABELS,
    SEVERITY_RANK,
    FindingStatus,
    RunKind,
    RunStatus,
    RunTrigger,
    Severity,
    WakeCheckState,
    WakeFinding,
    WakeRun,
)
from app.modules.wake.settings import WakeSettings

logger = logging.getLogger(__name__)

LEASE = timedelta(minutes=15)
MAX_ATTEMPTS = 3
KEEP = timedelta(days=90)
# 一次领取几次唤醒（每次都在自己的事务里执行）。
BATCH = 2
# 每条通知最多列出几个问题；简报里最多交给 AI 几个问题。
NOTICE_LINES = 5
BRIEF_TOP = 15
FEATURE_TTL = 600.0
RETIRED_NOTE = "检查项已关闭（或者套餐不再包含），不再跟踪这个问题"
KIND_FINDING = "wake_finding"
KIND_BRIEF = "wake_brief"
BRIEF_PATH = "/wake"
MINE_PATH = "/"

_features: dict[uuid.UUID, tuple[float, frozenset[str]]] = {}


@dataclass
class Outgoing:
    """提交之后发出的通知（站内信已经在事务里写好）。"""

    staff_ids: list[uuid.UUID]
    title: str
    body: str
    path: str


@dataclass
class Outcome:
    stats: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    outgoing: list[Outgoing] = field(default_factory=list)
    status: str = RunStatus.DONE


def clock(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


# ---- 登记 ----


def due_slots(
    settings: WakeSettings, spec: dict[str, Any] | None, now: datetime
) -> list[tuple[str, str]]:
    """这个时刻到期的（类型, 时段）。错过的每日巡检当天补上，错过的知识库整理当周补上。"""
    if not settings.enabled:
        return []
    tz = ZoneInfo((spec or {}).get("tz") or DEFAULT_TZ)
    local = now.astimezone(tz)
    slots: list[tuple[str, str]] = []
    workday = is_business_day(spec, now)
    if settings.hourly and workday and in_business_hours(spec, now):
        slots.append((RunKind.HOURLY, local.strftime("%Y-%m-%dT%H")))
    if (workday or not settings.daily_workdays_only) and (local.hour, local.minute) >= clock(
        settings.daily_time
    ):
        slots.append((RunKind.DAILY, local.date().isoformat()))
    weekday = local.isoweekday()
    if settings.kb_enabled and (
        weekday > settings.kb_weekday
        or (
            weekday == settings.kb_weekday and (local.hour, local.minute) >= clock(settings.kb_time)
        )
    ):
        year, week, _ = local.isocalendar()
        slots.append((RunKind.KB, f"{year}-W{week:02d}"))
    return slots


async def _tenant_features(session: AsyncSession, tenant_id: uuid.UUID) -> frozenset[str]:
    """套餐可用的功能（缓存 10 分钟，调度每分钟一次，不必每次都查订阅和套餐）。"""
    cached = _features.get(tenant_id)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    granted = await entitlements(session, tenant_id)
    value = frozenset(k for k, v in granted.features.items() if v)
    _features[tenant_id] = (time.monotonic() + FEATURE_TTL, value)
    return value


async def dispatch(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：登记到期的定时唤醒，返回登记的条数。"""
    now = now or datetime.now(UTC)
    async with ctx.db.platform_sessionmaker() as session:
        rows = (
            await session.execute(
                select(Tenant.id, TenantSetting.wake)
                .outerjoin(TenantSetting, TenantSetting.tenant_id == Tenant.id)
                .where(Tenant.status == TenantStatus.ACTIVE)
            )
        ).all()
        hours: dict[uuid.UUID, dict[str, Any] | None] = {
            tenant_id: spec
            for tenant_id, spec in await session.execute(
                select(RoutingPolicy.tenant_id, RoutingPolicy.business_hours).where(
                    RoutingPolicy.is_default
                )
            )
        }
        wanted = [
            (tenant_id, kind, slot)
            for tenant_id, raw in rows
            for kind, slot in due_slots(wake_settings.parse(raw), hours.get(tenant_id), now)
        ]
        if not wanted:
            return 0
        existing = set(
            (
                await session.execute(
                    select(WakeRun.tenant_id, WakeRun.kind, WakeRun.slot).where(
                        tuple_(WakeRun.tenant_id, WakeRun.kind, WakeRun.slot).in_(wanted)
                    )
                )
            ).all()
        )
        created = 0
        for tenant_id, kind, slot in wanted:
            if (tenant_id, kind, slot) in existing:
                continue
            if "ai" not in await _tenant_features(session, tenant_id):
                continue
            result = await session.execute(
                insert(WakeRun)
                .values(
                    id=new_id(),
                    tenant_id=tenant_id,
                    kind=kind,
                    trigger=RunTrigger.SCHEDULE,
                    slot=slot,
                    not_before=now,
                    created_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_wake_runs_slot")
            )
            created += max(0, getattr(result, "rowcount", 0) or 0)
        await session.commit()
    return created


# ---- 领取与执行 ----


async def run_due(ctx: AppContext, *, now: datetime | None = None, limit: int = BATCH) -> int:
    """实时消费进程：领取并执行排队的唤醒，返回执行的次数。"""
    now = now or datetime.now(UTC)
    claimed: list[tuple[uuid.UUID, uuid.UUID, str, str]] = []
    async with ctx.db.platform_sessionmaker() as session:
        # 进程中断后租约过期的：重新排队，已经试过 3 次的记为失败。
        await session.execute(
            update(WakeRun)
            .where(WakeRun.status == RunStatus.RUNNING, WakeRun.lease_until < now)
            .values(
                status=case(
                    (WakeRun.attempts >= MAX_ATTEMPTS, RunStatus.FAILED.value),
                    else_=RunStatus.QUEUED.value,
                ),
                error="执行中断（租约过期）",
                lease_until=None,
            )
        )
        runs = (
            await session.scalars(
                select(WakeRun)
                .where(WakeRun.status == RunStatus.QUEUED, WakeRun.not_before <= now)
                .order_by(WakeRun.not_before, WakeRun.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for run in runs:
            run.status = RunStatus.RUNNING
            run.lease_until = now + LEASE
            run.attempts += 1
            run.started_at = now
            claimed.append((run.id, run.tenant_id, run.kind, run.trigger))
        await session.commit()
    for run_id, tenant_id, kind, trigger in claimed:
        await execute(ctx, run_id, tenant_id, kind, trigger)
    return len(claimed)


async def run_once(
    ctx: AppContext, tenant_id: uuid.UUID, kind: str, trigger: str = RunTrigger.SCHEDULE
) -> WakeRun:
    """命令行：立即执行一次唤醒（直接领取，不经过队列，实时消费进程不会重复执行），返回执行完的
    记录。trigger 为 manual 时和"立即唤醒"一样全部重新检查，否则按增量更新索引跳过没有变化的。"""
    now = datetime.now(UTC)
    run = WakeRun(
        tenant_id=tenant_id,
        kind=kind,
        trigger=trigger,
        slot=f"{trigger}:{uuid.uuid4().hex[:24]}",
        status=RunStatus.RUNNING,
        not_before=now,
        lease_until=now + LEASE,
        attempts=1,
        started_at=now,
        created_at=now,
    )
    async with ctx.db.tenant_session(tenant_id) as session:
        session.add(run)
        await session.commit()
        run_id = run.id
    await execute(ctx, run_id, tenant_id, kind, trigger)
    async with ctx.db.tenant_session(tenant_id) as session:
        done = await session.get(WakeRun, run_id)
        assert done is not None
        return done


async def execute(
    ctx: AppContext,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    kind: str,
    trigger: str = RunTrigger.SCHEDULE,
) -> None:
    """执行一次唤醒；出错时记下错误（不再重试，下一个时段照常唤醒）。"""
    now = datetime.now(UTC)
    force = trigger == RunTrigger.MANUAL
    try:
        if kind == RunKind.KB:
            outcome = await _kb(ctx, run_id, tenant_id, now, force=force)
        else:
            outcome = await inspect(ctx, run_id, tenant_id, kind, now, force=force)
    except Exception as exc:
        logger.exception("wake run %s (%s) for tenant %s failed", run_id, kind, tenant_id)
        async with ctx.db.tenant_session(tenant_id) as session:
            await session.execute(
                update(WakeRun)
                .where(WakeRun.id == run_id)
                .values(
                    status=RunStatus.FAILED,
                    finished_at=datetime.now(UTC),
                    lease_until=None,
                    error=str(exc)[:500] or exc.__class__.__name__,
                )
            )
            await session.commit()
        return
    for notice in outcome.outgoing:
        try:
            await push.notify_staff(
                ctx,
                tenant_id,
                notice.staff_ids,
                title=notice.title,
                description=notice.body,
                path=notice.path,
            )
        except Exception:
            logger.exception("wake notification for tenant %s failed", tenant_id)


async def _finish(session: AsyncSession, run_id: uuid.UUID, outcome: Outcome) -> None:
    run = await session.get(WakeRun, run_id)
    if run is None:
        return
    run.status = outcome.status
    run.stats = outcome.stats
    run.summary = outcome.summary
    run.finished_at = datetime.now(UTC)
    run.lease_until = None
    run.error = None


async def _available(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[WakeSettings, frozenset[str]] | None:
    """醒来时仍然开启、套餐仍然包含 AI 时返回设置和可用的功能。"""
    settings = await wake_settings.load(session, tenant_id)
    if not settings.enabled:
        return None
    granted = await entitlements(session, tenant_id)
    features = frozenset(k for k, v in granted.features.items() if v)
    return (settings, features) if "ai" in features else None


async def _kb(
    ctx: AppContext, run_id: uuid.UUID, tenant_id: uuid.UUID, now: datetime, *, force: bool = False
) -> Outcome:
    async with ctx.db.tenant_session(tenant_id) as session:
        available = await _available(session, tenant_id)
        # 立即整理（手动）时不看"定期整理"的开关。
        if available is None or not (force or available[0].kb_enabled):
            outcome = Outcome(status=RunStatus.SKIPPED, stats={"reason": "disabled"})
        else:
            report = await align.run(
                ctx, session, tenant_id, now, settings=available[0], force=force
            )
            outcome = Outcome(
                stats=report.stats,
                outgoing=[Outgoing(n.staff_ids, n.title, n.body, n.path) for n in report.notices],
            )
        await _finish(session, run_id, outcome)
        await session.commit()
    return outcome


# ---- 数据巡检 ----


async def inspect(
    ctx: AppContext,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    kind: str,
    now: datetime,
    *,
    force: bool = False,
) -> Outcome:
    """每小时检查（只做需要及时处理的检查项）和每日巡检（全部检查项、升级、AI 简报）。
    force（立即唤醒）时不看增量更新索引，全部重新检查。"""
    daily = kind == RunKind.DAILY
    async with ctx.db.tenant_session(tenant_id) as session:
        available = await _available(session, tenant_id)
        if available is None:
            outcome = Outcome(status=RunStatus.SKIPPED, stats={"reason": "disabled"})
            await _finish(session, run_id, outcome)
            await session.commit()
            return outcome
        settings, features = available
        tz = sla.tz_of(await sla.business_hours(session))
        scope = registry.Scope(session, tenant_id, now, tz)
        enabled = [c for c in registry.applicable(features) if settings.check(c.code).enabled]
        retired = await retire(session, {c.code for c in enabled}, now)
        checks = [c for c in enabled if daily or c.hourly]
        # 增量更新索引：每张表最近一次变化的编号（一次查询）。
        index = await changes.snapshot(session)
        states = {s.check_code: s for s in (await session.scalars(select(WakeCheckState))).all()}
        results: dict[str, list[registry.Hit] | None] = {}
        skipped: list[str] = []
        for check in checks:
            params = check.resolve(settings.check(check.code).params)
            digest = registry.digest(check, params)
            seq = changes.latest(index, check.watched)
            if not force and check_state.unchanged(states.get(check.code), seq, digest, now):
                skipped.append(check.code)
                continue
            check_scope = scope.with_params(params)
            try:
                async with session.begin_nested():
                    results[check.code] = await check.run(check_scope)
            except Exception:
                logger.exception("wake check %s for tenant %s failed", check.code, tenant_id)
                results[check.code] = None
                continue
            await check_state.save(
                session,
                tenant_id,
                check.code,
                seq=seq,
                digest=digest,
                next_due=check_scope.next_due,
                now=now,
            )
        recorded = await record(session, tenant_id, run_id, results, now)
        outcome = Outcome(
            stats={
                "checks": len(checks),
                "ran": len(results),
                "skipped": len(skipped),
                "errors": [code for code, hits in results.items() if hits is None],
                "found": sum(len(h) for h in results.values() if h),
                "new": len(recorded.new),
                "raised": len(recorded.raised),
                "resolved": recorded.resolved + retired,
            }
        )
        outcome.outgoing += await _notify_assignees(
            session, tenant_id, [*recorded.new, *recorded.raised], now
        )
        if daily:
            escalated = await _escalate(session, settings, now)
            outcome.stats["escalated"] = len(escalated)
            summary, brief_stats, notice = await _brief(
                ctx, session, tenant_id, settings, recorded, escalated, tz, now
            )
            outcome.summary = summary
            outcome.stats.update(brief_stats)
            if notice is not None:
                outcome.outgoing.append(notice)
        outcome.stats["open"] = await _open_counts(session)
        outcome.stats["notified"] = len({s for n in outcome.outgoing for s in n.staff_ids})
        await _finish(session, run_id, outcome)
        await session.commit()
    return outcome


async def retire(session: AsyncSession, active: set[str], now: datetime) -> int:
    """关闭了的（或者套餐不再包含的）检查项：之前发现、还没处理的问题消除，检查状态删除（再打开时
    重新检查）。返回消除的问题数。"""
    result = await session.execute(
        update(WakeFinding)
        .where(
            WakeFinding.check_code.notin_(active),
            WakeFinding.status.in_((FindingStatus.OPEN, FindingStatus.IGNORED)),
        )
        .values(
            status=FindingStatus.RESOLVED,
            resolved_at=now,
            resolved_by=None,
            resolve_note=RETIRED_NOTE,
        )
    )
    await session.execute(
        delete(WakeCheckState).where(
            WakeCheckState.check_code.notin_(active),
            WakeCheckState.check_code.in_(list(registry.BY_CODE)),
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


@dataclass
class Recorded:
    new: list[WakeFinding] = field(default_factory=list)
    raised: list[WakeFinding] = field(default_factory=list)
    resolved: int = 0


def _apply(
    finding: WakeFinding, hit: registry.Hit, check: registry.Check, run_id: uuid.UUID
) -> None:
    finding.category = check.category
    finding.severity = hit.severity
    finding.title = hit.title
    finding.detail = hit.detail
    finding.link = hit.link
    finding.entity_type = hit.entity_type
    finding.entity_id = hit.entity_id
    finding.data = hit.data
    finding.assignee_ids = list(dict.fromkeys(hit.assignees))
    finding.run_id = run_id


def _reopen(finding: WakeFinding, now: datetime) -> None:
    finding.status = FindingStatus.OPEN
    finding.first_seen_at = now
    finding.seen_count = 0
    finding.notified_at = None
    finding.notified_severity = None
    finding.escalated_at = None
    finding.resolved_at = None
    finding.resolved_by = None
    finding.resolve_note = None
    finding.ignored_by = None
    finding.ignored_until = None
    finding.ignore_note = None


async def record(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    results: dict[str, list[registry.Hit] | None],
    now: datetime,
) -> Recorded:
    """按问题的唯一标识（检查项 + 对象）更新问题（§33.4）。只有成功运行的检查项才消除它之前
    发现的问题。"""
    recorded = Recorded()
    fingerprints = {
        f"{code}:{hit.key}": (code, hit) for code, hits in results.items() for hit in hits or []
    }
    ran = [code for code, hits in results.items() if hits is not None]
    conditions: list[ColumnElement[bool]] = []
    if fingerprints:
        conditions.append(WakeFinding.fingerprint.in_(list(fingerprints)))
    if ran:
        conditions.append(
            WakeFinding.check_code.in_(ran)
            & WakeFinding.status.in_((FindingStatus.OPEN, FindingStatus.IGNORED))
        )
    existing: dict[str, WakeFinding] = {}
    if conditions:
        rows = await session.scalars(select(WakeFinding).where(or_(*conditions)).with_for_update())
        existing = {f.fingerprint: f for f in rows.all()}
    for fingerprint, (code, hit) in fingerprints.items():
        check = registry.BY_CODE[code]
        finding = existing.get(fingerprint)
        if finding is None:
            finding = WakeFinding(
                tenant_id=tenant_id,
                check_code=code,
                fingerprint=fingerprint[:160],
                status=FindingStatus.OPEN,
                first_seen_at=now,
                last_seen_at=now,
                seen_count=1,
            )
            _apply(finding, hit, check, run_id)
            session.add(finding)
            recorded.new.append(finding)
            continue
        expired = (
            finding.status == FindingStatus.IGNORED
            and finding.ignored_until is not None
            and finding.ignored_until <= now
        )
        if finding.status == FindingStatus.RESOLVED or expired:
            _reopen(finding, now)
            recorded.new.append(finding)
        elif (
            finding.status == FindingStatus.OPEN
            and finding.notified_severity is not None
            and SEVERITY_RANK[Severity(hit.severity)]
            > SEVERITY_RANK[Severity(finding.notified_severity)]
        ):
            recorded.raised.append(finding)
        _apply(finding, hit, check, run_id)
        finding.last_seen_at = now
        finding.seen_count += 1
    for fingerprint, finding in existing.items():
        if fingerprint in fingerprints or finding.check_code not in ran:
            continue
        if finding.status in (FindingStatus.OPEN, FindingStatus.IGNORED):
            finding.status = FindingStatus.RESOLVED
            finding.resolved_at = now
            finding.resolved_by = None
            finding.resolve_note = "检查不再发现这个问题，已自动消除"
            recorded.resolved += 1
    await session.flush()
    return recorded


def notice_text(findings: list[WakeFinding]) -> tuple[str, str, str]:
    """给一个负责人的一条通知：标题、正文（最多列出 5 个）、链接。"""
    ordered = sorted(findings, key=lambda f: (-SEVERITY_RANK[Severity(f.severity)], f.title))
    if len(ordered) == 1:
        only = ordered[0]
        return (
            f"AI 巡检：{only.title}"[:200],
            only.detail or "",
            only.link or MINE_PATH,
        )
    lines = [f"· [{SEVERITY_LABELS[f.severity]}] {f.title}" for f in ordered[:NOTICE_LINES]]
    if len(ordered) > NOTICE_LINES:
        lines.append(f"……还有 {len(ordered) - NOTICE_LINES} 个")
    links = {f.link for f in ordered}
    path = links.pop() if len(links) == 1 and None not in links else MINE_PATH
    return f"AI 巡检：有 {len(ordered)} 个问题需要你处理", "\n".join(lines), path or MINE_PATH


async def _active(session: AsyncSession, staff_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    ids = list(set(staff_ids))
    if not ids:
        return set()
    rows = await session.scalars(
        select(Staff.id).where(Staff.id.in_(ids), Staff.status == StaffStatus.ACTIVE)
    )
    return set(rows.all())


async def _notify_assignees(
    session: AsyncSession, tenant_id: uuid.UUID, findings: list[WakeFinding], now: datetime
) -> list[Outgoing]:
    """新出现、重新打开和变严重的问题：给每个负责人发一条（站内信 + 统一出口）。"""
    per_staff: dict[uuid.UUID, list[WakeFinding]] = defaultdict(list)
    active = await _active(session, (s for f in findings for s in f.assignee_ids))
    for finding in findings:
        for staff_id in finding.assignee_ids:
            if staff_id in active:
                per_staff[staff_id].append(finding)
        finding.notified_at = now
        finding.notified_severity = finding.severity
    outgoing: list[Outgoing] = []
    for staff_id, items in per_staff.items():
        title, body, path = notice_text(items)
        notifications.add(
            session, tenant_id, [staff_id], kind=KIND_FINDING, title=title, body=body, link=path
        )
        outgoing.append(Outgoing([staff_id], title, body, path))
    return outgoing


async def _escalate(
    session: AsyncSession, settings: WakeSettings, now: datetime
) -> list[WakeFinding]:
    """超过设定的天数还没处理的问题升级给管理员（每个问题只升级一次，写在简报里）。"""
    rows = await session.scalars(
        select(WakeFinding)
        .where(
            WakeFinding.status == FindingStatus.OPEN,
            WakeFinding.escalated_at.is_(None),
            WakeFinding.first_seen_at <= now - timedelta(days=settings.escalate_days),
        )
        .with_for_update()
    )
    escalated = list(rows.all())
    for finding in escalated:
        finding.escalated_at = now
    return escalated


async def _open_counts(session: AsyncSession) -> dict[str, int]:
    rows = await session.execute(
        select(WakeFinding.severity, func.count())
        .where(WakeFinding.status == FindingStatus.OPEN)
        .group_by(WakeFinding.severity)
    )
    counts = {s.value: 0 for s in Severity}
    for severity, count in rows:
        counts[severity] = int(count)
    return counts


async def brief_recipients(session: AsyncSession, settings: WakeSettings) -> list[uuid.UUID]:
    """简报发给租户管理员和设置里指定的员工（启用的）。"""
    admins = await assign.staff_with(session, Permission.TENANT_MANAGE)
    extra = await _active(session, settings.brief_staff_ids)
    return list(dict.fromkeys([*admins, *[s for s in settings.brief_staff_ids if s in extra]]))


def _age(finding: WakeFinding, now: datetime) -> str:
    hours = int((now - finding.first_seen_at).total_seconds() // 3600)
    return f"已持续 {hours // 24} 天" if hours >= 24 else "今天发现"


def fallback_brief(counts: dict[str, int], new: int, resolved: int, top: list[str]) -> str:
    total = sum(counts.values())
    if not total:
        return "今天没有发现需要处理的问题。"
    parts = "、".join(
        f"{SEVERITY_LABELS[s]} {counts.get(s, 0)}"
        for s in (Severity.CRITICAL, Severity.WARNING, Severity.INFO)
        if counts.get(s)
    )
    text = f"目前有 {total} 个问题待处理（{parts}），今天新出现 {new} 个，已消除 {resolved} 个。"
    if top:
        text += "最要紧的：" + "；".join(f"{i}. {t}" for i, t in enumerate(top[:3], 1)) + "。"
    return text


async def _brief(
    ctx: AppContext,
    session: AsyncSession,
    tenant_id: uuid.UUID,
    settings: WakeSettings,
    recorded: Recorded,
    escalated: list[WakeFinding],
    tz: ZoneInfo,
    now: datetime,
) -> tuple[str, dict[str, Any], Outgoing | None]:
    """每日巡检后给管理员的 AI 简报（§33.6）。大模型不可用时用固定格式。"""
    rows = (
        await session.scalars(select(WakeFinding).where(WakeFinding.status == FindingStatus.OPEN))
    ).all()
    ordered = sorted(
        rows,
        key=lambda f: (
            -SEVERITY_RANK[Severity(f.severity)],
            f.escalated_at is None,
            f.first_seen_at,
        ),
    )
    counts = {s.value: 0 for s in Severity}
    for finding in rows:
        counts[finding.severity] += 1
    names = dict((await session.execute(select(Staff.id, Staff.display_name))).all())
    escalated_ids = {f.id for f in escalated}
    lines = []
    for i, finding in enumerate(ordered[:BRIEF_TOP], 1):
        owners = "、".join(names.get(s, "") for s in finding.assignee_ids[:3] if names.get(s))
        flags = [_age(finding, now)]
        if finding.id in escalated_ids:
            flags.append("今天升级给管理员")
        severity = SEVERITY_LABELS[finding.severity]
        category = registry.CATEGORY_LABELS.get(finding.category, "")
        lines.append(
            f"{i}. [{severity}][{category}] {finding.title}"
            f"（负责：{owners or '管理员'}；{'；'.join(flags)}）"
        )
    local = now.astimezone(tz)
    report = "\n".join(
        [
            f"日期：{local:%Y-%m-%d}",
            f"待处理的问题：共 {len(rows)} 个（严重 {counts['critical']}、"
            f"注意 {counts['warning']}、提示 {counts['info']}）；"
            f"今天新出现 {len(recorded.new)} 个，已消除 {recorded.resolved} 个；"
            f"超过 {settings.escalate_days} 天没处理、今天升级给管理员的 {len(escalated)} 个。",
            "最要紧的问题：" if lines else "没有待处理的问题。",
            *lines,
        ]
    )
    stats: dict[str, Any] = {"llm_calls": 0, "llm_cost": 0.0}
    summary = fallback_brief(
        counts, len(recorded.new), recorded.resolved, [f.title for f in ordered]
    )
    if rows and await ctx.llms.any_enabled():
        masked, _ = pii.mask(report)
        try:
            result = await gateway.chat(
                ctx,
                tenant_id,
                prompts.wake_brief_messages(report=masked),
                scene="wake_brief",
                fast=True,
                max_tokens=500,
            )
        except LLMUnavailable as exc:
            logger.warning("wake brief for tenant %s fell back: %s", tenant_id, exc)
        else:
            stats = {"llm_calls": 1, "llm_cost": round(result.cost, 4)}
            text = pii.strip_placeholders(result.content).strip()
            if text:
                summary = text[:1000]
    stats["brief_source"] = "llm" if stats["llm_calls"] else "template"
    recipients = await brief_recipients(session, settings)
    if not recipients:
        return summary, stats, None
    severe = counts["critical"]
    title = (
        f"AI 巡检日报：{len(rows)} 个问题待处理" + (f"（严重 {severe}）" if severe else "")
        if rows
        else "AI 巡检日报：今天没有发现需要处理的问题"
    )
    notifications.add(
        session, tenant_id, recipients, kind=KIND_BRIEF, title=title, body=summary, link=BRIEF_PATH
    )
    return summary, stats, Outgoing(recipients, title, summary, BRIEF_PATH)


# ---- 清理 ----


async def purge(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：删除 90 天前的唤醒记录和已消除的问题。返回删除的行数。"""
    now = now or datetime.now(UTC)
    cutoff = now - KEEP
    async with ctx.db.platform_sessionmaker() as session:
        runs = await session.execute(
            delete(WakeRun).where(
                WakeRun.created_at < cutoff,
                WakeRun.status.notin_((RunStatus.QUEUED, RunStatus.RUNNING)),
            )
        )
        findings = await session.execute(
            delete(WakeFinding).where(
                WakeFinding.status == FindingStatus.RESOLVED, WakeFinding.resolved_at < cutoff
            )
        )
        await session.commit()
    return int(getattr(runs, "rowcount", 0) or 0) + int(getattr(findings, "rowcount", 0) or 0)
