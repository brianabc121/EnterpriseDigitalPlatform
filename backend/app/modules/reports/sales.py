"""销售报表（设计文档 §40.9）：漏斗、金额、赢单率、输单原因、按负责人、按来源。

数据范围按客户的可见范围（opportunities.service.visible_to）；预计金额是人填的估计，和订单的实际金额
分开；金额设置为只有管理者可见而自己不能看时，金额都返回空。期间按新建时间（漏斗、按负责人的新建、
按来源）和关闭时间（赢单率、输单原因）算；进行中的金额不限期间。
"""

import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dates import day_bounds
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.opportunities import service as opportunity_service
from app.modules.opportunities import settings as opportunity_settings
from app.modules.opportunities.models import (
    SOURCE_LABELS,
    ActivityKind,
    Opportunity,
    OpportunityActivity,
    OpportunityStatus,
    StageKind,
)
from app.modules.reports.schemas import (
    Bucket,
    FunnelStage,
    OwnerSales,
    SalesAmount,
    SalesReport,
    SalesWin,
    SourceSales,
)

CENT = Decimal("0.01")
CLOSED = (OpportunityStatus.WON, OpportunityStatus.LOST)


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _money(value: Decimal | int | None) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def _avg(values: list[Decimal]) -> Decimal | None:
    return (sum(values, Decimal(0)) / len(values)).quantize(CENT) if values else None


def _avg_days(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _month_bounds(day: date) -> tuple[date, date, date]:
    start = day.replace(day=1)
    following = (start + timedelta(days=32)).replace(day=1)
    after = (following + timedelta(days=32)).replace(day=1)
    return start, following, after


async def sales_report(
    session: AsyncSession, principal: Principal, start: date, end: date, tz: ZoneInfo
) -> SalesReport:
    lower, upper = day_bounds(start, tz)[0], day_bounds(end, tz)[1]
    scope = opportunity_service.visible_to(principal)
    settings = await opportunity_settings.load(session, principal.tenant_id)
    show_amount = opportunity_service.amount_visible(principal, settings)
    all_stages = await opportunity_service.stages(session, principal.tenant_id)
    opens = opportunity_service.open_stages(all_stages)
    by_id = {s.id: s for s in all_stages}
    position = {s.code: s.position for s in opens}
    top = max((s.position for s in opens), default=0)

    def money(value: Decimal | int | None) -> Decimal | None:
        return _money(value) if show_amount else None

    # 期间内新建的（AI 建议的要员工确认过；忽略的不算）。
    created_rows = list(
        (
            await session.scalars(
                select(Opportunity).where(
                    scope,
                    Opportunity.created_at >= lower,
                    Opportunity.created_at < upper,
                    Opportunity.status.in_((OpportunityStatus.ACTIVE, *CLOSED)),
                )
            )
        ).all()
    )
    created_ids = [o.id for o in created_rows]
    moves: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    if created_ids:
        for opportunity_id, properties in await session.execute(
            select(OpportunityActivity.opportunity_id, OpportunityActivity.properties).where(
                OpportunityActivity.opportunity_id.in_(created_ids),
                OpportunityActivity.kind == ActivityKind.STAGE,
            )
        ):
            moves[opportunity_id].append(properties or {})

    # 漏斗：到过 = 曾经进入过这个阶段或更靠后的阶段（赢单的到过全部）。
    reached: dict[uuid.UUID, int] = {}
    for o in created_rows:
        stage = by_id.get(o.stage_id)
        best = -1
        if o.status == OpportunityStatus.WON:
            best = top
        elif stage is not None and stage.kind == StageKind.OPEN:
            best = stage.position
        for properties in moves[o.id]:
            for key in ("from", "to"):
                code = properties.get(key)
                if isinstance(code, str) and code in position:
                    best = max(best, position[code])
        reached[o.id] = best
    funnel = []
    previous: int | None = None
    for stage in opens:
        count = sum(1 for best in reached.values() if best >= stage.position)
        funnel.append(
            FunnelStage(
                code=stage.code,
                name=stage.name,
                count=count,
                rate=_rate(count, previous) if previous is not None else None,
            )
        )
        previous = count
    won_created = sum(1 for o in created_rows if o.status == OpportunityStatus.WON)
    funnel.append(
        FunnelStage(
            code="won",
            name="赢单",
            count=won_created,
            rate=_rate(won_created, previous) if previous is not None else None,
        )
    )

    # 进行中的金额（不限期间）。
    today = datetime.now(tz).date()
    month_start, next_start, after_next = _month_bounds(today)
    by_stage: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    weighted = Decimal(0)
    this_month = Decimal(0)
    next_month = Decimal(0)
    open_count = 0
    for stage_id, amount, probability, expected in await session.execute(
        select(
            Opportunity.stage_id,
            Opportunity.amount,
            Opportunity.probability,
            Opportunity.expected_close_at,
        ).where(scope, Opportunity.status == OpportunityStatus.ACTIVE)
    ):
        open_count += 1
        if amount is None:
            continue
        stage = by_id.get(stage_id)
        by_stage[stage_id] += amount
        chance = probability if probability is not None else (stage.probability if stage else 0)
        weighted += amount * Decimal(chance) / Decimal(100)
        if expected is not None and month_start <= expected < next_start:
            this_month += amount
        elif expected is not None and next_start <= expected < after_next:
            next_month += amount
    amount_stats = SalesAmount(
        open_count=open_count,
        by_stage=[
            Bucket(key=s.code, label=s.name, count=0, amount=money(by_stage.get(s.id)))
            for s in opens
        ],
        weighted=money(weighted),
        this_month=money(this_month),
        next_month=money(next_month),
    )

    # 期间内关闭的：赢单率、周期、金额、输单原因。
    closed_rows = (
        await session.execute(
            select(
                Opportunity.status,
                Opportunity.amount,
                Opportunity.opened_at,
                Opportunity.closed_at,
                Opportunity.lost_reason_code,
                Opportunity.owner_id,
            ).where(
                scope,
                Opportunity.status.in_(CLOSED),
                Opportunity.closed_at >= lower,
                Opportunity.closed_at < upper,
            )
        )
    ).all()
    won_amounts: list[Decimal] = []
    won_days: list[float] = []
    lost_reasons: dict[str, int] = defaultdict(int)
    won = lost = 0
    for status, amount, opened_at, closed_at, reason_code, _owner in closed_rows:
        if status == OpportunityStatus.WON:
            won += 1
            if amount is not None:
                won_amounts.append(amount)
            if opened_at is not None and closed_at is not None:
                won_days.append((closed_at - opened_at).total_seconds() / 86400)
        else:
            lost += 1
            lost_reasons[reason_code or "other"] += 1
    win = SalesWin(
        closed=won + lost,
        won=won,
        lost=lost,
        win_rate=_rate(won, won + lost),
        avg_days=_avg_days(won_days),
        avg_amount=_avg(won_amounts) if show_amount else None,
    )
    reasons = [
        Bucket(key=code, label=settings.lost_reason_name(code) or code, count=n)
        for code, n in sorted(lost_reasons.items(), key=lambda item: -item[1])
    ]

    # 按负责人：新建（期间）、进行中（现在）、赢单 / 输单（期间关闭）、赢单金额、平均周期。
    owners: dict[uuid.UUID | None, dict[str, Any]] = defaultdict(
        lambda: {"created": 0, "active": 0, "won": 0, "lost": 0, "amount": Decimal(0), "days": []}
    )
    for o in created_rows:
        owners[o.owner_id]["created"] += 1
    for owner_id, n in await session.execute(
        select(Opportunity.owner_id, func.count())
        .where(scope, Opportunity.status == OpportunityStatus.ACTIVE)
        .group_by(Opportunity.owner_id)
    ):
        owners[owner_id]["active"] = int(n)
    for status, amount, opened_at, closed_at, _reason, owner_id in closed_rows:
        row = owners[owner_id]
        if status == OpportunityStatus.WON:
            row["won"] += 1
            row["amount"] += amount or 0
            if opened_at is not None and closed_at is not None:
                row["days"].append((closed_at - opened_at).total_seconds() / 86400)
        else:
            row["lost"] += 1
    names = dict(
        (
            await session.execute(
                select(Staff.id, Staff.display_name).where(
                    Staff.id.in_([k for k in owners if k is not None])
                )
            )
        ).all()
    )
    by_owner = sorted(
        (
            OwnerSales(
                staff_id=owner_id,
                name=names.get(owner_id, "员工") if owner_id else "没有负责人",
                created=row["created"],
                active=row["active"],
                won=row["won"],
                lost=row["lost"],
                won_amount=money(row["amount"]),
                avg_days=_avg_days(row["days"]),
            )
            for owner_id, row in owners.items()
        ),
        key=lambda item: (-item.won, -item.created, item.name),
    )

    # 按来源：期间内新建的数量和其中赢单的。
    sources: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "won": 0})
    for o in created_rows:
        sources[o.source]["count"] += 1
        if o.status == OpportunityStatus.WON:
            sources[o.source]["won"] += 1
    by_source = [
        SourceSales(
            key=source,
            label=SOURCE_LABELS.get(source, source),
            count=row["count"],
            won=row["won"],
            win_rate=_rate(row["won"], row["count"]),
        )
        for source, row in sorted(sources.items(), key=lambda item: -item[1]["count"])
    ]
    return SalesReport(
        start=start,
        end=end,
        amount_visible=show_amount,
        funnel=funnel,
        amount=amount_stats,
        win=win,
        lost_reasons=reasons,
        by_owner=by_owner,
        by_source=by_source,
    )
