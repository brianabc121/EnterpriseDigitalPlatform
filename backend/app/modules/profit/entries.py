"""收支登记（设计文档 §30.4）：费用和其他收入的登记、修改、删除，每月固定的一键登记。"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import NotFound, Unprocessable
from app.core.ids import new_id
from app.modules.audit.service import record_audit
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.profit import periods
from app.modules.profit.models import DEFAULT_CATEGORIES, KIND_LABELS, EntryKind, ProfitEntry
from app.modules.profit.periods import Period
from app.modules.profit.report import ZERO, money
from app.modules.profit.schemas import (
    CategoryOptions,
    CategoryTotal,
    CopyRecurringResult,
    EntryIn,
    EntryOut,
    EntryPage,
    RecurringPending,
)

EARLIEST = date(2000, 1, 1)
USED_CATEGORIES = 30


def check_day(day: date, today: date) -> None:
    """发生日期：2000 年以后，最晚到本月最后一天（每月固定的费用可以先登记）。"""
    if day < EARLIEST:
        raise Unprocessable("日期不能早于 2000 年")
    if day > periods.month_end(today):
        raise Unprocessable("日期最晚到本月最后一天")


async def _names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(ids)))
    return {staff_id: name for staff_id, name in rows}


def _out(entry: ProfitEntry, names: dict[uuid.UUID, str]) -> EntryOut:
    return EntryOut(
        id=entry.id,
        kind=entry.kind,
        kind_label=KIND_LABELS.get(entry.kind, entry.kind),
        category=entry.category,
        amount=money(entry.amount),
        occurred_on=entry.occurred_on,
        note=entry.note,
        recurring=entry.recurring,
        copied_from=entry.copied_from,
        created_by_name=names.get(entry.created_by) if entry.created_by else None,
        updated_by_name=names.get(entry.updated_by) if entry.updated_by else None,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


async def outs(session: AsyncSession, entries: list[ProfitEntry]) -> list[EntryOut]:
    ids = {e.created_by for e in entries if e.created_by} | {
        e.updated_by for e in entries if e.updated_by
    }
    names = await _names(session, ids)
    return [_out(e, names) for e in entries]


def _snapshot(entry: ProfitEntry) -> dict[str, Any]:
    return {
        "kind": entry.kind,
        "category": entry.category,
        "amount": str(money(entry.amount)),
        "occurred_on": entry.occurred_on.isoformat(),
        "note": entry.note,
        "recurring": entry.recurring,
    }


def _audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    entry_id: uuid.UUID | None,
    detail: dict[str, Any],
    ip: str | None,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="profit_entry",
        resource_id=str(entry_id) if entry_id else None,
        detail=detail,
        ip=ip,
    )


# ---- 每月固定 ----


def _pending_sources(month: date) -> Any:
    """上个月标了"每月固定"、还没有登记到 month 的收支。

    已经登记过的：有从它复制来的一笔；或者 month 里已经有同类型、同类别、同金额的一笔（员工手工
    登记过了，不再重复）。"""
    source_month = periods.add_months(month, -1)
    copy = aliased(ProfitEntry)
    same = aliased(ProfitEntry)
    return and_(
        ProfitEntry.recurring.is_(True),
        ProfitEntry.occurred_on >= source_month,
        ProfitEntry.occurred_on <= periods.month_end(source_month),
        ~exists().where(copy.copied_from == ProfitEntry.id),
        ~exists().where(
            same.kind == ProfitEntry.kind,
            same.category == ProfitEntry.category,
            same.amount == ProfitEntry.amount,
            same.occurred_on >= month,
            same.occurred_on <= periods.month_end(month),
        ),
    )


async def pending_recurring(session: AsyncSession, month: date) -> RecurringPending | None:
    sources = list(
        (
            await session.scalars(
                select(ProfitEntry)
                .where(_pending_sources(month))
                .order_by(ProfitEntry.occurred_on, ProfitEntry.created_at)
            )
        ).all()
    )
    if not sources:
        return None
    expense = sum((e.amount for e in sources if e.kind == EntryKind.EXPENSE), ZERO)
    income = sum((e.amount for e in sources if e.kind == EntryKind.INCOME), ZERO)
    return RecurringPending(
        month=periods.month_key(month),
        count=len(sources),
        expense=money(expense),
        income=money(income),
        categories=list(dict.fromkeys(e.category for e in sources)),
    )


async def copy_recurring(
    session: AsyncSession,
    principal: Principal,
    month: date,
    today: date,
    *,
    ip: str | None = None,
) -> CopyRecurringResult:
    """把上个月的每月固定收支登记到 month（同一天，没有这一天时用月底）。同一笔只复制一次。"""
    if month > today.replace(day=1):
        raise Unprocessable("只能登记到本月或以前的月份")
    sources = list(
        (
            await session.scalars(
                select(ProfitEntry)
                .where(_pending_sources(month))
                .order_by(ProfitEntry.occurred_on, ProfitEntry.created_at)
            )
        ).all()
    )
    created: list[uuid.UUID] = []
    last = periods.month_end(month)
    for source in sources:
        day = month.replace(day=min(source.occurred_on.day, last.day))
        entry_id = new_id()
        inserted = await session.scalar(
            insert(ProfitEntry)
            .values(
                id=entry_id,
                tenant_id=principal.tenant_id,
                kind=source.kind,
                category=source.category,
                amount=source.amount,
                occurred_on=day,
                note=source.note,
                recurring=True,
                copied_from=source.id,
                created_by=principal.staff_id,
                updated_by=principal.staff_id,
            )
            .on_conflict_do_nothing(
                index_elements=[ProfitEntry.tenant_id, ProfitEntry.copied_from],
                index_where=ProfitEntry.copied_from.is_not(None),
            )
            .returning(ProfitEntry.id)
        )
        if inserted is not None:
            created.append(inserted)
    if created:
        _audit(
            session,
            principal,
            "profit.entry_copy",
            None,
            {"month": periods.month_key(month), "count": len(created)},
            ip,
        )
    await session.commit()
    entries = list(
        (
            await session.scalars(
                select(ProfitEntry)
                .where(ProfitEntry.id.in_(created))
                .order_by(ProfitEntry.occurred_on, ProfitEntry.category)
            )
        ).all()
    )
    return CopyRecurringResult(created=len(entries), items=await outs(session, entries))


# ---- 列表与类别 ----


async def list_entries(
    session: AsyncSession,
    period: Period,
    today: date,
    *,
    kind: str | None = None,
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> EntryPage:
    where = [ProfitEntry.occurred_on >= period.start, ProfitEntry.occurred_on <= period.end]
    if kind:
        where.append(ProfitEntry.kind == kind)
    if category:
        where.append(ProfitEntry.category == category)
    total = int(
        await session.scalar(select(func.count()).select_from(ProfitEntry).where(*where)) or 0
    )
    entries = list(
        (
            await session.scalars(
                select(ProfitEntry)
                .where(*where)
                .order_by(
                    ProfitEntry.occurred_on.desc(),
                    ProfitEntry.created_at.desc(),
                    ProfitEntry.id,
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    amount = func.sum(ProfitEntry.amount)
    by_category = [
        CategoryTotal(kind=k, category=c, amount=money(a), count=int(n))
        for k, c, a, n in await session.execute(
            select(ProfitEntry.kind, ProfitEntry.category, amount, func.count())
            .where(*where)
            .group_by(ProfitEntry.kind, ProfitEntry.category)
            .order_by(ProfitEntry.kind, amount.desc(), ProfitEntry.category)
        )
    ]
    month = min(period.end, today).replace(day=1)
    return EntryPage(
        items=await outs(session, entries),
        total=total,
        expense_total=money(sum((c.amount for c in by_category if c.kind == "expense"), ZERO)),
        income_total=money(sum((c.amount for c in by_category if c.kind == "income"), ZERO)),
        by_category=by_category,
        recurring=await pending_recurring(session, month),
    )


async def categories(session: AsyncSession) -> CategoryOptions:
    """常用类别在前，后面是本企业用过的（最近用过的在前）。"""
    used: dict[str, list[str]] = {EntryKind.EXPENSE: [], EntryKind.INCOME: []}
    latest = func.max(ProfitEntry.updated_at)
    for kind, category in await session.execute(
        select(ProfitEntry.kind, ProfitEntry.category)
        .group_by(ProfitEntry.kind, ProfitEntry.category)
        .order_by(latest.desc())
        .limit(USED_CATEGORIES * 2)
    ):
        used.setdefault(kind, []).append(category)

    def merged(kind: str) -> list[str]:
        return list(dict.fromkeys([*DEFAULT_CATEGORIES[kind], *used.get(kind, [])]))

    return CategoryOptions(expense=merged(EntryKind.EXPENSE), income=merged(EntryKind.INCOME))


# ---- 登记、修改、删除 ----


async def get(session: AsyncSession, entry_id: uuid.UUID) -> ProfitEntry:
    entry = await session.get(ProfitEntry, entry_id)
    if entry is None:
        raise NotFound("这笔收支不存在")
    return entry


async def create(
    session: AsyncSession,
    principal: Principal,
    payload: EntryIn,
    today: date,
    *,
    ip: str | None = None,
) -> EntryOut:
    check_day(payload.occurred_on, today)
    entry = ProfitEntry(
        id=new_id(),
        tenant_id=principal.tenant_id,
        kind=payload.kind,
        category=payload.category,
        amount=payload.amount,
        occurred_on=payload.occurred_on,
        note=payload.note,
        recurring=payload.recurring,
        created_by=principal.staff_id,
        updated_by=principal.staff_id,
    )
    session.add(entry)
    await session.flush()
    _audit(session, principal, "profit.entry_create", entry.id, _snapshot(entry), ip)
    await session.commit()
    await session.refresh(entry)
    return (await outs(session, [entry]))[0]


async def update(
    session: AsyncSession,
    principal: Principal,
    entry_id: uuid.UUID,
    payload: EntryIn,
    today: date,
    *,
    ip: str | None = None,
) -> EntryOut:
    entry = await get(session, entry_id)
    check_day(payload.occurred_on, today)
    before = _snapshot(entry)
    entry.kind = payload.kind
    entry.category = payload.category
    entry.amount = payload.amount
    entry.occurred_on = payload.occurred_on
    entry.note = payload.note
    entry.recurring = payload.recurring
    entry.updated_by = principal.staff_id
    after = _snapshot(entry)
    changes = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    if changes:
        _audit(session, principal, "profit.entry_update", entry.id, {"changes": changes}, ip)
    await session.commit()
    await session.refresh(entry)
    return (await outs(session, [entry]))[0]


async def delete(
    session: AsyncSession,
    principal: Principal,
    entry_id: uuid.UUID,
    *,
    ip: str | None = None,
) -> None:
    entry = await get(session, entry_id)
    _audit(session, principal, "profit.entry_delete", entry.id, _snapshot(entry), ip)
    await session.delete(entry)
    await session.commit()
