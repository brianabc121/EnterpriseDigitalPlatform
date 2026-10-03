"""商机（设计文档 §40）：阶段、查看范围、列表和看板、新建、修改、跟进、换阶段、赢单、输单、
重新跟进、换负责人，AI 建议的确认和忽略，时间线。

- 商机跟着客户的可见范围：看得到这个客户，就能看到、跟进他的商机；负责人总能看到自己负责的；
- 把负责人改成别人、商机设置和阶段需要 opportunity:assign；
- 一个客户同时最多一条待确认或跟进中的商机（数据库的部分唯一索引）。
"""

import secrets
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, and_, case, func, or_, select, true
from sqlalchemy import update as update_rows
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.contracts.models import Contract
from app.modules.customer import service as customer_service
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.integration import outbox as webhook_outbox
from app.modules.integration.models import WebhookEventType
from app.modules.notifications import service as notifications
from app.modules.opportunities import settings as opportunity_settings
from app.modules.opportunities.models import (
    DEFAULT_STAGES,
    OPEN_STATUSES,
    ActivityKind,
    FollowMethod,
    Opportunity,
    OpportunityActivity,
    OpportunityLevel,
    OpportunitySource,
    OpportunityStatus,
    PipelineStage,
    StageKind,
)
from app.modules.opportunities.schemas import (
    BoardColumn,
    CustomerOpportunityInfo,
    FollowupCreate,
    NextStep,
    OpportunityActivityOut,
    OpportunityBoard,
    OpportunityBrief,
    OpportunityCreate,
    OpportunityOut,
    OpportunityPage,
    OpportunityStats,
    OpportunitySummary,
    OpportunityUpdate,
    OpportunityView,
    ProductRef,
    StageCreate,
    StageMove,
    StageOut,
    StageUpdate,
    TodoBrief,
)
from app.modules.opportunities.settings import OpportunitySettings
from app.modules.orders.models import Order, OrderStatus
from app.modules.routing.models import SkillGroupMember
from app.modules.security.keys import TenantKeyring
from app.modules.todos import sla
from app.modules.todos.models import ActorType, Todo, TodoSource, TodoStatus, TodoType

if TYPE_CHECKING:
    from app.context import AppContext

NOT_FOUND = "商机不存在"
ALREADY = "这个客户已经有一条进行中的商机了"
STAGE_NOT_FOUND = "阶段不存在"
# 成交：这些状态的订单算成交（确认以后）。
DEAL_STATUSES = (
    OrderStatus.CONFIRMED,
    OrderStatus.FULFILLING,
    OrderStatus.SHIPPED,
    OrderStatus.COMPLETED,
)
VIEWS: tuple[str, ...] = (
    "active",
    "mine",
    "today",
    "week",
    "overdue",
    "closing",
    "stale",
    "suggested",
    "won",
    "lost",
    "all",
)
LEVEL_RANK = {OpportunityLevel.LOW: 0, OpportunityLevel.MEDIUM: 1, OpportunityLevel.HIGH: 2}
# 转入时提示"最近下过单"的天数。
RECENT_DEAL = timedelta(days=30)
# 看板一列最多先加载多少条；赢单、输单两列只显示最近 30 天的。
BOARD_LIMIT = 200
CLOSED_LIMIT = 50
CLOSED_WINDOW = timedelta(days=30)
NAME_MAX = 128


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


# ---- 阶段 ----


async def stages(session: AsyncSession, tenant_id: uuid.UUID) -> list[PipelineStage]:
    """租户的阶段，按先后排列；还没有时写入默认的六个。"""
    rows = (
        await session.scalars(
            select(PipelineStage)
            .where(PipelineStage.tenant_id == tenant_id)
            .order_by(PipelineStage.position, PipelineStage.created_at)
        )
    ).all()
    if rows:
        return list(rows)
    return await seed_stages(session, tenant_id)


async def seed_stages(session: AsyncSession, tenant_id: uuid.UUID) -> list[PipelineStage]:
    """开通企业时写入默认阶段（由调用方提交）；已有阶段时不动。"""
    existing = (
        await session.scalars(
            select(PipelineStage)
            .where(PipelineStage.tenant_id == tenant_id)
            .order_by(PipelineStage.position)
        )
    ).all()
    if existing:
        return list(existing)
    rows = [
        PipelineStage(
            tenant_id=tenant_id,
            code=spec.code,
            name=spec.name,
            position=position,
            kind=spec.kind,
            probability=spec.probability,
            stale_days=spec.stale_days,
            color=spec.color,
        )
        for position, spec in enumerate(DEFAULT_STAGES)
    ]
    session.add_all(rows)
    await session.flush()
    return rows


def stage_out(stage: PipelineStage) -> StageOut:
    return StageOut(
        id=stage.id,
        code=stage.code,
        name=stage.name,
        position=stage.position,
        kind=stage.kind,
        probability=stage.probability,
        stale_days=stage.stale_days,
        color=stage.color,
    )


def open_stages(all_stages: list[PipelineStage]) -> list[PipelineStage]:
    return [s for s in all_stages if s.kind == StageKind.OPEN]


def stage_of_kind(all_stages: list[PipelineStage], kind: StageKind) -> PipelineStage:
    for stage in all_stages:
        if stage.kind == kind:
            return stage
    raise Unprocessable("阶段设置不完整，请检查商机设置")


def stage_by_code(all_stages: list[PipelineStage], code: str | None) -> PipelineStage | None:
    if not code:
        return None
    return next((s for s in all_stages if s.code == code), None)


def stage_by_id(all_stages: list[PipelineStage], stage_id: uuid.UUID) -> PipelineStage:
    for stage in all_stages:
        if stage.id == stage_id:
            return stage
    raise NotFound(STAGE_NOT_FOUND)


def _renumber(all_stages: list[PipelineStage]) -> None:
    """进行中的阶段按先后编号，赢单、输单排在最后。"""
    ordered = [
        *sorted(open_stages(all_stages), key=lambda s: (s.position, s.created_at)),
        *[s for s in all_stages if s.kind == StageKind.WON],
        *[s for s in all_stages if s.kind == StageKind.LOST],
    ]
    for position, stage in enumerate(ordered):
        stage.position = position


def _stage_audit(
    session: AsyncSession, principal: Principal, action: str, stage: PipelineStage, ip: str | None
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="pipeline_stage",
        resource_id=str(stage.id),
        detail={"code": stage.code, "name": stage.name, "kind": stage.kind},
        ip=ip,
    )


async def create_stage(
    session: AsyncSession, principal: Principal, payload: StageCreate, *, ip: str | None = None
) -> PipelineStage:
    """新增一个进行中的阶段：放在 after_id 后面，不填时放在最后一个进行中的阶段后面。"""
    all_stages = await stages(session, principal.tenant_id)
    opens = open_stages(all_stages)
    if len(opens) >= 12:
        raise Unprocessable("进行中的阶段最多 12 个")
    stage = PipelineStage(
        tenant_id=principal.tenant_id,
        code=f"stage_{secrets.token_hex(3)}",
        name=payload.name,
        kind=StageKind.OPEN,
        probability=payload.probability,
        stale_days=payload.stale_days,
        color=payload.color,
    )
    index = len(opens)
    if payload.after_id is not None:
        after = stage_by_id(all_stages, payload.after_id)
        if after.kind != StageKind.OPEN:
            raise Unprocessable("只能放在进行中的阶段后面")
        index = opens.index(after) + 1
    opens.insert(index, stage)
    for position, item in enumerate(opens):
        item.position = position
    session.add(stage)
    _renumber([*opens, *[s for s in all_stages if s.kind != StageKind.OPEN]])
    await session.flush()
    _stage_audit(session, principal, "opportunity.stage_create", stage, ip)
    await session.commit()
    await session.refresh(stage)
    return stage


async def update_stage(
    session: AsyncSession,
    principal: Principal,
    stage_id: uuid.UUID,
    payload: StageUpdate,
    *,
    ip: str | None = None,
) -> PipelineStage:
    all_stages = await stages(session, principal.tenant_id)
    stage = stage_by_id(all_stages, stage_id)
    changes = payload.model_dump(exclude_unset=True)
    if payload.name is not None:
        stage.name = payload.name
    if payload.probability is not None:
        stage.probability = payload.probability
    if payload.clear_stale_days:
        stage.stale_days = None
    elif payload.stale_days is not None:
        stage.stale_days = payload.stale_days
    if "color" in changes:
        stage.color = payload.color
    stage.updated_at = datetime.now(UTC)
    _stage_audit(session, principal, "opportunity.stage_update", stage, ip)
    await session.commit()
    await session.refresh(stage)
    return stage


async def delete_stage(
    session: AsyncSession,
    principal: Principal,
    stage_id: uuid.UUID,
    merge_into: uuid.UUID | None,
    *,
    ip: str | None = None,
) -> None:
    """删除一个进行中的阶段：还有商机时要指定并到哪个阶段；自动推进里指向它的规则清掉。"""
    all_stages = await stages(session, principal.tenant_id)
    stage = stage_by_id(all_stages, stage_id)
    if stage.kind != StageKind.OPEN:
        raise Unprocessable("赢单、输单的阶段不能删除")
    if len(open_stages(all_stages)) <= 1:
        raise Unprocessable("至少要保留一个进行中的阶段")
    count = int(
        await session.scalar(
            select(func.count()).where(
                Opportunity.stage_id == stage.id,
                Opportunity.status != OpportunityStatus.DISMISSED,
            )
        )
        or 0
    )
    if count:
        if merge_into is None or merge_into == stage.id:
            raise Unprocessable(f"这个阶段还有 {count} 条商机，请选择并到哪个阶段")
        target = stage_by_id(all_stages, merge_into)
        if target.kind != StageKind.OPEN:
            raise Unprocessable("只能并到进行中的阶段")
        await session.execute(
            update_rows(Opportunity)
            .where(Opportunity.stage_id == stage.id)
            .values(stage_id=target.id, updated_at=datetime.now(UTC))
        )
    else:
        await session.execute(
            update_rows(Opportunity)
            .where(Opportunity.stage_id == stage.id)
            .values(stage_id=open_stages(all_stages)[0].id)
        )
    settings = await opportunity_settings.load(session, principal.tenant_id)
    rules = settings.auto_advance
    changed = False
    for field in ("first_followup", "quote", "contract_final"):
        if getattr(rules, field) == stage.code:
            setattr(rules, field, None)
            changed = True
    if changed:
        await opportunity_settings.save(session, principal.tenant_id, settings, principal.staff_id)
    _stage_audit(session, principal, "opportunity.stage_delete", stage, ip)
    await session.delete(stage)
    await session.flush()
    _renumber([s for s in all_stages if s.id != stage.id])
    await session.commit()


async def reorder_stages(
    session: AsyncSession, principal: Principal, ids: list[uuid.UUID], *, ip: str | None = None
) -> list[PipelineStage]:
    all_stages = await stages(session, principal.tenant_id)
    opens = {s.id: s for s in open_stages(all_stages)}
    if set(ids) != set(opens) or len(ids) != len(opens):
        raise Unprocessable("请列出全部进行中的阶段")
    for position, stage_id in enumerate(ids):
        opens[stage_id].position = position
    _renumber(all_stages)
    record_audit(
        session,
        action="opportunity.stage_order",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="pipeline_stage",
        detail={"ids": [str(i) for i in ids]},
        ip=ip,
    )
    await session.commit()
    return await stages(session, principal.tenant_id)


# ---- 查看范围 ----


def visible_to(principal: Principal) -> ColumnElement[bool]:
    """看得到这个客户就看得到他的商机（客户的数据范围，§13.2）；负责人总能看到自己负责的
    （网页访客的客户没有归属坐席，会话结束后接待的坐席就看不到这个客户了）。"""
    if (
        principal.has(Permission.OPPORTUNITY_READ_ALL)
        or principal.has(Permission.CUSTOMER_READ_ALL)
        or principal.has(Permission.SESSION_READ_ALL)
    ):
        return true()
    return or_(
        Opportunity.owner_id == principal.staff_id,
        Opportunity.customer_id.in_(
            select(Customer.id).where(customer_service.visible_to(principal))
        ),
    )


async def get_visible(
    session: AsyncSession, principal: Principal, opportunity_id: uuid.UUID, *, lock: bool = False
) -> Opportunity:
    query = select(Opportunity).where(
        Opportunity.id == opportunity_id,
        Opportunity.status != OpportunityStatus.DISMISSED,
        visible_to(principal),
    )
    if lock:
        query = query.with_for_update()
    opportunity = await session.scalar(query)
    if opportunity is None:
        raise NotFound(NOT_FOUND)
    return opportunity


def _audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    opportunity: Opportunity,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="opportunity",
        resource_id=str(opportunity.id),
        detail={"customer_id": str(opportunity.customer_id), **(detail or {})},
        ip=ip,
    )


def emit(
    session: AsyncSession,
    opportunity: Opportunity,
    event: WebhookEventType,
    data: dict[str, Any] | None = None,
    *,
    actor_type: str = "staff",
) -> None:
    """同一个事务里写入推送事件（企业系统对接，§25.8）。"""
    webhook_outbox.emit(
        session,
        tenant_id=opportunity.tenant_id,
        event=event,
        resource_type="opportunity",
        resource_id=opportunity.id,
        data={"status": opportunity.status, **(data or {})},
        actor_type=actor_type,
    )


def _owner_notice(principal: Principal, opportunity: Opportunity) -> tuple[str, str, str]:
    """负责人变了：提醒新负责人的标题、内容和链接。"""
    return (
        f"商机「{opportunity.name}」交给你负责",
        f"{principal.display_name} 把这条商机交给你负责",
        f"/opportunities?id={opportunity.id}",
    )


async def _push_owner(ctx: "AppContext", principal: Principal, opportunity: Opportunity) -> None:
    """站内信在事务里已经写了；提交后再经企业微信、公司助理送到新负责人手上。"""
    from app.modules.notifications.push import notify_staff

    if opportunity.owner_id is None:
        return
    title, body, link = _owner_notice(principal, opportunity)
    await notify_staff(
        ctx, principal.tenant_id, [opportunity.owner_id], title=title, description=body, path=link
    )


# ---- 时间线 ----


def record(
    session: AsyncSession,
    opportunity: Opportunity,
    kind: ActivityKind,
    title: str | None,
    *,
    staff_id: uuid.UUID | None = None,
    content: str | None = None,
    method: FollowMethod = FollowMethod.OTHER,
    next_follow_at: date | None = None,
    properties: dict[str, Any] | None = None,
    linked_type: str | None = None,
    linked_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> OpportunityActivity:
    """在时间线上记一条，并更新商机的最近动态时间（由调用方提交）。"""
    now = at or datetime.now(UTC)
    row = OpportunityActivity(
        tenant_id=opportunity.tenant_id,
        opportunity_id=opportunity.id,
        kind=kind,
        title=title[:200] if title else None,
        method=method,
        content=content,
        next_follow_at=next_follow_at,
        properties=properties or {},
        linked_type=linked_type,
        linked_id=linked_id,
        staff_id=staff_id,
        session_id=session_id,
        created_at=now,
    )
    session.add(row)
    opportunity.last_activity_at = now
    opportunity.updated_at = now
    return row


# ---- 输出 ----


async def _names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(ids)))
    return dict(rows.all())


def amount_visible(principal: Principal, settings: OpportunitySettings) -> bool:
    """预计金额对这位员工可见（§40.10）：设置为"全部"，或者能分配商机。"""
    return settings.amount_visibility == "all" or principal.has(Permission.OPPORTUNITY_ASSIGN)


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(value).quantize(Decimal("0.01"))


def _products(raw: list[dict[str, Any]] | None) -> list[ProductRef]:
    result = []
    for item in raw or []:
        try:
            result.append(ProductRef.model_validate(item))
        except ValueError:
            continue
    return result


def is_stale(opportunity: Opportunity, stage: PipelineStage, now: datetime) -> bool:
    """跟进中、在当前阶段超过阶段的停滞天数没有任何动态。"""
    if opportunity.status != OpportunityStatus.ACTIVE or not stage.stale_days:
        return False
    last = max(
        opportunity.stage_entered_at, opportunity.last_activity_at or opportunity.stage_entered_at
    )
    return now - last > timedelta(days=stage.stale_days)


async def summaries(
    session: AsyncSession,
    opportunities: list[Opportunity],
    day: date,
    *,
    all_stages: list[PipelineStage],
    settings: OpportunitySettings,
    show_amount: bool,
) -> list[OpportunitySummary]:
    if not opportunities:
        return []
    now = datetime.now(UTC)
    by_id = {s.id: s for s in all_stages}
    customers = {
        c.id: c
        for c in (
            await session.scalars(
                select(Customer).where(Customer.id.in_({o.customer_id for o in opportunities}))
            )
        ).all()
    }
    order_ids = {o.order_id for o in opportunities if o.order_id}
    orders = (
        dict(
            (await session.execute(select(Order.id, Order.no).where(Order.id.in_(order_ids)))).all()
        )
        if order_ids
        else {}
    )
    contract_ids = {o.contract_id for o in opportunities if o.contract_id}
    contracts = (
        dict(
            (
                await session.execute(
                    select(Contract.id, Contract.no).where(Contract.id.in_(contract_ids))
                )
            ).all()
        )
        if contract_ids
        else {}
    )
    names = await _names(
        session,
        {o.owner_id for o in opportunities if o.owner_id}
        | {o.created_by for o in opportunities if o.created_by},
    )
    result = []
    for o in opportunities:
        customer = customers.get(o.customer_id)
        stage = by_id.get(o.stage_id)
        if stage is None:
            stage = all_stages[0]
        active = o.status == OpportunityStatus.ACTIVE
        result.append(
            OpportunitySummary(
                id=o.id,
                customer_id=o.customer_id,
                customer_name=customer.display_name if customer else "客户",
                customer_company=customer.company if customer else None,
                name=o.name,
                status=o.status,
                stage_id=stage.id,
                stage_code=stage.code,
                stage_name=stage.name,
                stage_kind=stage.kind,
                level=o.level,
                interest=o.interest,
                concerns=o.concerns,
                source=o.source,
                session_id=o.session_id,
                owner_id=o.owner_id,
                owner_name=names.get(o.owner_id) if o.owner_id else None,
                next_follow_at=o.next_follow_at,
                last_followed_at=o.last_followed_at,
                follow_count=o.follow_count,
                amount=_money(o.amount) if show_amount else None,
                expected_close_at=o.expected_close_at,
                probability=o.probability if o.probability is not None else stage.probability,
                stage_entered_at=o.stage_entered_at,
                days_in_stage=max((now - o.stage_entered_at).days, 0),
                stale=is_stale(o, stage, now),
                last_activity_at=o.last_activity_at,
                products=_products(o.products),
                order_id=o.order_id,
                order_no=orders.get(o.order_id) if o.order_id else None,
                contract_id=o.contract_id,
                contract_no=contracts.get(o.contract_id) if o.contract_id else None,
                lost_reason_code=o.lost_reason_code,
                lost_reason_name=settings.lost_reason_name(o.lost_reason_code),
                lost_reason=o.lost_reason,
                created_by_name=names.get(o.created_by) if o.created_by else None,
                created_at=o.created_at,
                updated_at=o.updated_at,
                closed_at=o.closed_at,
                overdue=active and o.next_follow_at is not None and o.next_follow_at < day,
                due_today=active and o.next_follow_at == day,
            )
        )
    return result


async def activities(
    session: AsyncSession, opportunity_id: uuid.UUID
) -> list[OpportunityActivityOut]:
    rows = (
        await session.scalars(
            select(OpportunityActivity)
            .where(OpportunityActivity.opportunity_id == opportunity_id)
            .order_by(OpportunityActivity.created_at.desc(), OpportunityActivity.id.desc())
        )
    ).all()
    names = await _names(session, {r.staff_id for r in rows if r.staff_id})
    return [
        OpportunityActivityOut(
            id=r.id,
            kind=r.kind,
            title=r.title,
            method=r.method,
            content=r.content,
            next_follow_at=r.next_follow_at,
            properties=r.properties or {},
            linked_type=r.linked_type,
            linked_id=r.linked_id,
            staff_id=r.staff_id,
            staff_name=names.get(r.staff_id) if r.staff_id else None,
            session_id=r.session_id,
            created_at=r.created_at,
        )
        for r in rows
    ]


async def out(
    session: AsyncSession, principal: Principal, opportunity: Opportunity
) -> OpportunityOut:
    settings = await opportunity_settings.load(session, principal.tenant_id)
    show_amount = amount_visible(principal, settings)
    [summary] = await summaries(
        session,
        [opportunity],
        await today(session),
        all_stages=await stages(session, principal.tenant_id),
        settings=settings,
        show_amount=show_amount,
    )
    return OpportunityOut(
        **summary.model_dump(),
        activities=await activities(session, opportunity.id),
        todos=await open_todos(session, opportunity.id),
        can_manage=principal.has(Permission.OPPORTUNITY_MANAGE),
        can_assign=principal.has(Permission.OPPORTUNITY_ASSIGN),
        amount_visible=show_amount,
    )


# ---- 列表、看板、数字 ----


def _stale_condition() -> ColumnElement[bool]:
    """跟进中、在当前阶段超过阶段的停滞天数没有动态（要连接 PipelineStage）。"""
    last = func.greatest(
        Opportunity.stage_entered_at,
        func.coalesce(Opportunity.last_activity_at, Opportunity.stage_entered_at),
    )
    return and_(
        Opportunity.status == OpportunityStatus.ACTIVE,
        PipelineStage.stale_days.is_not(None),
        last < func.now() - func.make_interval(0, 0, 0, PipelineStage.stale_days),
    )


def _month_range(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


def _view(view: str, day: date, principal: Principal) -> ColumnElement[bool]:
    status = Opportunity.status
    if view == "active":
        return status == OpportunityStatus.ACTIVE
    if view == "mine":
        return and_(status == OpportunityStatus.ACTIVE, Opportunity.owner_id == principal.staff_id)
    if view == "today":
        return and_(status == OpportunityStatus.ACTIVE, Opportunity.next_follow_at == day)
    if view == "week":
        # 本周（到周日）要跟进的，包括今天。
        return and_(
            status == OpportunityStatus.ACTIVE,
            Opportunity.next_follow_at >= day,
            Opportunity.next_follow_at <= day + timedelta(days=6 - day.weekday()),
        )
    if view == "overdue":
        return and_(status == OpportunityStatus.ACTIVE, Opportunity.next_follow_at < day)
    if view == "closing":
        start, end = _month_range(day)
        return and_(
            status == OpportunityStatus.ACTIVE,
            Opportunity.expected_close_at >= start,
            Opportunity.expected_close_at < end,
        )
    if view == "stale":
        return _stale_condition()
    if view in ("suggested", "won", "lost"):
        return status == view
    return status != OpportunityStatus.DISMISSED


def _base() -> Any:
    return select(Opportunity).join(PipelineStage, PipelineStage.id == Opportunity.stage_id)


async def _scope(
    keys: TenantKeyring,
    principal: Principal,
    *,
    stage_id: uuid.UUID | None = None,
    level: str | None = None,
    source: str | None = None,
    owner_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    close_month: str | None = None,
    q: str | None = None,
) -> list[ColumnElement[bool]]:
    scope: list[ColumnElement[bool]] = [visible_to(principal)]
    if stage_id:
        scope.append(Opportunity.stage_id == stage_id)
    if level:
        scope.append(Opportunity.level == level)
    if source:
        scope.append(Opportunity.source == source)
    if owner_id:
        scope.append(Opportunity.owner_id == owner_id)
    if customer_id:
        scope.append(Opportunity.customer_id == customer_id)
    if amount_min is not None:
        scope.append(Opportunity.amount >= amount_min)
    if amount_max is not None:
        scope.append(Opportunity.amount <= amount_max)
    if close_month:
        try:
            first = datetime.strptime(close_month, "%Y-%m").date()
        except ValueError as exc:
            raise Unprocessable("预计成交月份的格式是 YYYY-MM") from exc
        start, end = _month_range(first)
        scope.append(
            and_(Opportunity.expected_close_at >= start, Opportunity.expected_close_at < end)
        )
    if q and q.strip():
        match = await customer_service.search_condition(keys, principal.tenant_id, q)
        pattern = (
            "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        scope.append(
            or_(
                Opportunity.name.ilike(pattern, escape="\\"),
                Opportunity.customer_id.in_(select(Customer.id).where(match)),
            )
        )
    return scope


async def list_opportunities(
    session: AsyncSession,
    keys: TenantKeyring,
    principal: Principal,
    *,
    view: OpportunityView,
    stage_id: uuid.UUID | None = None,
    level: str | None = None,
    source: str | None = None,
    owner_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    amount_min: Decimal | None = None,
    amount_max: Decimal | None = None,
    close_month: str | None = None,
    stale: bool = False,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> OpportunityPage:
    day = await today(session)
    settings = await opportunity_settings.load(session, principal.tenant_id)
    all_stages = await stages(session, principal.tenant_id)
    scope = await _scope(
        keys,
        principal,
        stage_id=stage_id,
        level=level,
        source=source,
        owner_id=owner_id,
        customer_id=customer_id,
        amount_min=amount_min,
        amount_max=amount_max,
        close_month=close_month,
        q=q,
    )
    if stale:
        scope.append(_stale_condition())
    counts_row = (
        await session.execute(
            select(*(func.count().filter(_view(v, day, principal)) for v in VIEWS))
            .select_from(Opportunity)
            .join(PipelineStage, PipelineStage.id == Opportunity.stage_id)
            .where(*scope)
        )
    ).one()
    counts = dict(zip(VIEWS, (int(n or 0) for n in counts_row), strict=True))
    won_this_month = int(
        await session.scalar(
            select(func.count()).where(
                *scope,
                Opportunity.status == OpportunityStatus.WON,
                Opportunity.closed_at >= await month_start(session),
            )
        )
        or 0
    )
    condition = and_(*scope, _view(view, day, principal))
    # 跟进中的按下次跟进日期（早的在前），本月预计成交的按预计成交日，其他的按最近修改。
    order: list[Any]
    if view in ("active", "mine", "today", "overdue", "stale"):
        order = [Opportunity.next_follow_at.asc().nulls_last(), Opportunity.updated_at.desc()]
    elif view == "closing":
        order = [Opportunity.expected_close_at.asc(), Opportunity.updated_at.desc()]
    else:
        order = [
            case((Opportunity.status.in_(OPEN_STATUSES), 0), else_=1),
            Opportunity.updated_at.desc(),
        ]
    rows = (
        await session.scalars(
            _base().where(condition).order_by(*order, Opportunity.id).limit(limit).offset(offset)
        )
    ).all()
    show_amount = amount_visible(principal, settings)
    return OpportunityPage(
        items=await summaries(
            session,
            list(rows),
            day,
            all_stages=all_stages,
            settings=settings,
            show_amount=show_amount,
        ),
        total=counts.get(view, 0),
        counts=counts,
        won_this_month=won_this_month,
        amount_visible=show_amount,
    )


async def board(
    session: AsyncSession,
    keys: TenantKeyring,
    principal: Principal,
    *,
    level: str | None = None,
    source: str | None = None,
    owner_id: uuid.UUID | None = None,
    q: str | None = None,
    mine: bool = False,
) -> OpportunityBoard:
    """看板：每个进行中的阶段一列（待确认的也在它的阶段里），赢单、输单两列只显示最近 30 天的。"""
    day = await today(session)
    settings = await opportunity_settings.load(session, principal.tenant_id)
    all_stages = await stages(session, principal.tenant_id)
    scope = await _scope(keys, principal, level=level, source=source, owner_id=owner_id, q=q)
    if mine:
        scope.append(Opportunity.owner_id == principal.staff_id)
    show_amount = amount_visible(principal, settings)
    now = datetime.now(UTC)
    columns = []
    for stage in all_stages:
        condition = [*scope, Opportunity.stage_id == stage.id]
        if stage.kind == StageKind.OPEN:
            condition.append(Opportunity.status.in_(OPEN_STATUSES))
            limit = BOARD_LIMIT
            # 同一列里按位置，再按下次跟进日期，早转入的在前。
            order = [
                Opportunity.position,
                Opportunity.next_follow_at.asc().nulls_last(),
                Opportunity.created_at.asc(),
            ]
            item_condition = condition
        else:
            condition.append(Opportunity.status == stage.kind)
            limit = CLOSED_LIMIT
            order = [Opportunity.closed_at.desc()]
            item_condition = [*condition, Opportunity.closed_at >= now - CLOSED_WINDOW]
        total, amount_sum = (
            await session.execute(
                select(func.count(), func.sum(Opportunity.amount)).where(*condition)
            )
        ).one()
        rows = (
            await session.scalars(
                select(Opportunity)
                .where(*item_condition)
                .order_by(*order, Opportunity.id)
                .limit(limit + 1)
            )
        ).all()
        columns.append(
            BoardColumn(
                stage=stage_out(stage),
                total=int(total or 0),
                amount_sum=_money(amount_sum) if show_amount else None,
                items=await summaries(
                    session,
                    list(rows[:limit]),
                    day,
                    all_stages=all_stages,
                    settings=settings,
                    show_amount=show_amount,
                ),
                truncated=len(rows) > limit,
            )
        )
    return OpportunityBoard(columns=columns, amount_visible=show_amount)


async def stats(session: AsyncSession, principal: Principal) -> OpportunityStats:
    """顶部数字：进行中、我负责的、今天该跟进、本周要跟进、已逾期、停滞、待确认、本月赢单（数量和
    金额）。首页的四个数字（§40.8）也从这里取。"""
    day = await today(session)
    settings = await opportunity_settings.load(session, principal.tenant_id)
    scope = [visible_to(principal)]
    keys = ("active", "mine", "today", "week", "overdue", "stale", "suggested")
    row = (
        await session.execute(
            select(*(func.count().filter(_view(v, day, principal)) for v in keys))
            .select_from(Opportunity)
            .join(PipelineStage, PipelineStage.id == Opportunity.stage_id)
            .where(*scope)
        )
    ).one()
    counts = dict(zip(keys, (int(n or 0) for n in row), strict=True))
    won_count, won_amount = (
        await session.execute(
            select(func.count(), func.sum(Opportunity.amount)).where(
                *scope,
                Opportunity.status == OpportunityStatus.WON,
                Opportunity.closed_at >= await month_start(session),
            )
        )
    ).one()
    show_amount = amount_visible(principal, settings)
    return OpportunityStats(
        **counts,
        won_this_month=int(won_count or 0),
        won_amount_this_month=_money(won_amount) if show_amount else None,
    )


# ---- 客户资料 ----


async def customer_info(
    session: AsyncSession, principal: Principal, customer_id: uuid.UUID
) -> CustomerOpportunityInfo:
    """客户资料里的商机：最近的一条（待确认、跟进中的优先），以及最近 30 天确认的订单。"""
    await customer_service.ensure_visible(session, principal, customer_id)
    recent_deal_at = await session.scalar(
        select(func.max(Order.confirmed_at)).where(
            Order.customer_id == customer_id,
            Order.status.in_(DEAL_STATUSES),
            Order.confirmed_at >= datetime.now(UTC) - RECENT_DEAL,
        )
    )
    return CustomerOpportunityInfo(
        opportunity=await _brief(session, principal, customer_id), recent_deal_at=recent_deal_at
    )


async def _brief(
    session: AsyncSession, principal: Principal, customer_id: uuid.UUID
) -> OpportunityBrief | None:
    opportunity = await session.scalar(
        select(Opportunity)
        .where(
            Opportunity.customer_id == customer_id,
            Opportunity.status != OpportunityStatus.DISMISSED,
        )
        .order_by(
            case((Opportunity.status.in_(OPEN_STATUSES), 0), else_=1),
            Opportunity.updated_at.desc(),
        )
        .limit(1)
    )
    if opportunity is None:
        return None
    settings = await opportunity_settings.load(session, principal.tenant_id)
    [summary] = await summaries(
        session,
        [opportunity],
        await today(session),
        all_stages=await stages(session, principal.tenant_id),
        settings=settings,
        show_amount=amount_visible(principal, settings),
    )
    return OpportunityBrief(
        id=summary.id,
        name=summary.name,
        status=summary.status,
        stage_name=summary.stage_name,
        stage_kind=summary.stage_kind,
        level=summary.level,
        amount=summary.amount,
        next_follow_at=summary.next_follow_at,
        owner_name=summary.owner_name,
        overdue=summary.overdue,
    )


# ---- 新建和修改 ----


async def _owner(
    session: AsyncSession,
    principal: Principal,
    customer: Customer,
    owner_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """负责人：不填时是客户的归属坐席（没有时是自己）；改成别人需要 opportunity:assign。"""
    if owner_id is None:
        return customer.owner_id or principal.staff_id
    if owner_id not in (principal.staff_id, customer.owner_id) and not principal.has(
        Permission.OPPORTUNITY_ASSIGN
    ):
        raise Forbidden("把负责人改成别人需要分配商机的权限")
    staff = await session.get(Staff, owner_id)
    if staff is None or staff.status != StaffStatus.ACTIVE:
        raise Unprocessable("负责人不存在或者已经停用")
    return owner_id


def default_name(name: str | None, interest: str | None, customer: Customer) -> str:
    """商机名称：不填时按想要什么的前 40 字，再没有时是"客户称呼 的商机"。"""
    if name:
        return name[:NAME_MAX]
    if interest and interest.strip():
        return interest.strip()[:40]
    return f"{customer.display_name} 的商机"[:NAME_MAX]


async def create(
    session: AsyncSession,
    principal: Principal,
    payload: OpportunityCreate,
    *,
    ip: str | None = None,
) -> Opportunity:
    """员工新建商机（把客户转入）。"""
    customer = await customer_service.ensure_visible(session, principal, payload.customer_id)
    settings = await opportunity_settings.load(session, principal.tenant_id)
    all_stages = await stages(session, principal.tenant_id)
    day = await today(session)
    if payload.next_follow_at is not None and payload.next_follow_at < day:
        raise Unprocessable("下次跟进日期不能早于今天")
    stage = (
        stage_by_id(all_stages, payload.stage_id)
        if payload.stage_id
        else open_stages(all_stages)[0]
    )
    if stage.kind != StageKind.OPEN:
        raise Unprocessable("新建的商机只能放在进行中的阶段")
    now = datetime.now(UTC)
    opportunity = Opportunity(
        tenant_id=principal.tenant_id,
        customer_id=customer.id,
        name=default_name(payload.name, payload.interest, customer),
        stage_id=stage.id,
        status=OpportunityStatus.ACTIVE,
        level=payload.level,
        interest=payload.interest,
        concerns=payload.concerns,
        source=OpportunitySource.STAFF,
        owner_id=await _owner(session, principal, customer, payload.owner_id),
        next_follow_at=payload.next_follow_at or day + timedelta(days=settings.follow_days),
        amount=payload.amount,
        expected_close_at=payload.expected_close_at,
        products=[p.model_dump(mode="json") for p in payload.products],
        stage_entered_at=now,
        last_activity_at=now,
        created_by=principal.staff_id,
    )
    session.add(opportunity)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict(ALREADY) from exc
    record(
        session,
        opportunity,
        ActivityKind.CREATED,
        f"转入商机，阶段：{stage.name}",
        staff_id=principal.staff_id,
        properties={"stage": stage.code, "stage_name": stage.name},
        at=now,
    )
    _audit(session, principal, "opportunity.create", opportunity, {"level": payload.level}, ip)
    emit(session, opportunity, WebhookEventType.OPPORTUNITY_CREATED, {"stage": stage.code})
    await session.commit()
    await session.refresh(opportunity)
    return opportunity


def _field_title(field: str, before: Any, after: Any) -> str:
    labels = {"amount": "预计金额", "expected_close_at": "预计成交日"}

    def show(value: Any) -> str:
        return "（空）" if value is None else str(value)

    return f"{labels[field]}：{show(before)} → {show(after)}"


async def _change_owner(
    session: AsyncSession,
    principal: Principal,
    opportunity: Opportunity,
    owner_id: uuid.UUID | None,
    ip: str | None,
) -> None:
    customer = await session.get(Customer, opportunity.customer_id)
    assert customer is not None
    if owner_id is None:
        if not principal.has(Permission.OPPORTUNITY_ASSIGN):
            raise Forbidden("把负责人改成别人需要分配商机的权限")
        opportunity.owner_id = None
    else:
        opportunity.owner_id = await _owner(session, principal, customer, owner_id)
    names = await _names(session, {opportunity.owner_id} if opportunity.owner_id else set())
    who = names.get(opportunity.owner_id, "") if opportunity.owner_id else "（没有负责人）"
    record(
        session,
        opportunity,
        ActivityKind.OWNER,
        f"负责人改为 {who}",
        staff_id=principal.staff_id,
        properties={"owner_id": str(opportunity.owner_id) if opportunity.owner_id else None},
    )
    _audit(
        session,
        principal,
        "opportunity.assign",
        opportunity,
        {"owner_id": str(opportunity.owner_id) if opportunity.owner_id else None},
        ip,
    )
    emit(
        session,
        opportunity,
        WebhookEventType.OPPORTUNITY_ASSIGNED,
        {"owner_id": str(opportunity.owner_id) if opportunity.owner_id else None},
    )
    # 交给别人时提醒新负责人（§40.6）。
    if opportunity.owner_id is not None and opportunity.owner_id != principal.staff_id:
        title, body, link = _owner_notice(principal, opportunity)
        notifications.add(
            session,
            principal.tenant_id,
            [opportunity.owner_id],
            kind="opportunity",
            title=title,
            body=body,
            link=link,
        )


async def update(
    session: AsyncSession,
    principal: Principal,
    opportunity_id: uuid.UUID,
    payload: OpportunityUpdate,
    *,
    ip: str | None = None,
    ctx: "AppContext | None" = None,
) -> Opportunity:
    opportunity = await get_visible(session, principal, opportunity_id, lock=True)
    owner_before = opportunity.owner_id
    changes = payload.model_dump(exclude_unset=True)
    if payload.name is not None:
        opportunity.name = payload.name[:NAME_MAX]
    if payload.level is not None:
        opportunity.level = payload.level
    if "interest" in changes:
        opportunity.interest = payload.interest
    if "concerns" in changes:
        opportunity.concerns = payload.concerns
    if "next_follow_at" in changes:
        if payload.next_follow_at is not None and payload.next_follow_at < await today(session):
            raise Unprocessable("下次跟进日期不能早于今天")
        opportunity.next_follow_at = payload.next_follow_at
    if "probability" in changes:
        opportunity.probability = payload.probability
    if payload.products is not None:
        opportunity.products = [p.model_dump(mode="json") for p in payload.products]
    for field in ("amount", "expected_close_at"):
        if field not in changes:
            continue
        before = getattr(opportunity, field)
        after = getattr(payload, field)
        if field == "amount":
            before, after = _money(before), _money(after)
        if before == after:
            continue
        setattr(opportunity, field, after)
        record(
            session,
            opportunity,
            ActivityKind.FIELD,
            _field_title(field, before, after),
            staff_id=principal.staff_id,
            properties={
                "field": field,
                "from": str(before) if before is not None else None,
                "to": str(after) if after is not None else None,
            },
        )
    if "owner_id" in changes and payload.owner_id != opportunity.owner_id:
        await _change_owner(session, principal, opportunity, payload.owner_id, ip)
    opportunity.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(opportunity)
    if ctx is not None and opportunity.owner_id not in (None, owner_before, principal.staff_id):
        await _push_owner(ctx, principal, opportunity)
    return opportunity


async def assign(
    session: AsyncSession,
    principal: Principal,
    opportunity_id: uuid.UUID,
    owner_id: uuid.UUID | None,
    *,
    ip: str | None = None,
    ctx: "AppContext | None" = None,
) -> Opportunity:
    opportunity = await get_visible(session, principal, opportunity_id, lock=True)
    owner_before = opportunity.owner_id
    if owner_id != opportunity.owner_id:
        await _change_owner(session, principal, opportunity, owner_id, ip)
    await session.commit()
    await session.refresh(opportunity)
    if ctx is not None and opportunity.owner_id not in (None, owner_before, principal.staff_id):
        await _push_owner(ctx, principal, opportunity)
    return opportunity


# ---- 跟进 ----


def enter_stage(
    session: AsyncSession,
    opportunity: Opportunity,
    current: PipelineStage,
    target: PipelineStage,
    *,
    title: str,
    staff_id: uuid.UUID | None,
    properties: dict[str, Any] | None = None,
    position: int | None = None,
) -> None:
    now = datetime.now(UTC)
    days = max((now - opportunity.stage_entered_at).days, 0)
    opportunity.stage_id = target.id
    opportunity.stage_entered_at = now
    opportunity.position = position or 0
    record(
        session,
        opportunity,
        ActivityKind.STAGE,
        title,
        staff_id=staff_id,
        properties={
            "from": current.code,
            "from_name": current.name,
            "to": target.code,
            "to_name": target.name,
            "days": days,
            **(properties or {}),
        },
        at=now,
    )
    emit(
        session,
        opportunity,
        WebhookEventType.OPPORTUNITY_STAGE_CHANGED,
        {"from": current.code, "to": target.code},
        actor_type="staff" if staff_id else "system",
    )


def auto_advance(
    session: AsyncSession,
    opportunity: Opportunity,
    all_stages: list[PipelineStage],
    code: str | None,
    reason: str,
) -> bool:
    """自动推进（§40.5）：只往前不往后；目标阶段不存在或者不比当前靠后时不动。"""
    if opportunity.status != OpportunityStatus.ACTIVE:
        return False
    target = stage_by_code(all_stages, code)
    if target is None or target.kind != StageKind.OPEN:
        return False
    current = stage_by_id(all_stages, opportunity.stage_id)
    if current.kind != StageKind.OPEN or target.position <= current.position:
        return False
    enter_stage(
        session,
        opportunity,
        current,
        target,
        title=f"自动推进到{target.name}（{reason}）",
        staff_id=None,
        properties={"auto": True, "reason": reason},
    )
    return True


async def add_followup(
    session: AsyncSession,
    principal: Principal,
    opportunity_id: uuid.UUID,
    payload: FollowupCreate,
) -> Opportunity:
    """记一次跟进（下次跟进日期不填时按默认天数）或备注。第一次跟进后按设置自动推进。"""
    opportunity = await get_visible(session, principal, opportunity_id, lock=True)
    if opportunity.status != OpportunityStatus.ACTIVE:
        raise Conflict("只有跟进中的商机可以记跟进，先重新跟进")
    if payload.kind == "note":
        record(
            session,
            opportunity,
            ActivityKind.NOTE,
            None,
            staff_id=principal.staff_id,
            content=payload.content,
        )
        await session.commit()
        await session.refresh(opportunity)
        return opportunity
    day = await today(session)
    if payload.next_follow_at is not None and payload.next_follow_at < day:
        raise Unprocessable("下次跟进日期不能早于今天")
    settings = await opportunity_settings.load(session, principal.tenant_id)
    next_at = payload.next_follow_at or day + timedelta(days=settings.follow_days)
    now = datetime.now(UTC)
    first = opportunity.follow_count == 0
    record(
        session,
        opportunity,
        ActivityKind.FOLLOWUP,
        None,
        staff_id=principal.staff_id,
        content=payload.content,
        method=FollowMethod(payload.method),
        next_follow_at=next_at,
        at=now,
    )
    opportunity.next_follow_at = next_at
    opportunity.last_followed_at = now
    opportunity.follow_count += 1
    if first:
        all_stages = await stages(session, principal.tenant_id)
        auto_advance(
            session, opportunity, all_stages, settings.auto_advance.first_followup, "第一次跟进"
        )
    await session.commit()
    await session.refresh(opportunity)
    return opportunity


# ---- 换阶段、赢单、输单、重新跟进 ----


def _close(
    opportunity: Opportunity,
    status: OpportunityStatus,
    stage: PipelineStage,
    staff_id: uuid.UUID | None,
) -> None:
    now = datetime.now(UTC)
    opportunity.status = status
    opportunity.stage_id = stage.id
    opportunity.stage_entered_at = now
    opportunity.position = 0
    opportunity.closed_by = staff_id
    opportunity.closed_at = now
    opportunity.updated_at = now


async def _win(
    session: AsyncSession,
    principal: Principal,
    opportunity: Opportunity,
    all_stages: list[PipelineStage],
    *,
    order_id: uuid.UUID | None,
    contract_id: uuid.UUID | None,
    note: str | None,
    ip: str | None,
) -> None:
    properties: dict[str, Any] = {}
    if order_id is not None:
        from app.modules.orders import service as order_service

        if not principal.has(Permission.ORDER_READ):
            raise Forbidden("没有查看订单的权限")
        order = await order_service.get_visible(session, principal, order_id)
        if order.customer_id != opportunity.customer_id:
            raise Unprocessable("订单不是这个客户的")
        opportunity.order_id = order.id
        if opportunity.amount is None:
            opportunity.amount = order.total
        properties["order_no"] = order.no
    if contract_id is not None:
        contract = await session.get(Contract, contract_id)
        if contract is None or contract.customer_id != opportunity.customer_id:
            raise Unprocessable("合同不是这个客户的")
        opportunity.contract_id = contract.id
        properties["contract_no"] = contract.no
    current = stage_by_id(all_stages, opportunity.stage_id)
    won_stage = stage_of_kind(all_stages, StageKind.WON)
    _close(opportunity, OpportunityStatus.WON, won_stage, principal.staff_id)
    record(
        session,
        opportunity,
        ActivityKind.STAGE,
        f"赢单（{current.name} → {won_stage.name}）",
        staff_id=principal.staff_id,
        content=note,
        properties={"from": current.code, "to": won_stage.code, **properties},
        linked_type="order" if order_id else ("contract" if contract_id else None),
        linked_id=order_id or contract_id,
    )
    emit(
        session, opportunity, WebhookEventType.OPPORTUNITY_WON, {"from": current.code, **properties}
    )
    _audit(
        session,
        principal,
        "opportunity.won",
        opportunity,
        {
            "order_id": str(order_id) if order_id else None,
            "contract_id": str(contract_id) if contract_id else None,
        },
        ip,
    )


async def _lose(
    session: AsyncSession,
    principal: Principal,
    opportunity: Opportunity,
    all_stages: list[PipelineStage],
    *,
    reason_code: str | None,
    reason: str | None,
    ip: str | None,
) -> None:
    settings = await opportunity_settings.load(session, principal.tenant_id)
    name = settings.lost_reason_name(reason_code)
    if reason_code is None or name is None:
        raise Unprocessable("请选择输单原因")
    current = stage_by_id(all_stages, opportunity.stage_id)
    lost_stage = stage_of_kind(all_stages, StageKind.LOST)
    opportunity.lost_reason_code = reason_code
    opportunity.lost_reason = reason
    _close(opportunity, OpportunityStatus.LOST, lost_stage, principal.staff_id)
    record(
        session,
        opportunity,
        ActivityKind.STAGE,
        f"输单：{name}",
        staff_id=principal.staff_id,
        content=reason,
        properties={"from": current.code, "to": lost_stage.code, "reason_code": reason_code},
    )
    emit(
        session,
        opportunity,
        WebhookEventType.OPPORTUNITY_LOST,
        {"from": current.code, "reason_code": reason_code, "reason_name": name},
    )
    _audit(
        session,
        principal,
        "opportunity.lost",
        opportunity,
        {"reason_code": reason_code, "reason": reason},
        ip,
    )


def _ensure_active(opportunity: Opportunity, action: str) -> None:
    if opportunity.status == OpportunityStatus.SUGGESTED:
        raise Conflict("AI 建议的商机请先确认")
    if opportunity.status != OpportunityStatus.ACTIVE:
        raise Conflict(f"只有跟进中的商机可以{action}")


async def move_stage(
    session: AsyncSession,
    principal: Principal,
    opportunity_id: uuid.UUID,
    payload: StageMove,
    *,
    ip: str | None = None,
) -> Opportunity:
    """换阶段（看板拖拽）：拖到赢单、输单按赢单、输单处理；已赢单、已输单的请用重新跟进。"""
    opportunity = await get_visible(session, principal, opportunity_id, lock=True)
    if opportunity.status in (OpportunityStatus.WON, OpportunityStatus.LOST):
        raise Conflict("已赢单、已输单的商机请用重新跟进")
    _ensure_active(opportunity, "换阶段")
    all_stages = await stages(session, principal.tenant_id)
    target = stage_by_id(all_stages, payload.stage_id)
    current = stage_by_id(all_stages, opportunity.stage_id)
    if target.kind == StageKind.WON:
        await _win(
            session,
            principal,
            opportunity,
            all_stages,
            order_id=payload.order_id,
            contract_id=payload.contract_id,
            note=None,
            ip=ip,
        )
    elif target.kind == StageKind.LOST:
        await _lose(
            session,
            principal,
            opportunity,
            all_stages,
            reason_code=payload.lost_reason_code,
            reason=payload.lost_reason,
            ip=ip,
        )
    elif target.id == current.id:
        if payload.position is not None:
            opportunity.position = payload.position
            opportunity.updated_at = datetime.now(UTC)
    else:
        enter_stage(
            session,
            opportunity,
            current,
            target,
            title=f"{current.name} → {target.name}",
            staff_id=principal.staff_id,
            position=payload.position,
        )
        _audit(
            session,
            principal,
            "opportunity.stage",
            opportunity,
            {"from": current.code, "to": target.code},
            ip,
        )
    await session.commit()
    await session.refresh(opportunity)
    return opportunity


async def won(
    session: AsyncSession,
    principal: Principal,
    opportunity_id: uuid.UUID,
    *,
    order_id: uuid.UUID | None,
    contract_id: uuid.UUID | None = None,
    note: str | None = None,
    ip: str | None = None,
) -> Opportunity:
    """手动赢单（可以关联订单或合同）。"""
    opportunity = await get_visible(session, principal, opportunity_id, lock=True)
    _ensure_active(opportunity, "赢单")
    all_stages = await stages(session, principal.tenant_id)
    await _win(
        session,
        principal,
        opportunity,
        all_stages,
        order_id=order_id,
        contract_id=contract_id,
        note=note,
        ip=ip,
    )
    await session.commit()
    await session.refresh(opportunity)
    return opportunity


async def lost(
    session: AsyncSession,
    principal: Principal,
    opportunity_id: uuid.UUID,
    *,
    reason_code: str,
    reason: str | None,
    ip: str | None = None,
) -> Opportunity:
    opportunity = await get_visible(session, principal, opportunity_id, lock=True)
    _ensure_active(opportunity, "输单")
    all_stages = await stages(session, principal.tenant_id)
    await _lose(
        session,
        principal,
        opportunity,
        all_stages,
        reason_code=reason_code,
        reason=reason,
        ip=ip,
    )
    await session.commit()
    await session.refresh(opportunity)
    return opportunity


async def reopen(
    session: AsyncSession,
    principal: Principal,
    opportunity_id: uuid.UUID,
    next_follow_at: date | None,
    *,
    ip: str | None = None,
) -> Opportunity:
    """重新跟进（§40.5）：输单的回到第二个进行中的阶段（原记录继续）；赢单的新开一条商机（原记录保留）。
    这个客户没有别的待确认、跟进中的商机时才能重开。"""
    opportunity = await get_visible(session, principal, opportunity_id, lock=True)
    if opportunity.status not in (OpportunityStatus.WON, OpportunityStatus.LOST):
        raise Conflict("只有已赢单、已输单的商机可以重新跟进")
    day = await today(session)
    if next_follow_at is not None and next_follow_at < day:
        raise Unprocessable("下次跟进日期不能早于今天")
    settings = await opportunity_settings.load(session, principal.tenant_id)
    all_stages = await stages(session, principal.tenant_id)
    opens = open_stages(all_stages)
    now = datetime.now(UTC)
    next_at = next_follow_at or day + timedelta(days=settings.follow_days)
    if opportunity.status == OpportunityStatus.LOST:
        target = opens[1] if len(opens) > 1 else opens[0]
        current = stage_by_id(all_stages, opportunity.stage_id)
        opportunity.status = OpportunityStatus.ACTIVE
        opportunity.next_follow_at = next_at
        opportunity.lost_reason = None
        opportunity.lost_reason_code = None
        opportunity.closed_by = None
        opportunity.closed_at = None
        opportunity.opened_at = now
        opportunity.stage_id = target.id
        opportunity.stage_entered_at = now
        opportunity.position = 0
        try:
            await session.flush()
        except IntegrityError as exc:
            raise Conflict(ALREADY) from exc
        record(
            session,
            opportunity,
            ActivityKind.STAGE,
            f"重新跟进（{current.name} → {target.name}）",
            staff_id=principal.staff_id,
            properties={"from": current.code, "to": target.code, "reopen": True},
            at=now,
        )
        emit(
            session,
            opportunity,
            WebhookEventType.OPPORTUNITY_STAGE_CHANGED,
            {"from": current.code, "to": target.code, "reopen": True},
        )
        _audit(session, principal, "opportunity.reopen", opportunity, ip=ip)
        await session.commit()
        await session.refresh(opportunity)
        return opportunity
    customer = await session.get(Customer, opportunity.customer_id)
    assert customer is not None
    stage = opens[0]
    fresh = Opportunity(
        tenant_id=principal.tenant_id,
        customer_id=customer.id,
        name=f"{customer.display_name} 的商机"[:NAME_MAX],
        stage_id=stage.id,
        status=OpportunityStatus.ACTIVE,
        level=opportunity.level,
        source=OpportunitySource.STAFF,
        owner_id=opportunity.owner_id or principal.staff_id,
        next_follow_at=next_at,
        stage_entered_at=now,
        last_activity_at=now,
        created_by=principal.staff_id,
    )
    session.add(fresh)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict(ALREADY) from exc
    record(
        session,
        fresh,
        ActivityKind.CREATED,
        f"再开一个商机（上一个已赢单），阶段：{stage.name}",
        staff_id=principal.staff_id,
        properties={"stage": stage.code, "previous_id": str(opportunity.id)},
        linked_type="opportunity",
        linked_id=opportunity.id,
        at=now,
    )
    record(
        session,
        opportunity,
        ActivityKind.CREATED,
        "再开了一个商机",
        staff_id=principal.staff_id,
        properties={"next_id": str(fresh.id)},
        linked_type="opportunity",
        linked_id=fresh.id,
        at=now,
    )
    _audit(session, principal, "opportunity.reopen", opportunity, {"next_id": str(fresh.id)}, ip)
    emit(
        session,
        fresh,
        WebhookEventType.OPPORTUNITY_CREATED,
        {"stage": stage.code, "previous_id": str(opportunity.id)},
    )
    await session.commit()
    await session.refresh(fresh)
    return fresh


async def accept(
    session: AsyncSession, principal: Principal, opportunity_id: uuid.UUID, *, ip: str | None = None
) -> Opportunity:
    """确认 AI 的建议：转入商机。"""
    opportunity = await get_visible(session, principal, opportunity_id, lock=True)
    if opportunity.status != OpportunityStatus.SUGGESTED:
        raise Conflict("只有 AI 建议的商机需要确认")
    opportunity.status = OpportunityStatus.ACTIVE
    if opportunity.owner_id is None:
        opportunity.owner_id = principal.staff_id
    now = datetime.now(UTC)
    opportunity.stage_entered_at = now
    record(
        session,
        opportunity,
        ActivityKind.CREATED,
        "确认了 AI 的建议",
        staff_id=principal.staff_id,
        at=now,
    )
    _audit(session, principal, "opportunity.accept", opportunity, ip=ip)
    stage = stage_by_id(await stages(session, principal.tenant_id), opportunity.stage_id)
    emit(
        session,
        opportunity,
        WebhookEventType.OPPORTUNITY_CREATED,
        {"stage": stage.code, "accepted": True},
    )
    await session.commit()
    await session.refresh(opportunity)
    return opportunity


async def dismiss(
    session: AsyncSession, principal: Principal, opportunity_id: uuid.UUID, *, ip: str | None = None
) -> None:
    """忽略 AI 的建议：不在列表里显示，30 天内 AI 不再建议这个客户。"""
    opportunity = await get_visible(session, principal, opportunity_id, lock=True)
    if opportunity.status != OpportunityStatus.SUGGESTED:
        raise Conflict("只有 AI 建议的商机可以忽略")
    now = datetime.now(UTC)
    opportunity.status = OpportunityStatus.DISMISSED
    opportunity.closed_by = principal.staff_id
    opportunity.closed_at = now
    opportunity.updated_at = now
    _audit(session, principal, "opportunity.dismiss", opportunity, ip=ip)
    await session.commit()


# ---- 成交（订单确认） ----


async def win_by_order(
    session: AsyncSession, opportunity: Opportunity, order: Order, all_stages: list[PipelineStage]
) -> None:
    """订单确认后自动赢单（由调用方提交）：记下订单，预计金额没填时取订单合计。"""
    current = stage_by_id(all_stages, opportunity.stage_id)
    won_stage = stage_of_kind(all_stages, StageKind.WON)
    opportunity.order_id = order.id
    if opportunity.amount is None:
        opportunity.amount = order.total
    _close(opportunity, OpportunityStatus.WON, won_stage, None)
    if order.confirmed_at is not None:
        opportunity.closed_at = order.confirmed_at
    record(
        session,
        opportunity,
        ActivityKind.ORDER,
        f"订单 {order.no} 已确认，自动赢单",
        properties={
            "from": current.code,
            "to": won_stage.code,
            "order_no": order.no,
            "total": str(order.total),
        },
        linked_type="order",
        linked_id=order.id,
        at=order.confirmed_at or datetime.now(UTC),
    )
    emit(
        session,
        opportunity,
        WebhookEventType.OPPORTUNITY_WON,
        {"from": current.code, "order_no": order.no},
        actor_type="system",
    )


async def order_confirmed(session: AsyncSession, order: Order) -> Opportunity | None:
    """订单确认后：这个客户待确认、跟进中的商机变成"赢单"（由调用方提交）。"""
    return await order_changed(session, order, "confirmed")


# ---- 订单、合同、待办的动态（§40.7）----

ORDER_TITLES = {
    "submitted": "订单 {no} 提交审核，金额 {total} 元",
    "confirmed": "订单 {no} 已确认，金额 {total} 元",
    "shipped": "订单 {no} 已发货",
    "completed": "订单 {no} 已完成",
    "cancelled": "订单 {no} 已取消",
    "paid": "订单 {no} 收款 {amount} 元",
    "refunded": "订单 {no} 退款 {amount} 元",
}
CONTRACT_TITLES = {
    "create": "起草合同 {no}《{title}》",
    "generate": "AI 起草合同 {no}《{title}》",
    "finalize": "合同 {no} 已定稿",
    "reopen": "合同 {no} 退回修改",
    "sign": "合同 {no} 已签署",
    "void": "合同 {no} 已作废",
}
TODO_TITLES = {
    "created": "待办 {no}：{title}",
    "done": "完成待办 {no}：{title}",
    "cancelled": "取消待办 {no}：{title}",
}
OPEN_TODO_STATUSES = (
    TodoStatus.PENDING,
    TodoStatus.OPEN,
    TodoStatus.IN_PROGRESS,
    TodoStatus.WAITING,
)


async def related(
    session: AsyncSession,
    customer_id: uuid.UUID | None,
    *,
    order_id: uuid.UUID | None = None,
    contract_id: uuid.UUID | None = None,
) -> Opportunity | None:
    """订单、合同所属的商机（锁住）：关联了它的那条；否则这个客户待确认、跟进中的那条。"""
    links = [
        column == value
        for column, value in (
            (Opportunity.order_id, order_id),
            (Opportunity.contract_id, contract_id),
        )
        if value is not None
    ]
    if links:
        linked = await session.scalar(
            select(Opportunity)
            .where(or_(*links))
            .order_by(Opportunity.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        if linked is not None:
            return linked
    if customer_id is None:
        return None
    return await session.scalar(
        select(Opportunity)
        .where(Opportunity.customer_id == customer_id, Opportunity.status.in_(OPEN_STATUSES))
        .with_for_update()
    )


def _yuan(value: Decimal | None) -> str:
    return f"{value:.2f}" if value is not None else "0.00"


async def order_changed(
    session: AsyncSession,
    order: Order,
    change: str,
    *,
    amount: Decimal | None = None,
    staff_id: uuid.UUID | None = None,
) -> Opportunity | None:
    """订单的动态写进商机的时间线并自动推进（§40.7，由调用方提交）：提交审核 → 已报价；确认 →
    赢单（待确认、跟进中的）；发货、完成、取消和收款、退款各记一条。只记事实和编号。"""
    title = ORDER_TITLES.get(change)
    if title is None:
        return None
    opportunity = await related(session, order.customer_id, order_id=order.id)
    if opportunity is None:
        return None
    all_stages = await stages(session, opportunity.tenant_id)
    if change == "confirmed" and opportunity.status in OPEN_STATUSES:
        await win_by_order(session, opportunity, order, all_stages)
        return opportunity
    record(
        session,
        opportunity,
        ActivityKind.PAYMENT if change in ("paid", "refunded") else ActivityKind.ORDER,
        title.format(no=order.no, total=_yuan(order.total), amount=_yuan(amount)),
        staff_id=staff_id,
        properties={
            "change": change,
            "order_no": order.no,
            "status": order.status,
            "total": _yuan(order.total),
            **({"amount": _yuan(amount)} if amount is not None else {}),
        },
        linked_type="order",
        linked_id=order.id,
    )
    if change == "submitted":
        settings = await opportunity_settings.load(session, opportunity.tenant_id)
        auto_advance(
            session,
            opportunity,
            all_stages,
            settings.auto_advance.quote,
            f"订单 {order.no} 提交审核",
        )
    return opportunity


async def win_by_contract(
    session: AsyncSession,
    opportunity: Opportunity,
    contract: Contract,
    all_stages: list[PipelineStage],
    *,
    staff_id: uuid.UUID | None,
) -> None:
    """合同签署后自动赢单（由调用方提交）：记下合同，预计金额没填时取合同金额。"""
    current = stage_by_id(all_stages, opportunity.stage_id)
    won_stage = stage_of_kind(all_stages, StageKind.WON)
    opportunity.contract_id = contract.id
    if opportunity.amount is None and contract.amount is not None:
        opportunity.amount = contract.amount
    _close(opportunity, OpportunityStatus.WON, won_stage, staff_id)
    record(
        session,
        opportunity,
        ActivityKind.CONTRACT,
        f"合同 {contract.no} 已签署，自动赢单",
        staff_id=staff_id,
        properties={
            "from": current.code,
            "to": won_stage.code,
            "action": "sign",
            "contract_no": contract.no,
        },
        linked_type="contract",
        linked_id=contract.id,
    )
    emit(
        session,
        opportunity,
        WebhookEventType.OPPORTUNITY_WON,
        {"from": current.code, "contract_no": contract.no},
        actor_type="staff" if staff_id else "system",
    )


async def contract_changed(
    session: AsyncSession,
    contract: Contract,
    action: str,
    *,
    staff_id: uuid.UUID | None = None,
) -> Opportunity | None:
    """合同的动态写进时间线并自动推进（§40.7，由调用方提交）：定稿 → 谈判中；签署 → 赢单；起草、
    退回修改、作废各记一条。起草的合同挂到这条商机上（还没有关联合同时）。"""
    title = CONTRACT_TITLES.get(action)
    if title is None:
        return None
    opportunity = await related(
        session, contract.customer_id, order_id=contract.order_id, contract_id=contract.id
    )
    if opportunity is None:
        return None
    all_stages = await stages(session, opportunity.tenant_id)
    if action == "sign" and opportunity.status in OPEN_STATUSES:
        await win_by_contract(session, opportunity, contract, all_stages, staff_id=staff_id)
        return opportunity
    record(
        session,
        opportunity,
        ActivityKind.CONTRACT,
        title.format(no=contract.no, title=contract.title),
        staff_id=staff_id,
        properties={
            "action": action,
            "contract_no": contract.no,
            "status": contract.status,
            **({"amount": _yuan(contract.amount)} if contract.amount is not None else {}),
        },
        linked_type="contract",
        linked_id=contract.id,
    )
    if (
        opportunity.contract_id is None
        and action in ("create", "generate")
        and opportunity.status in OPEN_STATUSES
    ):
        opportunity.contract_id = contract.id
    if action == "finalize":
        settings = await opportunity_settings.load(session, opportunity.tenant_id)
        auto_advance(
            session,
            opportunity,
            all_stages,
            settings.auto_advance.contract_final,
            f"合同 {contract.no} 定稿",
        )
    return opportunity


async def open_opportunity_id(
    session: AsyncSession, customer_id: uuid.UUID | None
) -> uuid.UUID | None:
    """客户待确认、跟进中的商机（新建待办时挂上去）。"""
    if customer_id is None:
        return None
    return await session.scalar(
        select(Opportunity.id).where(
            Opportunity.customer_id == customer_id, Opportunity.status.in_(OPEN_STATUSES)
        )
    )


async def todo_changed(
    session: AsyncSession, todo: Todo, change: str, *, staff_id: uuid.UUID | None = None
) -> Opportunity | None:
    """关联商机的待办的创建、完成、取消写进时间线（§40.7，由调用方提交）；"报价"类的待办完成后
    自动推进到已报价。"""
    title = TODO_TITLES.get(change)
    if title is None or todo.opportunity_id is None:
        return None
    opportunity = await session.get(Opportunity, todo.opportunity_id, with_for_update=True)
    if opportunity is None:
        return None
    type_ = await session.get(TodoType, todo.type_id)
    record(
        session,
        opportunity,
        ActivityKind.TODO,
        title.format(no=todo.no, title=todo.title),
        staff_id=staff_id,
        content=todo.result if change == "done" else None,
        properties={
            "change": change,
            "todo_no": todo.no,
            "type": type_.code if type_ else None,
            "type_name": type_.name if type_ else None,
            "status": todo.status,
        },
        linked_type="todo",
        linked_id=todo.id,
    )
    if change == "done" and type_ is not None and type_.code == "quote":
        settings = await opportunity_settings.load(session, opportunity.tenant_id)
        auto_advance(
            session,
            opportunity,
            await stages(session, opportunity.tenant_id),
            settings.auto_advance.quote,
            f"报价待办 {todo.no} 完成",
        )
    return opportunity


async def open_todos(session: AsyncSession, opportunity_id: uuid.UUID) -> list[TodoBrief]:
    """这条商机上没完成的待办（详情里显示）。"""
    rows = (
        await session.execute(
            select(Todo, TodoType.name, Staff.display_name)
            .join(TodoType, TodoType.id == Todo.type_id)
            .outerjoin(Staff, Staff.id == Todo.assignee_id)
            .where(Todo.opportunity_id == opportunity_id, Todo.status.in_(OPEN_TODO_STATUSES))
            .order_by(Todo.due_at.nulls_last(), Todo.created_at)
        )
    ).all()
    return [
        TodoBrief(
            id=t.id,
            no=t.no,
            title=t.title,
            type_name=type_name,
            status=t.status,
            due_at=t.due_at,
            assignee_name=assignee,
        )
        for t, type_name, assignee in rows
    ]


async def schedule_todo(
    session: AsyncSession,
    keys: TenantKeyring | None,
    principal: Principal,
    opportunity_id: uuid.UUID,
    payload: NextStep,
) -> Opportunity:
    """安排下一步（§40.7）：建一条关联这条商机的待办（默认"回电 / 回访"，处理人默认是负责人），
    可以同时改下次跟进日期。待办的到期提醒走待办自己的机制。"""
    from app.modules.todos import service as todo_service
    from app.modules.todos.presets import type_by_code

    opportunity = await get_visible(session, principal, opportunity_id, lock=True)
    _ensure_active(opportunity, "安排下一步")
    try:
        type_ = await type_by_code(session, principal.tenant_id, payload.type_code)
    except LookupError as exc:
        raise Unprocessable("待办类型不存在") from exc
    if not type_.enabled or type_.system:
        raise Unprocessable("这个类型的待办不能手工新建")
    assignee = payload.assignee_id or opportunity.owner_id or principal.staff_id
    if assignee not in (principal.staff_id, opportunity.owner_id) and not principal.has(
        Permission.TODO_ASSIGN
    ):
        raise Forbidden("没有分派待办的权限，只能安排给自己或负责人")
    staff = await session.get(Staff, assignee)
    if staff is None or staff.status != StaffStatus.ACTIVE:
        raise Unprocessable("处理人不存在或者已经停用")
    now = datetime.now(UTC)
    if payload.due_at is not None and payload.due_at <= now:
        raise Unprocessable("截止时间必须晚于现在")
    day = await today(session)
    if payload.next_follow_at is not None and payload.next_follow_at < day:
        raise Unprocessable("下次跟进日期不能早于今天")
    await todo_service.create(
        session,
        keys,
        todo_service.Draft(
            type=type_,
            title=(payload.title or f"{type_.name}：{opportunity.name}")[:100],
            detail=payload.detail,
            source=TodoSource.STAFF,
            created_by_type=ActorType.STAFF,
            created_by=principal.staff_id,
            customer_id=opportunity.customer_id,
            due_at=payload.due_at,
            explicit=True,
            assignee_id=assignee,
            opportunity_id=opportunity.id,
        ),
        now=now,
    )
    if payload.next_follow_at is not None:
        opportunity.next_follow_at = payload.next_follow_at
    opportunity.updated_at = now
    await session.commit()
    await session.refresh(opportunity)
    return opportunity


# ---- 轮流分配（§40.6）----


async def round_robin_owner(
    session: AsyncSession, settings: OpportunitySettings
) -> uuid.UUID | None:
    """设置为轮流分配时：技能组里启用的成员中，还没分到过商机的先分（先加入企业的在前），然后是最近
    分到商机最早的。没有设置或者组里没有启用的成员时为空。"""
    if settings.assignment != "round_robin" or settings.assignment_group_id is None:
        return None
    members = list(
        (
            await session.scalars(
                select(SkillGroupMember.staff_id)
                .join(Staff, Staff.id == SkillGroupMember.staff_id)
                .where(
                    SkillGroupMember.skill_group_id == settings.assignment_group_id,
                    Staff.status == StaffStatus.ACTIVE,
                )
                .order_by(Staff.created_at, Staff.id)
            )
        ).all()
    )
    if not members:
        return None
    latest: dict[uuid.UUID, datetime] = {
        owner_id: at
        for owner_id, at in await session.execute(
            select(Opportunity.owner_id, func.max(Opportunity.created_at))
            .where(Opportunity.owner_id.in_(members))
            .group_by(Opportunity.owner_id)
        )
        if owner_id is not None
    }
    floor = datetime.min.replace(tzinfo=UTC)
    return min(
        members,
        key=lambda staff_id: (
            staff_id in latest,
            latest.get(staff_id, floor),
            members.index(staff_id),
        ),
    )


# ---- 合并客户 ----


async def merge_customers(
    session: AsyncSession, target_id: uuid.UUID, source_ids: list[uuid.UUID]
) -> int:
    """合并客户时（customer/privacy.py）：来源客户的商机并入目标客户（由调用方提交）。

    同一客户只能有一条待确认或跟进中的：留下一条（跟进中的优先，其次是目标客户的、最近修改的），
    其他的时间线并到这条上（想要什么、顾虑为空时补上，等级取高的，下次跟进取早的，金额取大的），然后删除。
    已赢单、已输单的直接改挂到目标客户。返回改挂和合并的条数。
    """
    rows = (
        await session.scalars(
            select(Opportunity)
            .where(Opportunity.customer_id.in_([target_id, *source_ids]))
            .with_for_update()
        )
    ).all()
    opened = sorted(
        (r for r in rows if r.status in OPEN_STATUSES),
        key=lambda r: (
            r.status != OpportunityStatus.ACTIVE,
            r.customer_id != target_id,
            -r.updated_at.timestamp(),
        ),
    )
    for extra in opened[1:]:
        keep = opened[0]
        await session.execute(
            update_rows(OpportunityActivity)
            .where(OpportunityActivity.opportunity_id == extra.id)
            .values(opportunity_id=keep.id)
        )
        keep.interest = keep.interest or extra.interest
        keep.concerns = keep.concerns or extra.concerns
        if LEVEL_RANK[OpportunityLevel(extra.level)] > LEVEL_RANK[OpportunityLevel(keep.level)]:
            keep.level = extra.level
        if extra.next_follow_at is not None and (
            keep.next_follow_at is None or extra.next_follow_at < keep.next_follow_at
        ):
            keep.next_follow_at = extra.next_follow_at
        if extra.last_followed_at is not None and (
            keep.last_followed_at is None or extra.last_followed_at > keep.last_followed_at
        ):
            keep.last_followed_at = extra.last_followed_at
        if extra.amount is not None and (keep.amount is None or extra.amount > keep.amount):
            keep.amount = extra.amount
        keep.expected_close_at = keep.expected_close_at or extra.expected_close_at
        keep.follow_count += extra.follow_count
        keep.owner_id = keep.owner_id or extra.owner_id
        await session.delete(extra)
    # 先删掉多出来的，再改挂（部分唯一索引）。
    await session.flush()
    result = await session.execute(
        update_rows(Opportunity)
        .where(Opportunity.customer_id.in_(source_ids))
        .values(customer_id=target_id)
    )
    return int(getattr(result, "rowcount", 0) or 0) + max(len(opened) - 1, 0)


def level_of(stage: int | None) -> OpportunityLevel:
    """会话的最高意向 → 意向等级：准备下单为高，意向明确为中，其他为低。"""
    if stage is not None and stage >= 4:
        return OpportunityLevel.HIGH
    if stage is not None and stage >= 3:
        return OpportunityLevel.MEDIUM
    return OpportunityLevel.LOW


# ---- 导出、企业系统回传 ----


async def export_conditions(
    session: AsyncSession,
    keys: TenantKeyring,
    principal: Principal,
    *,
    view: str,
    stage_id: uuid.UUID | None,
    owner_id: uuid.UUID | None,
    q: str | None,
) -> list[ColumnElement[bool]]:
    """导出用的筛选条件：查看范围、快捷视图、阶段、负责人、搜索。"""
    day = await today(session)
    conditions = await _scope(keys, principal, stage_id=stage_id, owner_id=owner_id, q=q)
    conditions.append(_view(view, day, principal))
    return conditions


async def close_by_system(
    session: AsyncSession,
    opportunity: Opportunity,
    all_stages: list[PipelineStage],
    status: OpportunityStatus,
    *,
    order: Order | None = None,
    reason_code: str | None = None,
    reason: str | None = None,
    actor_type: str = "api",
) -> None:
    """没有员工操作的赢单 / 输单（企业系统回传，由调用方提交）。"""
    current = stage_by_id(all_stages, opportunity.stage_id)
    if status == OpportunityStatus.WON:
        if order is not None:
            await win_by_order(session, opportunity, order, all_stages)
            return
        won_stage = stage_of_kind(all_stages, StageKind.WON)
        _close(opportunity, OpportunityStatus.WON, won_stage, None)
        record(
            session,
            opportunity,
            ActivityKind.STAGE,
            f"赢单（{current.name} → {won_stage.name}，企业系统回传）",
            properties={"from": current.code, "to": won_stage.code, "by": actor_type},
        )
        emit(
            session,
            opportunity,
            WebhookEventType.OPPORTUNITY_WON,
            {"from": current.code},
            actor_type=actor_type,
        )
        return
    settings = await opportunity_settings.load(session, opportunity.tenant_id)
    name = settings.lost_reason_name(reason_code)
    if reason_code is None or name is None:
        raise Unprocessable("请选择输单原因（商机设置里的分类代码）")
    lost_stage = stage_of_kind(all_stages, StageKind.LOST)
    opportunity.lost_reason_code = reason_code
    opportunity.lost_reason = reason
    _close(opportunity, OpportunityStatus.LOST, lost_stage, None)
    record(
        session,
        opportunity,
        ActivityKind.STAGE,
        f"输单：{name}（企业系统回传）",
        content=reason,
        properties={
            "from": current.code,
            "to": lost_stage.code,
            "reason_code": reason_code,
            "by": actor_type,
        },
    )
    emit(
        session,
        opportunity,
        WebhookEventType.OPPORTUNITY_LOST,
        {"from": current.code, "reason_code": reason_code, "reason_name": name},
        actor_type=actor_type,
    )
