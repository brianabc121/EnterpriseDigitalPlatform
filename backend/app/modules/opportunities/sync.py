"""开放接口的商机（设计文档 §40.6、§40.13）：企业系统（官网表单、投放线索）创建线索，查询和修改
商机。

动态记为系统记的（没有员工），推送事件的 actor 是 api；操作日志记接口密钥。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.ids import new_id
from app.modules.audit.service import record_audit
from app.modules.customer import sensitive
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff, StaffStatus
from app.modules.integration.models import WebhookEventType
from app.modules.integration.schemas import OpenOpportunityCreate, OpenOpportunityUpdate
from app.modules.opportunities import service
from app.modules.opportunities import settings as opportunity_settings
from app.modules.opportunities.models import (
    OPEN_STATUSES,
    ActivityKind,
    Opportunity,
    OpportunitySource,
    OpportunityStatus,
    StageKind,
)
from app.modules.orders.models import Order
from app.modules.orders.sync import API, ApiActor

__all__ = ["ApiActor", "create", "update"]


def _audit(
    session: AsyncSession, actor: ApiActor, action: str, opportunity: Opportunity, **detail: Any
) -> None:
    record_audit(
        session,
        action=action,
        actor_type=API,
        actor_id=actor.key_id,
        tenant_id=actor.tenant_id,
        resource_type="opportunity",
        resource_id=str(opportunity.id),
        detail={"customer_id": str(opportunity.customer_id), **detail},
    )


async def _customer(
    ctx: AppContext, session: AsyncSession, actor: ApiActor, payload: OpenOpportunityCreate
) -> Customer:
    """客户：按 customer_id，或者按手机号找到已有的，找不到时新建（和开放接口的订单一样）。"""
    if payload.customer_id is not None:
        customer = await session.get(Customer, payload.customer_id)
        if customer is None:
            raise NotFound("客户不存在")
        return customer
    if payload.customer is None:
        raise Unprocessable("请提供 customer_id，或者客户的名称和手机号（customer）")
    phone = sensitive.normalize_phone(payload.customer.phone or "")
    if phone:
        if not sensitive.valid_phone(phone):
            raise Unprocessable("客户手机号的格式不正确")
        phone_hash, _ = await sensitive.search_indexes(ctx.keys, actor.tenant_id, phone)
        found = await session.scalar(
            select(Customer).where(Customer.phone_hash == phone_hash).limit(1)
        )
        if found is not None:
            return found
    customer = Customer(
        id=new_id(),
        tenant_id=actor.tenant_id,
        display_name=payload.customer.name.strip(),
        source_channel="api",
    )
    await sensitive.set_phone(ctx.keys, customer, phone or None)
    session.add(customer)
    await session.flush()
    record_audit(
        session,
        action="customer.create",
        actor_type=API,
        actor_id=actor.key_id,
        tenant_id=actor.tenant_id,
        resource_type="customer",
        resource_id=str(customer.id),
    )
    return customer


async def create(
    ctx: AppContext, session: AsyncSession, actor: ApiActor, payload: OpenOpportunityCreate
) -> tuple[Opportunity, bool]:
    """创建线索：进第一个进行中的阶段；负责人按用户名指定，不填时是客户的归属坐席，再没有时按设置
    轮流分配。返回（商机，是否新建）：同一客户已经有待确认、跟进中的商机时返回已有的。"""
    customer = await _customer(ctx, session, actor, payload)
    existing = await session.scalar(
        select(Opportunity).where(
            Opportunity.customer_id == customer.id, Opportunity.status.in_(OPEN_STATUSES)
        )
    )
    if existing is not None:
        return existing, False
    settings = await opportunity_settings.load(session, actor.tenant_id)
    all_stages = await service.stages(session, actor.tenant_id)
    stage = service.open_stages(all_stages)[0]
    day = await service.today(session)
    owner_id: uuid.UUID | None
    if payload.owner_username:
        staff = await session.scalar(
            select(Staff).where(
                Staff.username == payload.owner_username.strip(),
                Staff.status == StaffStatus.ACTIVE,
            )
        )
        if staff is None:
            raise Unprocessable("负责人不存在或者已经停用")
        owner_id = staff.id
    else:
        owner_id = customer.owner_id or await service.round_robin_owner(session, settings)
    now = datetime.now(UTC)
    opportunity = Opportunity(
        tenant_id=actor.tenant_id,
        customer_id=customer.id,
        name=service.default_name(payload.name, payload.interest, customer),
        stage_id=stage.id,
        status=OpportunityStatus.ACTIVE,
        level=payload.level,
        interest=payload.interest,
        concerns=payload.concerns,
        source=OpportunitySource.API,
        owner_id=owner_id,
        next_follow_at=day + timedelta(days=settings.follow_days),
        amount=payload.amount,
        expected_close_at=payload.expected_close_at,
        stage_entered_at=now,
        last_activity_at=now,
    )
    session.add(opportunity)
    try:
        await session.flush()
    except IntegrityError as exc:
        # 同时来了两条同一客户的线索：后到的一条返回先建好的。
        await session.rollback()
        existing = await session.scalar(
            select(Opportunity).where(
                Opportunity.customer_id == customer.id, Opportunity.status.in_(OPEN_STATUSES)
            )
        )
        if existing is None:
            raise Conflict(service.ALREADY) from exc
        return existing, False
    service.record(
        session,
        opportunity,
        ActivityKind.CREATED,
        f"企业系统转入，阶段：{stage.name}",
        properties={"stage": stage.code, "stage_name": stage.name, "by": API},
        at=now,
    )
    _audit(session, actor, "opportunity.create", opportunity, source=API)
    service.emit(
        session,
        opportunity,
        WebhookEventType.OPPORTUNITY_CREATED,
        {"stage": stage.code},
        actor_type=API,
    )
    await session.commit()
    await session.refresh(opportunity)
    return opportunity, True


async def update(
    session: AsyncSession,
    actor: ApiActor,
    opportunity_id: uuid.UUID,
    payload: OpenOpportunityUpdate,
) -> Opportunity:
    """修改字段；换到进行中的阶段；赢单（可带平台订单号，预计金额没填时取订单合计）或输单。"""
    opportunity = await session.get(Opportunity, opportunity_id, with_for_update=True)
    if opportunity is None or opportunity.status == OpportunityStatus.DISMISSED:
        raise NotFound("商机不存在")
    changes = payload.model_dump(exclude_unset=True)
    now = datetime.now(UTC)
    if payload.name is not None:
        opportunity.name = payload.name.strip()[: service.NAME_MAX] or opportunity.name
    for field in ("interest", "concerns", "amount", "expected_close_at"):
        if field in changes:
            setattr(opportunity, field, getattr(payload, field))
    if payload.level is not None:
        opportunity.level = payload.level
    if "next_follow_at" in changes:
        day = await service.today(session)
        if payload.next_follow_at is not None and payload.next_follow_at < day:
            raise Unprocessable("下次跟进日期不能早于今天")
        opportunity.next_follow_at = payload.next_follow_at
    all_stages = await service.stages(session, actor.tenant_id)
    if payload.stage is not None:
        if opportunity.status != OpportunityStatus.ACTIVE:
            raise Conflict("只有跟进中的商机可以换阶段")
        target = service.stage_by_code(all_stages, payload.stage)
        if target is None or target.kind != StageKind.OPEN:
            raise Unprocessable("阶段不存在，或者不是进行中的阶段（赢单、输单请用 status）")
        current = service.stage_by_id(all_stages, opportunity.stage_id)
        if target.id != current.id:
            service.enter_stage(
                session,
                opportunity,
                current,
                target,
                title=f"{current.name} → {target.name}（企业系统回传）",
                staff_id=None,
                properties={"by": API},
            )
    if payload.status is not None:
        if opportunity.status not in OPEN_STATUSES:
            raise Conflict("已赢单、已输单的商机不能再回传赢单 / 输单")
        order = None
        if payload.status == "won" and payload.order_no:
            order = await session.scalar(
                select(Order).where(
                    (Order.no == payload.order_no) | (Order.external_no == payload.order_no)
                )
            )
            if order is None or order.customer_id != opportunity.customer_id:
                raise Unprocessable("订单不存在，或者不是这个客户的")
        await service.close_by_system(
            session,
            opportunity,
            all_stages,
            OpportunityStatus.WON if payload.status == "won" else OpportunityStatus.LOST,
            order=order,
            reason_code=payload.lost_reason_code,
            reason=payload.lost_reason,
            actor_type=API,
        )
    opportunity.updated_at = now
    _audit(session, actor, "opportunity.update", opportunity, changes=sorted(changes))
    await session.commit()
    await session.refresh(opportunity)
    return opportunity
