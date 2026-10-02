"""意向客户（设计文档 §35）：查看范围、列表、转入、修改、跟进、成交、放弃、重新跟进，
AI 建议的确认和忽略。

- 意向客户跟着客户的可见范围：看得到这个客户，就能看到、跟进他的意向记录；跟进人总能看到自己跟进的；
- 把跟进人改成别人需要 customer:assign；
- 一个客户同时最多一条待确认或跟进中的意向记录（数据库的部分唯一索引）。
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, case, func, or_, select, true
from sqlalchemy import update as update_rows
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.customer import service as customer_service
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.orders.models import Order, OrderStatus
from app.modules.prospects import settings as prospect_settings
from app.modules.prospects.models import (
    OPEN_STATUSES,
    CustomerProspect,
    ProspectFollowup,
    ProspectLevel,
    ProspectSource,
    ProspectStatus,
)
from app.modules.prospects.schemas import (
    CustomerProspectInfo,
    FollowupCreate,
    ProspectBrief,
    ProspectCreate,
    ProspectFollowupOut,
    ProspectOut,
    ProspectPage,
    ProspectSummary,
    ProspectUpdate,
    ProspectView,
)
from app.modules.security.keys import TenantKeyring
from app.modules.todos import sla

NOT_FOUND = "意向记录不存在"
ALREADY = "这个客户已经在意向客户里了"
# 成交：这些状态的订单算成交（确认以后）。
DEAL_STATUSES = (
    OrderStatus.CONFIRMED,
    OrderStatus.FULFILLING,
    OrderStatus.SHIPPED,
    OrderStatus.COMPLETED,
)
VIEWS: tuple[str, ...] = ("active", "today", "overdue", "suggested", "won", "lost", "all")
LEVEL_RANK = {ProspectLevel.LOW: 0, ProspectLevel.MEDIUM: 1, ProspectLevel.HIGH: 2}
# 转入时提示"最近下过单"的天数。
RECENT_DEAL = timedelta(days=30)


async def today(session: AsyncSession) -> date:
    """企业时区（默认路由策略的工作时间）的今天。"""
    spec = await sla.business_hours(session)
    return datetime.now(UTC).astimezone(sla.tz_of(spec)).date()


async def month_start(session: AsyncSession) -> datetime:
    """企业时区本月 1 日 0 点。"""
    spec = await sla.business_hours(session)
    zone = sla.tz_of(spec)
    now = datetime.now(UTC).astimezone(zone)
    return datetime.combine(now.date().replace(day=1), time.min, zone)


def visible_to(principal: Principal) -> ColumnElement[bool]:
    """看得到这个客户就看得到他的意向记录（客户的数据范围，§13.2）；跟进人总能看到自己跟进的
    （网页访客的客户没有归属坐席，会话结束后接待的坐席就看不到这个客户了）。"""
    if principal.has(Permission.CUSTOMER_READ_ALL) or principal.has(Permission.SESSION_READ_ALL):
        return true()
    return or_(
        CustomerProspect.follower_id == principal.staff_id,
        CustomerProspect.customer_id.in_(
            select(Customer.id).where(customer_service.visible_to(principal))
        ),
    )


async def get_visible(
    session: AsyncSession, principal: Principal, prospect_id: uuid.UUID, *, lock: bool = False
) -> CustomerProspect:
    query = select(CustomerProspect).where(
        CustomerProspect.id == prospect_id,
        CustomerProspect.status != ProspectStatus.DISMISSED,
        visible_to(principal),
    )
    if lock:
        query = query.with_for_update()
    prospect = await session.scalar(query)
    if prospect is None:
        raise NotFound(NOT_FOUND)
    return prospect


def _audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    prospect: CustomerProspect,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer_prospect",
        resource_id=str(prospect.id),
        detail={"customer_id": str(prospect.customer_id), **(detail or {})},
        ip=ip,
    )


# ---- 输出 ----


async def _names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(ids)))
    return dict(rows.all())


async def summaries(
    session: AsyncSession, prospects: list[CustomerProspect], day: date
) -> list[ProspectSummary]:
    if not prospects:
        return []
    customers = {
        c.id: c
        for c in (
            await session.scalars(
                select(Customer).where(Customer.id.in_({p.customer_id for p in prospects}))
            )
        ).all()
    }
    orders = dict(
        (
            await session.execute(
                select(Order.id, Order.no).where(
                    Order.id.in_({p.order_id for p in prospects if p.order_id})
                )
            )
        ).all()
    )
    names = await _names(
        session,
        {p.follower_id for p in prospects if p.follower_id}
        | {p.created_by for p in prospects if p.created_by},
    )
    result = []
    for p in prospects:
        customer = customers.get(p.customer_id)
        active = p.status == ProspectStatus.ACTIVE
        result.append(
            ProspectSummary(
                id=p.id,
                customer_id=p.customer_id,
                customer_name=customer.display_name if customer else "客户",
                customer_company=customer.company if customer else None,
                status=p.status,
                level=p.level,
                interest=p.interest,
                concerns=p.concerns,
                source=p.source,
                session_id=p.session_id,
                follower_id=p.follower_id,
                follower_name=names.get(p.follower_id) if p.follower_id else None,
                next_follow_at=p.next_follow_at,
                last_followed_at=p.last_followed_at,
                follow_count=p.follow_count,
                order_id=p.order_id,
                order_no=orders.get(p.order_id) if p.order_id else None,
                lost_reason=p.lost_reason,
                created_by_name=names.get(p.created_by) if p.created_by else None,
                created_at=p.created_at,
                updated_at=p.updated_at,
                closed_at=p.closed_at,
                overdue=active and p.next_follow_at is not None and p.next_follow_at < day,
                due_today=active and p.next_follow_at == day,
            )
        )
    return result


async def out(
    session: AsyncSession, principal: Principal, prospect: CustomerProspect
) -> ProspectOut:
    day = await today(session)
    [summary] = await summaries(session, [prospect], day)
    rows = (
        await session.scalars(
            select(ProspectFollowup)
            .where(ProspectFollowup.prospect_id == prospect.id)
            .order_by(ProspectFollowup.created_at.desc(), ProspectFollowup.id.desc())
        )
    ).all()
    names = await _names(session, {r.staff_id for r in rows if r.staff_id})
    return ProspectOut(
        **summary.model_dump(),
        followups=[
            ProspectFollowupOut(
                id=r.id,
                method=r.method,
                content=r.content,
                next_follow_at=r.next_follow_at,
                staff_id=r.staff_id,
                staff_name=names.get(r.staff_id) if r.staff_id else None,
                session_id=r.session_id,
                created_at=r.created_at,
            )
            for r in rows
        ],
        can_assign=principal.has(Permission.CUSTOMER_ASSIGN),
    )


# ---- 列表 ----


def _view(view: str, day: date) -> ColumnElement[bool]:
    status = CustomerProspect.status
    if view == "active":
        return status == ProspectStatus.ACTIVE
    if view == "today":
        return and_(status == ProspectStatus.ACTIVE, CustomerProspect.next_follow_at == day)
    if view == "overdue":
        return and_(status == ProspectStatus.ACTIVE, CustomerProspect.next_follow_at < day)
    if view in ("suggested", "won", "lost"):
        return status == view
    return status != ProspectStatus.DISMISSED


async def list_prospects(
    session: AsyncSession,
    keys: TenantKeyring,
    principal: Principal,
    *,
    view: ProspectView,
    level: str | None = None,
    source: str | None = None,
    follower_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ProspectPage:
    day = await today(session)
    scope: list[ColumnElement[bool]] = [visible_to(principal)]
    if level:
        scope.append(CustomerProspect.level == level)
    if source:
        scope.append(CustomerProspect.source == source)
    if follower_id:
        scope.append(CustomerProspect.follower_id == follower_id)
    if customer_id:
        scope.append(CustomerProspect.customer_id == customer_id)
    if q and q.strip():
        match = await customer_service.search_condition(keys, principal.tenant_id, q)
        scope.append(CustomerProspect.customer_id.in_(select(Customer.id).where(match)))
    counts_row = (
        await session.execute(
            select(*(func.count().filter(_view(v, day)) for v in VIEWS)).where(*scope)
        )
    ).one()
    counts = dict(zip(VIEWS, (int(n or 0) for n in counts_row), strict=True))
    won_this_month = int(
        await session.scalar(
            select(func.count()).where(
                *scope,
                CustomerProspect.status == ProspectStatus.WON,
                CustomerProspect.closed_at >= await month_start(session),
            )
        )
        or 0
    )
    condition = and_(*scope, _view(view, day))
    # 跟进中的按下次跟进日期（早的在前），其他的按最近修改。
    order: list[Any] = (
        [CustomerProspect.next_follow_at.asc().nulls_last(), CustomerProspect.updated_at.desc()]
        if view in ("active", "today", "overdue")
        else [
            case((CustomerProspect.status.in_(OPEN_STATUSES), 0), else_=1),
            CustomerProspect.updated_at.desc(),
        ]
    )
    rows = (
        await session.scalars(
            select(CustomerProspect)
            .where(condition)
            .order_by(*order, CustomerProspect.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return ProspectPage(
        items=await summaries(session, list(rows), day),
        total=counts.get(view, 0),
        counts=counts,
        won_this_month=won_this_month,
    )


async def customer_info(
    session: AsyncSession, principal: Principal, customer_id: uuid.UUID
) -> CustomerProspectInfo:
    """客户资料里的意向：最近的一条（待确认、跟进中的优先），以及最近 30 天确认的订单。"""
    await customer_service.ensure_visible(session, principal, customer_id)
    recent_deal_at = await session.scalar(
        select(func.max(Order.confirmed_at)).where(
            Order.customer_id == customer_id,
            Order.status.in_(DEAL_STATUSES),
            Order.confirmed_at >= datetime.now(UTC) - RECENT_DEAL,
        )
    )
    return CustomerProspectInfo(
        prospect=await _brief(session, customer_id), recent_deal_at=recent_deal_at
    )


async def _brief(session: AsyncSession, customer_id: uuid.UUID) -> ProspectBrief | None:
    prospect = await session.scalar(
        select(CustomerProspect)
        .where(
            CustomerProspect.customer_id == customer_id,
            CustomerProspect.status != ProspectStatus.DISMISSED,
        )
        .order_by(
            case((CustomerProspect.status.in_(OPEN_STATUSES), 0), else_=1),
            CustomerProspect.updated_at.desc(),
        )
        .limit(1)
    )
    if prospect is None:
        return None
    day = await today(session)
    [summary] = await summaries(session, [prospect], day)
    return ProspectBrief(
        id=summary.id,
        status=summary.status,
        level=summary.level,
        next_follow_at=summary.next_follow_at,
        follower_name=summary.follower_name,
        overdue=summary.overdue,
    )


# ---- 转入和修改 ----


async def _follower(
    session: AsyncSession,
    principal: Principal,
    customer: Customer,
    follower_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """跟进人：不填时是客户的归属坐席（没有时是自己）；改成别人需要 customer:assign。"""
    if follower_id is None:
        return customer.owner_id or principal.staff_id
    if follower_id not in (principal.staff_id, customer.owner_id) and not principal.has(
        Permission.CUSTOMER_ASSIGN
    ):
        raise Forbidden("把跟进人改成别人需要分配客户的权限")
    staff = await session.get(Staff, follower_id)
    if staff is None or staff.status != StaffStatus.ACTIVE:
        raise Unprocessable("跟进人不存在或者已经停用")
    return follower_id


async def create(
    session: AsyncSession,
    principal: Principal,
    payload: ProspectCreate,
    *,
    ip: str | None = None,
) -> CustomerProspect:
    """员工把客户转入意向客户。"""
    customer = await customer_service.ensure_visible(session, principal, payload.customer_id)
    settings = await prospect_settings.load(session, principal.tenant_id)
    day = await today(session)
    if payload.next_follow_at is not None and payload.next_follow_at < day:
        raise Unprocessable("下次跟进日期不能早于今天")
    prospect = CustomerProspect(
        tenant_id=principal.tenant_id,
        customer_id=customer.id,
        status=ProspectStatus.ACTIVE,
        level=payload.level,
        interest=payload.interest,
        concerns=payload.concerns,
        source=ProspectSource.STAFF,
        follower_id=await _follower(session, principal, customer, payload.follower_id),
        next_follow_at=payload.next_follow_at or day + timedelta(days=settings.follow_days),
        created_by=principal.staff_id,
    )
    session.add(prospect)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict(ALREADY) from exc
    _audit(session, principal, "prospect.create", prospect, {"level": payload.level}, ip)
    await session.commit()
    await session.refresh(prospect)
    return prospect


async def update(
    session: AsyncSession,
    principal: Principal,
    prospect_id: uuid.UUID,
    payload: ProspectUpdate,
    *,
    ip: str | None = None,
) -> CustomerProspect:
    prospect = await get_visible(session, principal, prospect_id, lock=True)
    changes = payload.model_dump(exclude_unset=True)
    if "level" in changes and payload.level is not None:
        prospect.level = payload.level
    if "interest" in changes:
        prospect.interest = payload.interest
    if "concerns" in changes:
        prospect.concerns = payload.concerns
    if "next_follow_at" in changes:
        if payload.next_follow_at is not None and payload.next_follow_at < await today(session):
            raise Unprocessable("下次跟进日期不能早于今天")
        prospect.next_follow_at = payload.next_follow_at
    if "follower_id" in changes and payload.follower_id != prospect.follower_id:
        customer = await session.get(Customer, prospect.customer_id)
        assert customer is not None
        if payload.follower_id is None:
            if not principal.has(Permission.CUSTOMER_ASSIGN):
                raise Forbidden("把跟进人改成别人需要分配客户的权限")
            prospect.follower_id = None
        else:
            prospect.follower_id = await _follower(
                session, principal, customer, payload.follower_id
            )
        _audit(
            session,
            principal,
            "prospect.assign",
            prospect,
            {"follower_id": str(prospect.follower_id) if prospect.follower_id else None},
            ip,
        )
    prospect.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(prospect)
    return prospect


async def add_followup(
    session: AsyncSession,
    principal: Principal,
    prospect_id: uuid.UUID,
    payload: FollowupCreate,
) -> CustomerProspect:
    """记一次跟进：下次跟进日期不填时按默认天数。"""
    prospect = await get_visible(session, principal, prospect_id, lock=True)
    if prospect.status != ProspectStatus.ACTIVE:
        raise Conflict("只有跟进中的意向客户可以记跟进，先重新跟进")
    day = await today(session)
    if payload.next_follow_at is not None and payload.next_follow_at < day:
        raise Unprocessable("下次跟进日期不能早于今天")
    settings = await prospect_settings.load(session, principal.tenant_id)
    next_at = payload.next_follow_at or day + timedelta(days=settings.follow_days)
    now = datetime.now(UTC)
    session.add(
        ProspectFollowup(
            tenant_id=principal.tenant_id,
            prospect_id=prospect.id,
            method=payload.method,
            content=payload.content,
            next_follow_at=next_at,
            staff_id=principal.staff_id,
        )
    )
    prospect.next_follow_at = next_at
    prospect.last_followed_at = now
    prospect.follow_count += 1
    prospect.updated_at = now
    await session.commit()
    await session.refresh(prospect)
    return prospect


def _close(prospect: CustomerProspect, status: ProspectStatus, staff_id: uuid.UUID | None) -> None:
    now = datetime.now(UTC)
    prospect.status = status
    prospect.closed_by = staff_id
    prospect.closed_at = now
    prospect.updated_at = now


async def won(
    session: AsyncSession,
    principal: Principal,
    prospect_id: uuid.UUID,
    order_id: uuid.UUID | None,
    *,
    ip: str | None = None,
) -> CustomerProspect:
    """手动标记成交（可以关联订单）。"""
    prospect = await get_visible(session, principal, prospect_id, lock=True)
    if prospect.status != ProspectStatus.ACTIVE:
        raise Conflict("只有跟进中的意向客户可以标记成交")
    if order_id is not None:
        from app.modules.orders import service as order_service

        if not principal.has(Permission.ORDER_READ):
            raise Forbidden("没有查看订单的权限")
        order = await order_service.get_visible(session, principal, order_id)
        if order.customer_id != prospect.customer_id:
            raise Unprocessable("订单不是这个客户的")
        prospect.order_id = order.id
    _close(prospect, ProspectStatus.WON, principal.staff_id)
    _audit(session, principal, "prospect.won", prospect, {"order_id": str(order_id)}, ip)
    await session.commit()
    await session.refresh(prospect)
    return prospect


async def lost(
    session: AsyncSession,
    principal: Principal,
    prospect_id: uuid.UUID,
    reason: str,
    *,
    ip: str | None = None,
) -> CustomerProspect:
    prospect = await get_visible(session, principal, prospect_id, lock=True)
    if prospect.status != ProspectStatus.ACTIVE:
        raise Conflict("只有跟进中的意向客户可以放弃")
    prospect.lost_reason = reason
    _close(prospect, ProspectStatus.LOST, principal.staff_id)
    _audit(session, principal, "prospect.lost", prospect, {"reason": reason}, ip)
    await session.commit()
    await session.refresh(prospect)
    return prospect


async def reopen(
    session: AsyncSession,
    principal: Principal,
    prospect_id: uuid.UUID,
    next_follow_at: date | None,
    *,
    ip: str | None = None,
) -> CustomerProspect:
    """已成交、已放弃的重新跟进（这个客户没有别的待确认、跟进中的记录时）。"""
    prospect = await get_visible(session, principal, prospect_id, lock=True)
    if prospect.status not in (ProspectStatus.WON, ProspectStatus.LOST):
        raise Conflict("只有已成交、已放弃的意向客户可以重新跟进")
    day = await today(session)
    if next_follow_at is not None and next_follow_at < day:
        raise Unprocessable("下次跟进日期不能早于今天")
    settings = await prospect_settings.load(session, principal.tenant_id)
    now = datetime.now(UTC)
    prospect.status = ProspectStatus.ACTIVE
    prospect.next_follow_at = next_follow_at or day + timedelta(days=settings.follow_days)
    prospect.lost_reason = None
    prospect.closed_by = None
    prospect.closed_at = None
    prospect.opened_at = now
    prospect.updated_at = now
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict(ALREADY) from exc
    _audit(session, principal, "prospect.reopen", prospect, ip=ip)
    await session.commit()
    await session.refresh(prospect)
    return prospect


async def accept(
    session: AsyncSession, principal: Principal, prospect_id: uuid.UUID, *, ip: str | None = None
) -> CustomerProspect:
    """确认 AI 的建议：转入意向客户。"""
    prospect = await get_visible(session, principal, prospect_id, lock=True)
    if prospect.status != ProspectStatus.SUGGESTED:
        raise Conflict("只有 AI 建议的意向客户需要确认")
    prospect.status = ProspectStatus.ACTIVE
    if prospect.follower_id is None:
        prospect.follower_id = principal.staff_id
    prospect.updated_at = datetime.now(UTC)
    _audit(session, principal, "prospect.accept", prospect, ip=ip)
    await session.commit()
    await session.refresh(prospect)
    return prospect


async def dismiss(
    session: AsyncSession, principal: Principal, prospect_id: uuid.UUID, *, ip: str | None = None
) -> None:
    """忽略 AI 的建议：不在列表里显示，30 天内 AI 不再建议这个客户。"""
    prospect = await get_visible(session, principal, prospect_id, lock=True)
    if prospect.status != ProspectStatus.SUGGESTED:
        raise Conflict("只有 AI 建议的意向客户可以忽略")
    _close(prospect, ProspectStatus.DISMISSED, principal.staff_id)
    _audit(session, principal, "prospect.dismiss", prospect, ip=ip)
    await session.commit()


# ---- 成交 ----


async def order_confirmed(session: AsyncSession, order: Order) -> CustomerProspect | None:
    """订单确认后：这个客户跟进中的意向记录变成"已成交"（由调用方提交）。"""
    if order.customer_id is None:
        return None
    prospect = await session.scalar(
        select(CustomerProspect)
        .where(
            CustomerProspect.customer_id == order.customer_id,
            CustomerProspect.status.in_(OPEN_STATUSES),
        )
        .with_for_update()
    )
    if prospect is None:
        return None
    prospect.order_id = order.id
    _close(prospect, ProspectStatus.WON, None)
    if order.confirmed_at is not None:
        prospect.closed_at = order.confirmed_at
    return prospect


# ---- 合并客户 ----


async def merge_customers(
    session: AsyncSession, target_id: uuid.UUID, source_ids: list[uuid.UUID]
) -> int:
    """合并客户时（customer/privacy.py）：来源客户的意向记录并入目标客户（由调用方提交）。

    同一客户只能有一条待确认或跟进中的：留下一条（跟进中的优先，其次是目标客户的、最近修改的），
    其他的跟进记录并到这条上（想要什么、顾虑为空时补上，等级取高的，下次跟进取早的），然后删除。
    已成交、已放弃的直接改挂到目标客户。返回改挂和合并的条数。
    """
    rows = (
        await session.scalars(
            select(CustomerProspect)
            .where(CustomerProspect.customer_id.in_([target_id, *source_ids]))
            .with_for_update()
        )
    ).all()
    opened = sorted(
        (r for r in rows if r.status in OPEN_STATUSES),
        key=lambda r: (
            r.status != ProspectStatus.ACTIVE,
            r.customer_id != target_id,
            -r.updated_at.timestamp(),
        ),
    )
    for extra in opened[1:]:
        keep = opened[0]
        await session.execute(
            update_rows(ProspectFollowup)
            .where(ProspectFollowup.prospect_id == extra.id)
            .values(prospect_id=keep.id)
        )
        keep.interest = keep.interest or extra.interest
        keep.concerns = keep.concerns or extra.concerns
        if LEVEL_RANK[ProspectLevel(extra.level)] > LEVEL_RANK[ProspectLevel(keep.level)]:
            keep.level = extra.level
        if extra.next_follow_at is not None and (
            keep.next_follow_at is None or extra.next_follow_at < keep.next_follow_at
        ):
            keep.next_follow_at = extra.next_follow_at
        if extra.last_followed_at is not None and (
            keep.last_followed_at is None or extra.last_followed_at > keep.last_followed_at
        ):
            keep.last_followed_at = extra.last_followed_at
        keep.follow_count += extra.follow_count
        keep.follower_id = keep.follower_id or extra.follower_id
        await session.delete(extra)
    # 先删掉多出来的，再改挂（部分唯一索引）。
    await session.flush()
    result = await session.execute(
        update_rows(CustomerProspect)
        .where(CustomerProspect.customer_id.in_(source_ids))
        .values(customer_id=target_id)
    )
    return int(getattr(result, "rowcount", 0) or 0) + max(len(opened) - 1, 0)


def level_of(stage: int | None) -> ProspectLevel:
    """会话的最高意向 → 意向等级：准备下单为高，意向明确为中，其他为低。"""
    if stage is not None and stage >= 4:
        return ProspectLevel.HIGH
    if stage is not None and stage >= 3:
        return ProspectLevel.MEDIUM
    return ProspectLevel.LOW
