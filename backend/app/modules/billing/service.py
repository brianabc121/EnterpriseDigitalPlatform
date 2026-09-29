"""套餐、订阅与账单（设计文档 §7.2、§7.3）。

- 套餐由平台运营维护；租户开通（或自助注册）时按套餐开始试用或正式订阅。
- 试用转正式、升级、降级都是开始一个新订阅，当前订阅随之取消；续费是延长当前订阅。
- 调度任务每小时把过了到期日的订阅标记为已到期，宽限期（平台设置 tenant_policy.grace_days）
  过后停用租户；续费或开始新订阅后自动恢复。
- 账单按月生成：每天归属于覆盖这一天、最近创建的那条订阅，正式订阅按天折算月费；套餐允许
  超额（overage.policy 为 warn）时，超出额度的 AI 回复按条计费。一期只展示，线下收款后由运营
  标记已收款。
"""

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.dates import day_bounds, today
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.ids import new_id
from app.db.errors import violated_unique_constraint
from app.modules.audit.service import record_audit
from app.modules.billing import entitlements as ent
from app.modules.billing.models import (
    Invoice,
    InvoiceStatus,
    Plan,
    PlanStatus,
    Subscription,
    SubscriptionStatus,
)
from app.modules.billing.schemas import (
    BillingOverview,
    FeatureOut,
    InvoiceGenerateResult,
    InvoiceItem,
    InvoiceOut,
    LimitUsage,
    PlanCreate,
    PlanOut,
    PlanUpdate,
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionRenew,
    TenantBilling,
    TenantOverrides,
)
from app.modules.platform import settings as platform_settings
from app.modules.platform.schemas import TenantPolicy
from app.modules.tenancy.models import Tenant, TenantStatus

logger = logging.getLogger(__name__)

TENANT_POLICY = "tenant_policy"
DEFAULT_TRIAL_DAYS = 14
EXPIRY_NOTICE_DAYS = 7
# 用量达到额度的这个比例时提醒。
NEAR_LIMIT_RATIO = 0.8
# 因订阅到期被停用的租户：续费后自动恢复（运营手动停用的不会）。
SUSPENDED_REASON = "suspended_reason"
SUSPENDED_FOR_EXPIRY = "subscription_expired"
LIVE = (SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE)


def utcnow() -> datetime:
    return datetime.now(UTC)


async def tenant_policy(session: AsyncSession) -> TenantPolicy:
    return await platform_settings.read(session, TENANT_POLICY, TenantPolicy)


# ---- 套餐 ----


async def list_plans(session: AsyncSession, *, public_only: bool = False) -> list[Plan]:
    statement = select(Plan).order_by(Plan.sort, Plan.price_monthly, Plan.code)
    if public_only:
        statement = statement.where(Plan.public.is_(True), Plan.status == PlanStatus.ACTIVE)
    return list((await session.scalars(statement)).all())


async def plan_by_code(session: AsyncSession, code: str) -> Plan:
    plan = await session.scalar(select(Plan).where(Plan.code == code))
    if plan is None:
        raise Unprocessable(f"套餐不存在：{code}")
    if plan.status != PlanStatus.ACTIVE:
        raise Unprocessable(f"套餐已下架：{plan.name}")
    return plan


async def get_plan(session: AsyncSession, plan_id: uuid.UUID) -> Plan:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise NotFound("套餐不存在")
    return plan


async def create_plan(
    session: AsyncSession, payload: PlanCreate, *, actor_id: uuid.UUID, ip: str | None
) -> Plan:
    if await session.scalar(select(Plan.id).where(Plan.code == payload.code)):
        raise Conflict("套餐代码已存在")
    plan = Plan(
        id=new_id(),
        code=payload.code,
        name=payload.name.strip(),
        description=payload.description,
        price_monthly=payload.price_monthly,
        limits=payload.limits.model_dump(exclude_none=True),
        features=payload.features.model_dump(),
        overage=payload.overage.model_dump(),
        trial_days=payload.trial_days,
        public=payload.public,
        sort=payload.sort,
    )
    session.add(plan)
    record_audit(
        session,
        action="plan.create",
        actor_type="platform",
        actor_id=actor_id,
        resource_type="plan",
        resource_id=str(plan.id),
        detail=payload.model_dump(mode="json"),
        ip=ip,
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if violated_unique_constraint(exc) == "uq_plans_code":
            raise Conflict("套餐代码已存在") from exc
        raise
    await session.refresh(plan)
    return plan


async def update_plan(
    session: AsyncSession,
    plan_id: uuid.UUID,
    payload: PlanUpdate,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> Plan:
    plan = await get_plan(session, plan_id)
    changes = payload.model_dump(exclude_unset=True, mode="json")
    for field, value in changes.items():
        if value is None and field != "description":
            continue
        if field == "limits":
            value = {k: v for k, v in value.items() if v is not None}
        setattr(plan, field, value)
    if changes:
        record_audit(
            session,
            action="plan.update",
            actor_type="platform",
            actor_id=actor_id,
            resource_type="plan",
            resource_id=str(plan.id),
            detail=changes,
            ip=ip,
        )
        await session.commit()
        await session.refresh(plan)
    return plan


# ---- 订阅 ----


def _plan_names(plans: list[Plan]) -> dict[uuid.UUID, Plan]:
    return {plan.id: plan for plan in plans}


def subscription_out(sub: Subscription, plan: Plan | None) -> SubscriptionOut:
    return SubscriptionOut(
        id=sub.id,
        plan_id=sub.plan_id,
        plan_code=plan.code if plan else "",
        plan_name=plan.name if plan else "",
        status=sub.status,
        period_start=sub.period_start,
        period_end=sub.period_end,
        note=sub.note,
        created_at=sub.created_at,
    )


async def list_subscriptions(session: AsyncSession, tenant_id: uuid.UUID) -> list[SubscriptionOut]:
    subs = (
        await session.scalars(
            select(Subscription)
            .where(Subscription.tenant_id == tenant_id)
            .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        )
    ).all()
    plans = _plan_names(await list_plans(session))
    return [subscription_out(s, plans.get(s.plan_id)) for s in subs]


def _reactivate(
    session: AsyncSession,
    tenant: Tenant,
    *,
    actor_type: str,
    actor_id: uuid.UUID | None,
    ip: str | None,
) -> None:
    """订阅恢复后，重新启用因到期被停用的租户。"""
    settings = dict(tenant.settings or {})
    if tenant.status != TenantStatus.SUSPENDED or settings.get(SUSPENDED_REASON) != (
        SUSPENDED_FOR_EXPIRY
    ):
        return
    settings.pop(SUSPENDED_REASON, None)
    tenant.settings = settings
    tenant.status = TenantStatus.ACTIVE
    record_audit(
        session,
        action="tenant.reactivate",
        actor_type=actor_type,
        actor_id=actor_id,
        tenant_id=tenant.id,
        resource_type="tenant",
        resource_id=str(tenant.id),
        detail={"reason": "subscription_renewed"},
        ip=ip,
    )


async def start_subscription(
    session: AsyncSession,
    tenant: Tenant,
    payload: SubscriptionCreate,
    *,
    actor_type: str,
    actor_id: uuid.UUID | None,
    ip: str | None,
    current_day: date,
) -> Subscription:
    """开始一个新订阅（不提交）：当前的试用或正式订阅随之取消，截止到新订阅开始的前一天。"""
    plan = await plan_by_code(session, payload.plan_code)
    start = payload.period_start or current_day
    if payload.period_end is not None:
        end = payload.period_end
    elif payload.status == "trial" and payload.months is None:
        end = start + timedelta(days=(plan.trial_days or DEFAULT_TRIAL_DAYS) - 1)
    else:
        end = ent.add_months(start, payload.months or 1) - timedelta(days=1)
    if end < start:
        raise Unprocessable("到期日不能早于开始日期")
    if end < current_day:
        raise Unprocessable("订阅的到期日已经过去")
    current = await ent.latest_subscription(session, tenant.id)
    replaced = None
    if current is not None and current.status in LIVE:
        current.status = SubscriptionStatus.CANCELLED
        current.period_end = max(
            current.period_start, min(current.period_end, start - timedelta(days=1))
        )
        replaced = str(current.id)
    sub = Subscription(
        id=new_id(),
        tenant_id=tenant.id,
        plan_id=plan.id,
        status=payload.status,
        period_start=start,
        period_end=end,
        note=payload.note,
        created_by=actor_id,
    )
    session.add(sub)
    _reactivate(session, tenant, actor_type=actor_type, actor_id=actor_id, ip=ip)
    record_audit(
        session,
        action="subscription.create",
        actor_type=actor_type,
        actor_id=actor_id,
        tenant_id=tenant.id,
        resource_type="subscription",
        resource_id=str(sub.id),
        detail={
            "plan": plan.code,
            "status": payload.status,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "replaced": replaced,
            "note": payload.note,
        },
        ip=ip,
    )
    return sub


async def _tenant_subscription(
    session: AsyncSession, tenant: Tenant, subscription_id: uuid.UUID
) -> Subscription:
    sub = await session.get(Subscription, subscription_id, with_for_update=True)
    if sub is None or sub.tenant_id != tenant.id:
        raise NotFound("订阅不存在")
    return sub


async def renew_subscription(
    session: AsyncSession,
    tenant: Tenant,
    subscription_id: uuid.UUID,
    payload: SubscriptionRenew,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
    current_day: date,
) -> Subscription:
    """续订当前订阅：从原到期日起延长（不提交）。已到期或已取消的订阅续订后恢复为正式。"""
    sub = await _tenant_subscription(session, tenant, subscription_id)
    latest = await ent.latest_subscription(session, tenant.id)
    if latest is None or latest.id != sub.id:
        raise Conflict("只能续订当前订阅")
    if payload.period_end is not None:
        end = payload.period_end
    else:
        end = ent.add_months(sub.period_end + timedelta(days=1), payload.months or 1)
        end -= timedelta(days=1)
    if end <= sub.period_end:
        raise Unprocessable("新的到期日必须晚于原到期日")
    if end < current_day:
        raise Unprocessable("续订后仍然已经到期，请增加续订的月数")
    previous = sub.period_end
    sub.period_end = end
    if sub.status in (SubscriptionStatus.EXPIRED, SubscriptionStatus.CANCELLED):
        sub.status = SubscriptionStatus.ACTIVE
    if payload.note:
        sub.note = payload.note
    _reactivate(session, tenant, actor_type="platform", actor_id=actor_id, ip=ip)
    record_audit(
        session,
        action="subscription.renew",
        actor_type="platform",
        actor_id=actor_id,
        tenant_id=tenant.id,
        resource_type="subscription",
        resource_id=str(sub.id),
        detail={"from": previous.isoformat(), "to": end.isoformat(), "note": payload.note},
        ip=ip,
    )
    return sub


async def cancel_subscription(
    session: AsyncSession,
    tenant: Tenant,
    subscription_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
    current_day: date,
) -> Subscription:
    """取消订阅（不提交）：截止到今天，宽限期过后停用租户。"""
    sub = await _tenant_subscription(session, tenant, subscription_id)
    if sub.status not in LIVE:
        raise Conflict("订阅已经结束")
    sub.status = SubscriptionStatus.CANCELLED
    sub.period_end = max(sub.period_start, min(sub.period_end, current_day))
    record_audit(
        session,
        action="subscription.cancel",
        actor_type="platform",
        actor_id=actor_id,
        tenant_id=tenant.id,
        resource_type="subscription",
        resource_id=str(sub.id),
        detail={"period_end": sub.period_end.isoformat()},
        ip=ip,
    )
    return sub


def set_overrides(
    session: AsyncSession,
    tenant: Tenant,
    payload: TenantOverrides,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> None:
    """替换租户的额度与功能覆盖值（不提交）。早期的 ai_monthly_quota 设置并入 limits。"""
    settings = {k: v for k, v in (tenant.settings or {}).items() if k != "ai_monthly_quota"}
    settings["limits"] = dict(payload.limits)
    settings["features"] = dict(payload.features)
    tenant.settings = settings
    record_audit(
        session,
        action="tenant.overrides",
        actor_type="platform",
        actor_id=actor_id,
        tenant_id=tenant.id,
        resource_type="tenant",
        resource_id=str(tenant.id),
        detail=payload.model_dump(mode="json"),
        ip=ip,
    )


# ---- 到期与停用 ----


@dataclass
class LifecycleReport:
    expired: int = 0
    suspended: int = 0


async def run_lifecycle(ctx: AppContext, *, now: datetime | None = None) -> LifecycleReport:
    """调度任务：标记到期的订阅；宽限期过后停用租户。"""
    report = LifecycleReport()
    current = today(ZoneInfo(ctx.settings.usage_timezone), now)
    async with ctx.db.platform_sessionmaker() as session:
        grace = (await tenant_policy(session)).grace_days
        due = (
            await session.scalars(
                select(Subscription).where(
                    Subscription.status.in_(LIVE), Subscription.period_end < current
                )
            )
        ).all()
        for sub in due:
            sub.status = SubscriptionStatus.EXPIRED
            record_audit(
                session,
                action="subscription.expire",
                actor_type="system",
                tenant_id=sub.tenant_id,
                resource_type="subscription",
                resource_id=str(sub.id),
                detail={"period_end": sub.period_end.isoformat()},
            )
            report.expired += 1
        await session.commit()

        latest = (
            select(Subscription.tenant_id, Subscription.status, Subscription.period_end)
            .order_by(
                Subscription.tenant_id, Subscription.created_at.desc(), Subscription.id.desc()
            )
            .ext(distinct_on(Subscription.tenant_id))
            .subquery()
        )
        tenants = (
            await session.scalars(
                select(Tenant)
                .join(latest, latest.c.tenant_id == Tenant.id)
                .where(
                    Tenant.status == TenantStatus.ACTIVE,
                    latest.c.status.not_in([s.value for s in LIVE]),
                    latest.c.period_end < current - timedelta(days=grace),
                )
            )
        ).all()
        for tenant in tenants:
            tenant.status = TenantStatus.SUSPENDED
            tenant.settings = {**(tenant.settings or {}), SUSPENDED_REASON: SUSPENDED_FOR_EXPIRY}
            record_audit(
                session,
                action="tenant.suspend",
                actor_type="system",
                tenant_id=tenant.id,
                resource_type="tenant",
                resource_id=str(tenant.id),
                detail={"reason": SUSPENDED_FOR_EXPIRY, "grace_days": grace},
            )
            report.suspended += 1
        await session.commit()
    if report.expired or report.suspended:
        logger.info("billing lifecycle: %s", report)
    return report


# ---- 概览 ----


def billing_notice(
    sub: Subscription | None, limits: list[LimitUsage], current_day: date
) -> tuple[int | None, str | None]:
    days_left = None
    notice = None
    if sub is not None:
        days_left = max(0, (sub.period_end - current_day).days + 1)
        end = sub.period_end.isoformat()
        if sub.status == SubscriptionStatus.TRIAL:
            notice = f"试用期剩余 {days_left} 天（{end} 到期），请联系平台开通正式套餐"
        elif sub.status == SubscriptionStatus.ACTIVE and days_left <= EXPIRY_NOTICE_DAYS:
            notice = f"套餐将于 {end} 到期（剩余 {days_left} 天），请联系平台续费"
        elif sub.status not in LIVE:
            notice = f"套餐已于 {end} 到期，宽限期结束后将停止服务，请尽快联系平台续费"
    if notice is None:
        full = [x for x in limits if x.limit is not None and x.used >= x.limit]
        near = [x for x in limits if x.limit and NEAR_LIMIT_RATIO * x.limit <= x.used < x.limit]
        if full:
            notice = "、".join(x.label for x in full) + "已达到套餐上限"
        elif near:
            notice = "、".join(x.label for x in near) + "即将达到套餐上限"
    return days_left, notice


async def overview(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    tz: ZoneInfo,
    *,
    now: datetime | None = None,
) -> BillingOverview:
    entitled = await ent.entitlements(session, tenant_id)
    overrides = entitled.overrides
    limits = [
        LimitUsage(
            key=key,
            label=label,
            unit=unit,
            limit=entitled.limit(key),
            used=await ent.used(session, tenant_id, key, tz, now),
            overridden=key in overrides["limits"],
        )
        for key, (label, unit) in ent.LIMITS.items()
    ]
    features = [
        FeatureOut(
            key=key, label=label, enabled=entitled.has(key), overridden=key in overrides["features"]
        )
        for key, label in ent.FEATURES.items()
    ]
    sub = entitled.subscription
    days_left, notice = billing_notice(sub, limits, today(tz, now))
    return BillingOverview(
        metered=sub is not None,
        plan=PlanOut.of(entitled.plan) if entitled.plan else None,
        subscription=subscription_out(sub, entitled.plan) if sub else None,
        days_left=days_left,
        notice=notice,
        limits=limits,
        features=features,
        overage_policy=str(entitled.overage.get("policy") or "degrade"),
        ai_reply_price=entitled.ai_reply_price,
    )


async def tenant_billing(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    tz: ZoneInfo,
    *,
    now: datetime | None = None,
) -> TenantBilling:
    base = await overview(session, tenant_id, tz, now=now)
    entitled = await ent.entitlements(session, tenant_id)
    return TenantBilling(
        **base.model_dump(),
        subscriptions=await list_subscriptions(session, tenant_id),
        invoices=await list_invoices(session, tenant_id=tenant_id),
        overrides=TenantOverrides.model_validate(entitled.overrides),
    )


# ---- 账单 ----


def invoice_out(invoice: Invoice, tenant: Tenant | None = None) -> InvoiceOut:
    return InvoiceOut(
        id=invoice.id,
        tenant_id=invoice.tenant_id,
        tenant_code=tenant.code if tenant else None,
        tenant_name=tenant.name if tenant else None,
        number=invoice.number,
        period_start=invoice.period_start,
        period_end=invoice.period_end,
        plan_name=invoice.plan_name,
        items=[InvoiceItem.model_validate(item) for item in invoice.items or []],
        amount=invoice.amount,
        status=invoice.status,
        issued_at=invoice.issued_at,
        paid_at=invoice.paid_at,
        note=invoice.note,
    )


async def list_invoices(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None = None,
    status: str | None = None,
    month: date | None = None,
    limit: int = 200,
) -> list[InvoiceOut]:
    statement = (
        select(Invoice, Tenant)
        .join(Tenant, Tenant.id == Invoice.tenant_id)
        .order_by(Invoice.period_start.desc(), Invoice.number)
        .limit(limit)
    )
    if tenant_id is not None:
        statement = statement.where(Invoice.tenant_id == tenant_id)
    if status:
        statement = statement.where(Invoice.status == status)
    if month is not None:
        statement = statement.where(Invoice.period_start == ent.month_start(month))
    rows = (await session.execute(statement)).all()
    return [invoice_out(invoice, tenant) for invoice, tenant in rows]


async def update_invoice(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    status: str | None,
    note: str | None,
    note_set: bool,
    actor_id: uuid.UUID,
    ip: str | None,
) -> InvoiceOut:
    invoice = await session.get(Invoice, invoice_id, with_for_update=True)
    if invoice is None:
        raise NotFound("账单不存在")
    detail: dict[str, Any] = {}
    if status and status != invoice.status:
        if invoice.status == InvoiceStatus.VOID:
            raise Conflict("账单已作废")
        detail["status"] = {"from": invoice.status, "to": status}
        invoice.status = status
        invoice.paid_at = utcnow() if status == InvoiceStatus.PAID else None
    if note_set:
        invoice.note = note
        detail["note"] = note
    if detail:
        record_audit(
            session,
            action="invoice.update",
            actor_type="platform",
            actor_id=actor_id,
            tenant_id=invoice.tenant_id,
            resource_type="invoice",
            resource_id=str(invoice.id),
            detail=detail,
            ip=ip,
        )
        await session.commit()
        await session.refresh(invoice)
    tenant = await session.get(Tenant, invoice.tenant_id)
    return invoice_out(invoice, tenant)


def _winners(subs: list[Subscription], first: date, last: date) -> dict[date, Subscription]:
    """每一天归属的订阅：覆盖这一天、最近创建的那条。"""
    ordered = sorted(subs, key=lambda s: (s.created_at, s.id), reverse=True)
    days: dict[date, Subscription] = {}
    day = first
    while day <= last:
        for sub in ordered:
            if sub.period_start <= day <= sub.period_end:
                days[day] = sub
                break
        day += timedelta(days=1)
    return days


async def compute_invoice(
    session: AsyncSession, tenant: Tenant, month: date, tz: ZoneInfo
) -> tuple[list[InvoiceItem], Plan | None]:
    """一个租户一个月的账单明细。"""
    first, last = ent.month_start(month), ent.month_end(month)
    subs = list(
        (
            await session.scalars(
                select(Subscription).where(
                    Subscription.tenant_id == tenant.id,
                    Subscription.period_start <= last,
                    Subscription.period_end >= first,
                )
            )
        ).all()
    )
    if not subs:
        return [], None
    plans = _plan_names(await list_plans(session))
    winners = _winners(subs, first, last)
    days_in_month = (last - first).days + 1
    billed: dict[uuid.UUID, list[date]] = defaultdict(list)
    for day, sub in sorted(winners.items()):
        if sub.status != SubscriptionStatus.TRIAL:
            billed[sub.id].append(day)
    items: list[InvoiceItem] = []
    by_id = {s.id: s for s in subs}
    for sub_id, days in billed.items():
        plan = plans.get(by_id[sub_id].plan_id)
        if plan is None or plan.price_monthly <= 0:
            continue
        amount = round(plan.price_monthly * len(days) / days_in_month)
        items.append(
            InvoiceItem(
                kind="plan",
                description=f"{plan.name}（{days[0].isoformat()} ~ {days[-1].isoformat()}）",
                quantity=len(days),
                unit="天",
                unit_price=None,
                amount=amount,
            )
        )
    last_sub = winners[max(winners)] if winners else None
    plan = plans.get(last_sub.plan_id) if last_sub else None
    if last_sub is not None and last_sub.status != SubscriptionStatus.TRIAL:
        entitled = ent.resolve(plan, last_sub, tenant.settings)
        quota = entitled.limit("ai_replies_monthly")
        if entitled.overage_allowed and entitled.ai_reply_price > 0 and quota is not None:
            start, _ = day_bounds(first, tz)
            _, end = day_bounds(last, tz)
            over = await ent.ai_replies(session, tenant.id, start, end) - quota
            if over > 0:
                items.append(
                    InvoiceItem(
                        kind="ai_overage",
                        description=f"AI 回复超出每月额度（{quota} 条）",
                        quantity=over,
                        unit="条",
                        unit_price=entitled.ai_reply_price,
                        amount=over * entitled.ai_reply_price,
                    )
                )
    return items, plan


async def generate_invoices(
    ctx: AppContext, month: date, *, only: uuid.UUID | None = None
) -> InvoiceGenerateResult:
    """生成（或重算未收款的）某月账单。金额为 0 的月份不出账单。"""
    tz = ZoneInfo(ctx.settings.usage_timezone)
    first, last = ent.month_start(month), ent.month_end(month)
    result = InvoiceGenerateResult(created=0, updated=0, unchanged=0)
    async with ctx.db.platform_sessionmaker() as session:
        statement = select(Tenant).order_by(Tenant.created_at)
        if only is not None:
            statement = statement.where(Tenant.id == only)
        tenants = (await session.scalars(statement)).all()
    for tenant in tenants:
        try:
            async with ctx.db.platform_sessionmaker() as session:
                items, plan = await compute_invoice(session, tenant, first, tz)
                amount = sum(item.amount for item in items)
                existing = await session.scalar(
                    select(Invoice)
                    .where(Invoice.tenant_id == tenant.id, Invoice.period_start == first)
                    .with_for_update()
                )
                data = [item.model_dump() for item in items]
                if existing is None:
                    if amount <= 0:
                        continue
                    session.add(
                        Invoice(
                            id=new_id(),
                            tenant_id=tenant.id,
                            number=f"INV-{first:%Y%m}-{tenant.code}",
                            period_start=first,
                            period_end=last,
                            plan_id=plan.id if plan else None,
                            plan_name=plan.name if plan else "",
                            items=data,
                            amount=amount,
                        )
                    )
                    result.created += 1
                elif existing.status == InvoiceStatus.ISSUED and (
                    existing.amount != amount or existing.items != data
                ):
                    existing.items = data
                    existing.amount = amount
                    existing.plan_id = plan.id if plan else None
                    existing.plan_name = plan.name if plan else ""
                    result.updated += 1
                else:
                    result.unchanged += 1
                await session.commit()
        except Exception:
            logger.exception("invoice generation failed for tenant %s", tenant.id)
    return result


async def run_invoices(ctx: AppContext, *, now: datetime | None = None) -> InvoiceGenerateResult:
    """调度任务：生成上个月的账单（重复执行只重算未收款的账单）。"""
    current = today(ZoneInfo(ctx.settings.usage_timezone), now)
    previous = ent.month_start(current) - timedelta(days=1)
    return await generate_invoices(ctx, previous)
