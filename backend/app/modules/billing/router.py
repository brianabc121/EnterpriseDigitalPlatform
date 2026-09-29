from datetime import date
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, status

from app.context import AppContext
from app.core.dates import today
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.billing import service
from app.modules.billing.schemas import (
    BillingOverview,
    InvoiceGenerate,
    InvoiceGenerateResult,
    InvoiceList,
    InvoiceOut,
    InvoiceUpdate,
    PlanCreate,
    PlanList,
    PlanOut,
    PlanUpdate,
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionRenew,
    TenantBilling,
    TenantOverrides,
)
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.tenancy import service as tenancy
from app.modules.tenancy.deps import CurrentPlatformUser, PlatformDb

router = APIRouter(prefix="/api/v1", tags=["billing"], responses=ERROR_RESPONSES)
platform_router = APIRouter(prefix="/platform/v1", tags=["platform"], responses=ERROR_RESPONSES)

ContextDep = Annotated[AppContext, Depends(get_context)]
CanManage = Annotated[Principal, Depends(require_permission(Permission.SETTINGS_MANAGE))]


def _tz(ctx: AppContext) -> ZoneInfo:
    return ZoneInfo(ctx.settings.usage_timezone)


# ---- 租户 ----


@router.get("/billing", response_model=BillingOverview)
async def my_billing(session: TenantDb, principal: CanManage, ctx: ContextDep) -> BillingOverview:
    """本租户的套餐、订阅、额度用量和功能。"""
    return await service.overview(session, principal.tenant_id, _tz(ctx))


@router.get("/billing/invoices", response_model=InvoiceList)
async def my_invoices(session: TenantDb, principal: CanManage) -> InvoiceList:
    return InvoiceList(items=await service.list_invoices(session, tenant_id=principal.tenant_id))


@router.get("/billing/plans", response_model=PlanList)
async def public_plans(session: TenantDb, _: CanManage) -> PlanList:
    """可以选择的套餐（升级请联系平台）。"""
    plans = await service.list_plans(session, public_only=True)
    return PlanList(items=[PlanOut.of(p) for p in plans])


# ---- 平台：套餐 ----


@platform_router.get("/plans", response_model=PlanList)
async def list_plans(session: PlatformDb, _: CurrentPlatformUser) -> PlanList:
    return PlanList(items=[PlanOut.of(p) for p in await service.list_plans(session)])


@platform_router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: PlanCreate, request: Request, session: PlatformDb, user: CurrentPlatformUser
) -> PlanOut:
    plan = await service.create_plan(session, payload, actor_id=user.id, ip=client_ip(request))
    return PlanOut.of(plan)


@platform_router.patch("/plans/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: UUID,
    payload: PlanUpdate,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
) -> PlanOut:
    """修改套餐（已有订阅立即按新的额度和功能生效）；下架后不能用于新订阅。"""
    plan = await service.update_plan(
        session, plan_id, payload, actor_id=user.id, ip=client_ip(request)
    )
    return PlanOut.of(plan)


# ---- 平台：租户订阅 ----


@platform_router.get("/tenants/{tenant_id}/billing", response_model=TenantBilling)
async def tenant_billing(
    tenant_id: UUID, session: PlatformDb, _: CurrentPlatformUser, ctx: ContextDep
) -> TenantBilling:
    await tenancy.get_tenant(session, tenant_id)
    return await service.tenant_billing(session, tenant_id, _tz(ctx))


@platform_router.post(
    "/tenants/{tenant_id}/subscriptions",
    response_model=SubscriptionOut,
    status_code=status.HTTP_201_CREATED,
)
async def start_subscription(
    tenant_id: UUID,
    payload: SubscriptionCreate,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> SubscriptionOut:
    """开始新订阅：试用转正式、升级或降级。当前订阅随之取消；因到期停用的租户恢复。"""
    tenant = await tenancy.get_tenant(session, tenant_id)
    sub = await service.start_subscription(
        session,
        tenant,
        payload,
        actor_type="platform",
        actor_id=user.id,
        ip=client_ip(request),
        current_day=today(_tz(ctx)),
    )
    await session.commit()
    plan = await service.get_plan(session, sub.plan_id)
    await session.refresh(sub)
    return service.subscription_out(sub, plan)


@platform_router.post(
    "/tenants/{tenant_id}/subscriptions/{subscription_id}/renew", response_model=SubscriptionOut
)
async def renew_subscription(
    tenant_id: UUID,
    subscription_id: UUID,
    payload: SubscriptionRenew,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> SubscriptionOut:
    """续费：延长当前订阅。"""
    tenant = await tenancy.get_tenant(session, tenant_id)
    sub = await service.renew_subscription(
        session,
        tenant,
        subscription_id,
        payload,
        actor_id=user.id,
        ip=client_ip(request),
        current_day=today(_tz(ctx)),
    )
    await session.commit()
    await session.refresh(sub)
    return service.subscription_out(sub, await service.get_plan(session, sub.plan_id))


@platform_router.post(
    "/tenants/{tenant_id}/subscriptions/{subscription_id}/cancel", response_model=SubscriptionOut
)
async def cancel_subscription(
    tenant_id: UUID,
    subscription_id: UUID,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> SubscriptionOut:
    """取消订阅：截止到今天，宽限期过后停用租户。"""
    tenant = await tenancy.get_tenant(session, tenant_id)
    sub = await service.cancel_subscription(
        session,
        tenant,
        subscription_id,
        actor_id=user.id,
        ip=client_ip(request),
        current_day=today(_tz(ctx)),
    )
    await session.commit()
    await session.refresh(sub)
    return service.subscription_out(sub, await service.get_plan(session, sub.plan_id))


@platform_router.put("/tenants/{tenant_id}/overrides", response_model=TenantOverrides)
async def set_overrides(
    tenant_id: UUID,
    payload: TenantOverrides,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
) -> TenantOverrides:
    """按租户覆盖套餐的额度和功能开关（整体替换；没有列出的项按套餐）。"""
    tenant = await tenancy.get_tenant(session, tenant_id)
    service.set_overrides(session, tenant, payload, actor_id=user.id, ip=client_ip(request))
    await session.commit()
    return payload


# ---- 平台：账单 ----


@platform_router.get("/invoices", response_model=InvoiceList)
async def list_invoices(
    session: PlatformDb,
    _: CurrentPlatformUser,
    tenant_id: UUID | None = None,
    invoice_status: Annotated[
        Literal["issued", "paid", "void"] | None, Query(alias="status")
    ] = None,
    month: Annotated[date | None, Query(description="该月任意一天")] = None,
) -> InvoiceList:
    items = await service.list_invoices(
        session, tenant_id=tenant_id, status=invoice_status, month=month
    )
    return InvoiceList(items=items)


@platform_router.patch("/invoices/{invoice_id}", response_model=InvoiceOut)
async def update_invoice(
    invoice_id: UUID,
    payload: InvoiceUpdate,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
) -> InvoiceOut:
    """标记已收款、作废，或修改备注。"""
    return await service.update_invoice(
        session,
        invoice_id,
        status=payload.status,
        note=payload.note,
        note_set="note" in payload.model_fields_set,
        actor_id=user.id,
        ip=client_ip(request),
    )


@platform_router.post("/invoices/generate", response_model=InvoiceGenerateResult)
async def generate_invoices(
    payload: InvoiceGenerate,
    request: Request,
    session: PlatformDb,
    user: CurrentPlatformUser,
    ctx: ContextDep,
) -> InvoiceGenerateResult:
    """生成或重算某月账单（已收款、已作废的不变）。调度任务每天为上个月执行一次。"""
    year, month = (int(part) for part in payload.month.split("-"))
    result = await service.generate_invoices(ctx, date(year, month, 1))
    record_audit(
        session,
        action="invoice.generate",
        actor_type="platform",
        actor_id=user.id,
        resource_type="invoice",
        detail={"month": payload.month, **result.model_dump()},
        ip=client_ip(request),
    )
    await session.commit()
    return result
