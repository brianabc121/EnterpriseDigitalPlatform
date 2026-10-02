"""AI 唤醒页面和首页"需要我处理的问题"的业务逻辑（设计文档 §33.5、§33.8、§33.10）。"""

import uuid
from collections.abc import Iterable
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.modules.ai import answer_cache
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import entitlements
from app.modules.changes import service as changes
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.kb import align
from app.modules.routing.hours import DEFAULT_TZ
from app.modules.todos import sla
from app.modules.wake import checks as registry
from app.modules.wake import queue
from app.modules.wake import settings as wake_settings
from app.modules.wake.models import (
    RUN_KIND_LABELS,
    FindingStatus,
    RunKind,
    RunStatus,
    RunTrigger,
    Severity,
    WakeCheckState,
    WakeFinding,
    WakeRun,
)
from app.modules.wake.runner import due_slots
from app.modules.wake.schemas import (
    CheckOut,
    CheckParamOut,
    DataChange,
    FindingOut,
    FindingPage,
    IgnoreRequest,
    KbAlignment,
    Person,
    ResolveRequest,
    RunOut,
    RunPage,
    SeverityCounts,
    WakeOverview,
    WakeSettingsOut,
)
from app.modules.wake.settings import WakeSettings

# 计算下次唤醒时最多往后看几天。
LOOKAHEAD_DAYS = 8


def is_admin(principal: Principal) -> bool:
    """AI 唤醒页面的管理员（有"设置"权限）：看全部问题、处理任何问题、改设置。"""
    return principal.has(Permission.SETTINGS_MANAGE)


async def features_of(session: AsyncSession, tenant_id: uuid.UUID) -> frozenset[str]:
    granted = await entitlements(session, tenant_id)
    return frozenset(k for k, v in granted.features.items() if v)


async def _names(session: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(wanted)))
    return {staff_id: name for staff_id, name in rows}


def _person(names: dict[uuid.UUID, str], staff_id: uuid.UUID | None) -> Person | None:
    if staff_id is None:
        return None
    return Person(id=staff_id, name=names.get(staff_id, "已删除的员工"))


# ---- 问题 ----


def _severity_order() -> Any:
    """严重的在前。"""
    return case(
        (WakeFinding.severity == Severity.CRITICAL, 0),
        (WakeFinding.severity == Severity.WARNING, 1),
        else_=2,
    )


def finding_out(
    finding: WakeFinding, names: dict[uuid.UUID, str], principal: Principal
) -> FindingOut:
    check = registry.BY_CODE.get(finding.check_code)
    mine = principal.staff_id in finding.assignee_ids
    return FindingOut(
        id=finding.id,
        check_code=finding.check_code,
        check_title=check.title if check else finding.check_code,
        category=finding.category,
        category_label=registry.CATEGORY_LABELS.get(finding.category, finding.category),
        severity=finding.severity,
        status=finding.status,
        title=finding.title,
        detail=finding.detail,
        link=finding.link,
        entity_type=finding.entity_type,
        entity_id=finding.entity_id,
        data=finding.data or {},
        assignees=[Person(id=s, name=names.get(s, "已删除的员工")) for s in finding.assignee_ids],
        first_seen_at=finding.first_seen_at,
        last_seen_at=finding.last_seen_at,
        seen_count=finding.seen_count,
        notified_at=finding.notified_at,
        escalated_at=finding.escalated_at,
        resolved_at=finding.resolved_at,
        resolved_by=_person(names, finding.resolved_by),
        resolve_note=finding.resolve_note,
        ignored_by=_person(names, finding.ignored_by),
        ignored_until=finding.ignored_until,
        ignore_note=finding.ignore_note,
        mine=mine,
        can_handle=mine or is_admin(principal),
    )


async def _outs(
    session: AsyncSession, findings: list[WakeFinding], principal: Principal
) -> list[FindingOut]:
    names = await _names(
        session,
        [
            *(s for f in findings for s in f.assignee_ids),
            *(f.resolved_by for f in findings),
            *(f.ignored_by for f in findings),
        ],
    )
    return [finding_out(f, names, principal) for f in findings]


async def counts(session: AsyncSession, staff_id: uuid.UUID | None = None) -> SeverityCounts:
    """待处理的问题数（staff_id 有值时只算他负责的）。"""
    query = (
        select(WakeFinding.severity, func.count())
        .where(WakeFinding.status == FindingStatus.OPEN)
        .group_by(WakeFinding.severity)
    )
    if staff_id is not None:
        query = query.where(WakeFinding.assignee_ids.contains([staff_id]))
    result = SeverityCounts()
    for severity, count in await session.execute(query):
        if severity in (Severity.CRITICAL, Severity.WARNING, Severity.INFO):
            setattr(result, severity, int(count))
        result.total += int(count)
    return result


async def list_findings(
    session: AsyncSession,
    principal: Principal,
    *,
    view: str,
    status: str | None,
    category: str | None,
    severity: str | None,
    limit: int,
    offset: int,
) -> FindingPage:
    """view=mine：负责人包含自己的；view=all：全部（管理员）。待处理的按级别、发现的先后排列。"""
    if view == "all" and not is_admin(principal):
        raise Forbidden("没有查看全部问题的权限")
    conditions = []
    if view == "mine":
        conditions.append(WakeFinding.assignee_ids.contains([principal.staff_id]))
    if status is not None:
        conditions.append(WakeFinding.status == status)
    if category is not None:
        conditions.append(WakeFinding.category == category)
    if severity is not None:
        conditions.append(WakeFinding.severity == severity)
    order = [
        (WakeFinding.status != FindingStatus.OPEN),
        _severity_order(),
        WakeFinding.first_seen_at.desc(),
        WakeFinding.id,
    ]
    total = int(
        await session.scalar(select(func.count()).select_from(WakeFinding).where(*conditions)) or 0
    )
    rows = (
        await session.scalars(
            select(WakeFinding).where(*conditions).order_by(*order).limit(limit).offset(offset)
        )
    ).all()
    return FindingPage(
        items=await _outs(session, list(rows), principal),
        total=total,
        open=await counts(session, principal.staff_id if view == "mine" else None),
    )


async def _handled(
    session: AsyncSession, principal: Principal, finding_id: uuid.UUID
) -> WakeFinding:
    finding = await session.get(WakeFinding, finding_id, with_for_update=True)
    if finding is None:
        raise NotFound("问题不存在")
    if principal.staff_id not in finding.assignee_ids and not is_admin(principal):
        raise Forbidden("只有负责人或管理员可以处理这个问题")
    return finding


def _audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    finding: WakeFinding,
    detail: dict[str, Any],
    ip: str | None,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="wake_finding",
        resource_id=str(finding.id),
        detail={"check": finding.check_code, "title": finding.title, **detail},
        ip=ip,
    )


async def ignore(
    session: AsyncSession,
    principal: Principal,
    finding_id: uuid.UUID,
    payload: IgnoreRequest,
    now: datetime,
    ip: str | None = None,
) -> FindingOut:
    """忽略（§33.4）：几天内不再提醒，到期后仍然存在就重新打开；一直忽略到问题消除。"""
    finding = await _handled(session, principal, finding_id)
    if finding.status == FindingStatus.RESOLVED:
        raise Unprocessable("问题已经消除")
    finding.status = FindingStatus.IGNORED
    finding.ignored_by = principal.staff_id
    finding.ignored_until = now + timedelta(days=payload.days) if payload.days else None
    finding.ignore_note = (payload.note or "").strip() or None
    _audit(session, principal, "wake.finding.ignore", finding, {"days": payload.days}, ip)
    await session.flush()
    return (await _outs(session, [finding], principal))[0]


async def resolve(
    session: AsyncSession,
    principal: Principal,
    finding_id: uuid.UUID,
    payload: ResolveRequest,
    now: datetime,
    ip: str | None = None,
) -> FindingOut:
    """标记已处理（§33.4）：这个检查项下次一定重新检查（不按增量更新索引跳过），仍然发现就
    重新打开。"""
    finding = await _handled(session, principal, finding_id)
    if finding.status != FindingStatus.RESOLVED:
        finding.status = FindingStatus.RESOLVED
        finding.resolved_at = now
        finding.resolved_by = principal.staff_id
        finding.resolve_note = (payload.note or "").strip() or "已处理"
        await session.execute(
            delete(WakeCheckState).where(WakeCheckState.check_code == finding.check_code)
        )
        _audit(session, principal, "wake.finding.resolve", finding, {}, ip)
        await session.flush()
    return (await _outs(session, [finding], principal))[0]


# ---- 唤醒记录 ----


def run_out(run: WakeRun, names: dict[uuid.UUID, str]) -> RunOut:
    return RunOut(
        id=run.id,
        kind=run.kind,
        kind_label=RUN_KIND_LABELS.get(run.kind, run.kind),
        trigger=run.trigger,
        status=run.status,
        not_before=run.not_before,
        started_at=run.started_at,
        finished_at=run.finished_at,
        stats=run.stats or {},
        summary=run.summary,
        error=run.error,
        created_by=_person(names, run.created_by),
        created_at=run.created_at,
    )


async def _run_outs(session: AsyncSession, runs: list[WakeRun]) -> list[RunOut]:
    names = await _names(session, (r.created_by for r in runs))
    return [run_out(r, names) for r in runs]


async def list_runs(session: AsyncSession, *, kind: str | None, limit: int, offset: int) -> RunPage:
    conditions = [WakeRun.kind == kind] if kind else []
    total = int(
        await session.scalar(select(func.count()).select_from(WakeRun).where(*conditions)) or 0
    )
    rows = (
        await session.scalars(
            select(WakeRun)
            .where(*conditions)
            .order_by(WakeRun.created_at.desc(), WakeRun.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return RunPage(items=await _run_outs(session, list(rows)), total=total)


async def run_now(
    session: AsyncSession, principal: Principal, kind: str, now: datetime, ip: str | None = None
) -> RunOut:
    """立即唤醒（每日巡检，不看增量更新索引、全部重新检查）或者立即整理知识库。已经有排队中的
    同类唤醒时合并到那一条并让它马上开始。"""
    settings = await wake_settings.load(session, principal.tenant_id)
    if not settings.enabled:
        raise Unprocessable("AI 唤醒已关闭，请先在设置里打开")
    run = await queue.enqueue(
        session,
        principal.tenant_id,
        RunKind.DAILY if kind == "daily" else RunKind.KB,
        RunTrigger.MANUAL,
        now=now,
        created_by=principal.staff_id,
    )
    record_audit(
        session,
        action="wake.run",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="wake_run",
        resource_id=str(run.id),
        detail={"kind": kind},
        ip=ip,
    )
    await session.flush()
    return (await _run_outs(session, [run]))[0]


# ---- 设置 ----


async def settings_out(
    session: AsyncSession, tenant_id: uuid.UUID, features: frozenset[str] | None = None
) -> WakeSettingsOut:
    settings = await wake_settings.load(session, tenant_id)
    features = features if features is not None else await features_of(session, tenant_id)
    available = {c.code for c in registry.applicable(features)}
    states = {s.check_code: s for s in (await session.scalars(select(WakeCheckState))).all()}
    changed = await changes.changed_at(session)
    checks = []
    for check in registry.CHECKS:
        config = settings.check(check.code)
        values = check.resolve(config.params)
        state = states.get(check.code)
        times = [changed[d] for d in check.watched if d in changed]
        checks.append(
            CheckOut(
                code=check.code,
                category=check.category,
                category_label=registry.CATEGORY_LABELS.get(check.category, check.category),
                title=check.title,
                description=check.description,
                hourly=check.hourly,
                available=check.code in available,
                enabled=config.enabled,
                params=[
                    CheckParamOut(
                        name=p.name,
                        label=p.label,
                        unit=p.unit,
                        default=p.default,
                        minimum=p.minimum,
                        maximum=p.maximum,
                        value=values[p.name],
                    )
                    for p in check.params
                ],
                domains=list(check.domains),
                checked_at=state.checked_at if state else None,
                changed_at=max(times) if times else None,
            )
        )
    return WakeSettingsOut(settings=settings, checks=checks)


async def save_settings(
    session: AsyncSession,
    principal: Principal,
    payload: WakeSettings,
    ip: str | None = None,
) -> WakeSettingsOut:
    """保存设置。检查项和数字超出范围时返回 422；只保存和默认不同的检查项。"""
    clean: dict[str, wake_settings.CheckConfig] = {}
    for code, config in payload.checks.items():
        check = registry.BY_CODE.get(code)
        if check is None:
            raise Unprocessable(f"未知的检查项：{code}")
        known = {p.name: p for p in check.params}
        params: dict[str, int] = {}
        for name, value in config.params.items():
            param = known.get(name)
            if param is None:
                raise Unprocessable(f"「{check.title}」没有这个数字：{name}")
            if not param.minimum <= value <= param.maximum:
                raise Unprocessable(
                    f"「{check.title}」的{param.label}要在 {param.minimum}–{param.maximum}"
                    f"{param.unit}之间"
                )
            if value != param.default:
                params[name] = value
        if not config.enabled or params:
            clean[code] = wake_settings.CheckConfig(enabled=config.enabled, params=params)
    saved = payload.model_copy(update={"checks": clean})
    before = await wake_settings.load(session, principal.tenant_id)
    await wake_settings.save(session, principal.tenant_id, saved, principal.staff_id)
    if saved.kb_hold_conflicts != before.kb_hold_conflicts:
        # 暂停或者恢复冲突的知识用于回复客户：之前缓存的回答不再使用。
        await answer_cache.clear(session, principal.tenant_id)
    record_audit(
        session,
        action="wake.settings.update",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="tenant_settings",
        resource_id=str(principal.tenant_id),
        detail=saved.model_dump(mode="json"),
        ip=ip,
    )
    await session.flush()
    return await settings_out(session, principal.tenant_id)


# ---- 概况 ----


def next_times(
    settings: WakeSettings,
    spec: dict[str, Any] | None,
    now: datetime,
    current: set[tuple[str, str]],
) -> dict[str, datetime | None]:
    """下一次每小时检查、每日巡检、知识库整理的时间。current 是已经登记过的（类型, 时段）：
    现在到期、还没登记的（例如刚把时间改早了）一分钟内就会唤醒，按现在算。"""
    result: dict[str, datetime | None] = {k.value: None for k in RunKind}
    for kind, slot in due_slots(settings, spec, now):
        if (kind, slot) not in current:
            result[kind] = now
    tz = ZoneInfo((spec or {}).get("tz") or DEFAULT_TZ)
    local = now.astimezone(tz)
    moments: set[datetime] = set()
    for offset in range(LOOKAHEAD_DAYS):
        day = local.date() + timedelta(days=offset)
        for hour in range(24):
            moments.add(datetime.combine(day, time(hour), tzinfo=tz))
        for value in (settings.daily_time, settings.kb_time):
            hour, minute = (int(x) for x in value.split(":"))
            moments.add(datetime.combine(day, time(hour, minute), tzinfo=tz))
        for ranges in ((spec or {}).get("days") or {}).get(str(day.isoweekday()), []):
            if ranges and ranges[0] != "24:00":
                hour, minute = (int(x) for x in ranges[0].split(":"))
                moments.add(datetime.combine(day, time(hour, minute), tzinfo=tz))
    seen = {(kind, slot) for kind, slot in due_slots(settings, spec, now)} | current
    for moment in sorted(m for m in moments if m > local):
        for kind, slot in due_slots(settings, spec, moment):
            if result[kind] is None and (kind, slot) not in seen:
                result[kind] = moment
        if all(result.values()):
            break
    return result


async def overview(session: AsyncSession, principal: Principal, now: datetime) -> WakeOverview:
    tenant_id = principal.tenant_id
    settings = await wake_settings.load(session, tenant_id)
    features = await features_of(session, tenant_id)
    spec = await sla.business_hours(session)
    slots = due_slots(settings, spec, now)
    current: set[tuple[str, str]] = set()
    if slots:
        rows = await session.execute(
            select(WakeRun.kind, WakeRun.slot).where(WakeRun.slot.in_([slot for _, slot in slots]))
        )
        current = {(kind, slot) for kind, slot in rows}
    times = (
        next_times(settings, spec, now, current)
        if settings.enabled and "ai" in features
        else {k.value: None for k in RunKind}
    )

    async def latest(*conditions: Any) -> WakeRun | None:
        return await session.scalar(
            select(WakeRun)
            .where(*conditions)
            .order_by(WakeRun.finished_at.desc().nulls_last(), WakeRun.created_at.desc())
            .limit(1)
        )

    done = WakeRun.status == RunStatus.DONE
    brief = await latest(WakeRun.kind == RunKind.DAILY, done)
    inspected = await latest(WakeRun.kind.in_((RunKind.DAILY, RunKind.HOURLY)), done)
    kb = await latest(WakeRun.kind == RunKind.KB, done)
    pending = (
        await session.scalars(
            select(WakeRun)
            .where(WakeRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)))
            .order_by(WakeRun.not_before)
            .limit(10)
        )
    ).all()
    runs = await _run_outs(
        session, [r for r in (brief, inspected, kb) if r is not None] + list(pending)
    )
    by_id = {r.id: r for r in runs}
    recent, tracked = await changes.recent(session)
    return WakeOverview(
        available="ai" in features,
        enabled=settings.enabled,
        next_hourly=times[RunKind.HOURLY],
        next_daily=times[RunKind.DAILY],
        next_kb=times[RunKind.KB],
        open=await counts(session),
        brief=by_id.get(brief.id) if brief else None,
        latest=by_id.get(inspected.id) if inspected else None,
        kb=by_id.get(kb.id) if kb else None,
        pending=[by_id[r.id] for r in pending],
        data_index=[
            DataChange(
                domain=row.domain,
                label=changes.label(row.domain),
                seq=row.seq,
                changed_at=row.changed_at,
            )
            for row in recent
        ],
        tracked=tracked,
    )


async def kb_alignment(session: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> KbAlignment:
    """知识库页的"制度对齐"：最近一次整理的报告、现行制度几份、待处理的建议、是否在整理。"""
    finished_at, report = await align.latest_report(session)
    settings = await wake_settings.load(session, tenant_id)
    running = bool(
        await session.scalar(
            select(func.count())
            .select_from(WakeRun)
            .where(
                WakeRun.kind == RunKind.KB,
                WakeRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)),
            )
        )
    )
    return KbAlignment(
        finished_at=finished_at,
        report=report,
        policies=len(await align.current_policies(session, now)),
        pending=await align.pending_count(session),
        running=running,
        enabled=settings.enabled and settings.kb_enabled,
    )
