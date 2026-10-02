"""意向客户的接口（设计文档 §35.9）：/api/v1/prospects。

看得到客户就能看到、跟进他的意向记录（customer:read）；把跟进人改成别人、意向客户设置需要
customer:assign。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.prospects import ai, service
from app.modules.prospects import settings as prospect_settings
from app.modules.prospects.schemas import (
    CustomerProspectInfo,
    FollowupCreate,
    ProspectCreate,
    ProspectLevelValue,
    ProspectLost,
    ProspectMessage,
    ProspectOut,
    ProspectPage,
    ProspectReopen,
    ProspectSettingsOut,
    ProspectSourceValue,
    ProspectUpdate,
    ProspectView,
    ProspectWon,
)
from app.modules.prospects.settings import ProspectSettings

router = APIRouter(prefix="/api/v1/prospects", tags=["prospects"], responses=ERROR_RESPONSES)

Context = Annotated[AppContext, Depends(get_context)]
CanRead = Annotated[Principal, Depends(require_permission(Permission.CUSTOMER_READ))]
CanAssign = Annotated[Principal, Depends(require_permission(Permission.CUSTOMER_ASSIGN))]


@router.get("", response_model=ProspectPage)
async def list_prospects(
    ctx: Context,
    session: TenantDb,
    principal: CanRead,
    view: Annotated[ProspectView, Query(description="页签")] = "active",
    level: Annotated[ProspectLevelValue | None, Query()] = None,
    source: Annotated[ProspectSourceValue | None, Query()] = None,
    follower_id: Annotated[UUID | None, Query()] = None,
    customer_id: Annotated[UUID | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100, description="客户名称、公司、手机号")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ProspectPage:
    """看得到的意向客户，以及各页签的数量和本月成交数。"""
    return await service.list_prospects(
        session,
        ctx.keys,
        principal,
        view=view,
        level=level,
        source=source,
        follower_id=follower_id,
        customer_id=customer_id,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=ProspectOut, status_code=status.HTTP_201_CREATED)
async def create_prospect(
    payload: ProspectCreate, request: Request, session: TenantDb, principal: CanRead
) -> ProspectOut:
    """把客户转入意向客户（同一客户只能有一条待确认或跟进中的）。"""
    prospect = await service.create(session, principal, payload, ip=client_ip(request))
    return await service.out(session, principal, prospect)


@router.get("/settings", response_model=ProspectSettingsOut)
async def get_settings(session: TenantDb, principal: CanRead) -> ProspectSettingsOut:
    return ProspectSettingsOut(
        settings=await prospect_settings.load(session, principal.tenant_id),
        can_edit=principal.has(Permission.CUSTOMER_ASSIGN),
    )


@router.put("/settings", response_model=ProspectSettingsOut)
async def save_settings(
    payload: ProspectSettings, request: Request, session: TenantDb, principal: CanAssign
) -> ProspectSettingsOut:
    value = await prospect_settings.save(session, principal.tenant_id, payload, principal.staff_id)
    record_audit(
        session,
        action="prospect.settings",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="tenant_settings",
        detail=value.model_dump(),
        ip=client_ip(request),
    )
    await session.commit()
    return ProspectSettingsOut(settings=value, can_edit=True)


@router.get("/customer/{customer_id}", response_model=CustomerProspectInfo)
async def customer_prospect(
    customer_id: UUID, session: TenantDb, principal: CanRead
) -> CustomerProspectInfo:
    """客户资料里的意向：最近的一条（待确认、跟进中的优先），以及最近 30 天确认的订单。"""
    return await service.customer_info(session, principal, customer_id)


@router.get("/{prospect_id}", response_model=ProspectOut)
async def get_prospect(prospect_id: UUID, session: TenantDb, principal: CanRead) -> ProspectOut:
    prospect = await service.get_visible(session, principal, prospect_id)
    return await service.out(session, principal, prospect)


@router.patch("/{prospect_id}", response_model=ProspectOut)
async def update_prospect(
    prospect_id: UUID,
    payload: ProspectUpdate,
    request: Request,
    session: TenantDb,
    principal: CanRead,
) -> ProspectOut:
    """修改等级、想要什么、顾虑、下次跟进日期、跟进人（改成别人需要分配客户的权限）。"""
    prospect = await service.update(session, principal, prospect_id, payload, ip=client_ip(request))
    return await service.out(session, principal, prospect)


@router.post("/{prospect_id}/followups", response_model=ProspectOut)
async def add_followup(
    prospect_id: UUID, payload: FollowupCreate, session: TenantDb, principal: CanRead
) -> ProspectOut:
    """记一次跟进：下次跟进日期不填时按默认天数。"""
    prospect = await service.add_followup(session, principal, prospect_id, payload)
    return await service.out(session, principal, prospect)


@router.post("/{prospect_id}/won", response_model=ProspectOut)
async def mark_won(
    prospect_id: UUID,
    request: Request,
    session: TenantDb,
    principal: CanRead,
    payload: ProspectWon | None = None,
) -> ProspectOut:
    """手动标记成交（可以关联这个客户的订单）。"""
    order_id = payload.order_id if payload else None
    prospect = await service.won(session, principal, prospect_id, order_id, ip=client_ip(request))
    return await service.out(session, principal, prospect)


@router.post("/{prospect_id}/lost", response_model=ProspectOut)
async def mark_lost(
    prospect_id: UUID,
    payload: ProspectLost,
    request: Request,
    session: TenantDb,
    principal: CanRead,
) -> ProspectOut:
    prospect = await service.lost(
        session, principal, prospect_id, payload.reason, ip=client_ip(request)
    )
    return await service.out(session, principal, prospect)


@router.post("/{prospect_id}/reopen", response_model=ProspectOut)
async def reopen(
    prospect_id: UUID,
    request: Request,
    session: TenantDb,
    principal: CanRead,
    payload: ProspectReopen | None = None,
) -> ProspectOut:
    """已成交、已放弃的重新跟进（下次跟进日期不填时按默认天数）。"""
    next_at = payload.next_follow_at if payload else None
    prospect = await service.reopen(session, principal, prospect_id, next_at, ip=client_ip(request))
    return await service.out(session, principal, prospect)


@router.post("/{prospect_id}/accept", response_model=ProspectOut)
async def accept(
    prospect_id: UUID, request: Request, session: TenantDb, principal: CanRead
) -> ProspectOut:
    """确认 AI 的建议，转入意向客户。"""
    prospect = await service.accept(session, principal, prospect_id, ip=client_ip(request))
    return await service.out(session, principal, prospect)


@router.post(
    "/{prospect_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def dismiss(
    prospect_id: UUID, request: Request, session: TenantDb, principal: CanRead
) -> None:
    """忽略 AI 的建议（30 天内不再建议这个客户）。"""
    await service.dismiss(session, principal, prospect_id, ip=client_ip(request))


@router.post("/{prospect_id}/message", response_model=ProspectMessage)
async def write_message(
    prospect_id: UUID, ctx: Context, session: TenantDb, principal: CanRead
) -> ProspectMessage:
    """AI 写跟进话术（需要套餐包含 AI）：员工修改后自己发送。"""
    prospect = await service.get_visible(session, principal, prospect_id)
    return await ai.message(ctx, session, principal, prospect)
