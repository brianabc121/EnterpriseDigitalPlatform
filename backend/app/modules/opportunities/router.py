"""商机的接口（设计文档 §40.13）：挂在 /api/v1/opportunities
（main.py）。

查看需要 opportunity:read（看得到客户的，或者自己是负责人）；新建、修改、跟进、换阶段、赢单 / 输单
需要 opportunity:manage；把负责人改成别人、商机设置和阶段需要 opportunity:assign。
"""

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.opportunities import ai, service
from app.modules.opportunities import export as opportunity_export
from app.modules.opportunities import settings as opportunity_settings
from app.modules.opportunities.schemas import (
    CustomerOpportunityInfo,
    FollowupCreate,
    NextStep,
    OpportunityActivityOut,
    OpportunityAssign,
    OpportunityBoard,
    OpportunityCreate,
    OpportunityDigest,
    OpportunityLevelValue,
    OpportunityLost,
    OpportunityMessage,
    OpportunityOut,
    OpportunityPage,
    OpportunityReopen,
    OpportunitySettingsOut,
    OpportunitySourceValue,
    OpportunityStats,
    OpportunityUpdate,
    OpportunityView,
    OpportunityWon,
    StageCreate,
    StageMove,
    StageOrder,
    StageOut,
    StageUpdate,
)
from app.modules.opportunities.settings import OpportunitySettings

router = APIRouter(tags=["opportunities"], responses=ERROR_RESPONSES)

Context = Annotated[AppContext, Depends(get_context)]
CanRead = Annotated[Principal, Depends(require_permission(Permission.OPPORTUNITY_READ))]
CanManage = Annotated[Principal, Depends(require_permission(Permission.OPPORTUNITY_MANAGE))]
CanAssign = Annotated[Principal, Depends(require_permission(Permission.OPPORTUNITY_ASSIGN))]
CanExport = Annotated[Principal, Depends(require_permission(Permission.OPPORTUNITY_EXPORT))]


@router.get("", response_model=OpportunityPage)
async def list_opportunities(
    ctx: Context,
    session: TenantDb,
    principal: CanRead,
    view: Annotated[OpportunityView, Query(description="快捷视图")] = "active",
    stage_id: Annotated[UUID | None, Query()] = None,
    level: Annotated[OpportunityLevelValue | None, Query()] = None,
    source: Annotated[OpportunitySourceValue | None, Query()] = None,
    owner_id: Annotated[UUID | None, Query()] = None,
    customer_id: Annotated[UUID | None, Query()] = None,
    amount_min: Annotated[Decimal | None, Query(ge=0)] = None,
    amount_max: Annotated[Decimal | None, Query(ge=0)] = None,
    close_month: Annotated[
        str | None, Query(max_length=7, description="预计成交月份 YYYY-MM")
    ] = None,
    stale: Annotated[bool, Query(description="只看停滞的")] = False,
    q: Annotated[
        str | None, Query(max_length=100, description="商机名称、客户名称、公司、手机号")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OpportunityPage:
    """看得到的商机，以及各快捷视图的数量和本月赢单数。"""
    return await service.list_opportunities(
        session,
        ctx.keys,
        principal,
        view=view,
        stage_id=stage_id,
        level=level,
        source=source,
        owner_id=owner_id,
        customer_id=customer_id,
        amount_min=amount_min,
        amount_max=amount_max,
        close_month=close_month,
        stale=stale,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/board", response_model=OpportunityBoard)
async def board(
    ctx: Context,
    session: TenantDb,
    principal: CanRead,
    level: Annotated[OpportunityLevelValue | None, Query()] = None,
    source: Annotated[OpportunitySourceValue | None, Query()] = None,
    owner_id: Annotated[UUID | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    mine: Annotated[bool, Query(description="只看我负责的")] = False,
) -> OpportunityBoard:
    """看板：每个阶段一列，列头有数量和预计金额合计。"""
    return await service.board(
        session, ctx.keys, principal, level=level, source=source, owner_id=owner_id, q=q, mine=mine
    )


@router.get("/stats", response_model=OpportunityStats)
async def stats(session: TenantDb, principal: CanRead) -> OpportunityStats:
    """顶部数字（首页的四个数字也从这里取：我负责的、本周要跟进、停滞、本月赢单金额）。"""
    return await service.stats(session, principal)


@router.get(
    "/export",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/csv": {}}, "description": "CSV 文件"}},
)
async def export_opportunities(
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanExport,
    view: OpportunityView = "all",
    stage_id: UUID | None = None,
    owner_id: UUID | None = None,
    q: str | None = Query(default=None, max_length=100),
) -> StreamingResponse:
    """导出查看范围内、符合筛选条件的商机（CSV）。预计金额设置为只有管理者可见而自己不能看时，
    金额列为空。"""
    conditions = await service.export_conditions(
        session, ctx.keys, principal, view=view, stage_id=stage_id, owner_id=owner_id, q=q
    )
    record_audit(
        session,
        action="opportunity.export",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="opportunity",
        detail={"view": view, "q": q},
        ip=client_ip(request),
    )
    await session.commit()
    name = f"opportunities-{date.today():%Y%m%d}.csv"
    return StreamingResponse(
        opportunity_export.rows(ctx, principal, conditions),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("", response_model=OpportunityOut, status_code=status.HTTP_201_CREATED)
async def create_opportunity(
    payload: OpportunityCreate, request: Request, session: TenantDb, principal: CanManage
) -> OpportunityOut:
    """新建商机（把客户转入；同一客户只能有一条待确认或跟进中的）。"""
    opportunity = await service.create(session, principal, payload, ip=client_ip(request))
    return await service.out(session, principal, opportunity)


@router.get("/settings", response_model=OpportunitySettingsOut)
async def get_settings(session: TenantDb, principal: CanRead) -> OpportunitySettingsOut:
    return OpportunitySettingsOut(
        settings=await opportunity_settings.load(session, principal.tenant_id),
        stages=[service.stage_out(s) for s in await service.stages(session, principal.tenant_id)],
        can_edit=principal.has(Permission.OPPORTUNITY_ASSIGN),
    )


@router.put("/settings", response_model=OpportunitySettingsOut)
async def save_settings(
    payload: OpportunitySettings, request: Request, session: TenantDb, principal: CanAssign
) -> OpportunitySettingsOut:
    value = await opportunity_settings.save(
        session, principal.tenant_id, payload, principal.staff_id
    )
    record_audit(
        session,
        action="opportunity.settings",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="tenant_settings",
        detail=value.model_dump(mode="json"),
        ip=client_ip(request),
    )
    await session.commit()
    return OpportunitySettingsOut(
        settings=value,
        stages=[service.stage_out(s) for s in await service.stages(session, principal.tenant_id)],
        can_edit=True,
    )


@router.get("/stages", response_model=list[StageOut])
async def list_stages(session: TenantDb, principal: CanRead) -> list[StageOut]:
    return [service.stage_out(s) for s in await service.stages(session, principal.tenant_id)]


@router.post("/stages", response_model=StageOut, status_code=status.HTTP_201_CREATED)
async def create_stage(
    payload: StageCreate, request: Request, session: TenantDb, principal: CanAssign
) -> StageOut:
    """新增一个进行中的阶段。"""
    return service.stage_out(
        await service.create_stage(session, principal, payload, ip=client_ip(request))
    )


@router.put("/stages/order", response_model=list[StageOut])
async def reorder_stages(
    payload: StageOrder, request: Request, session: TenantDb, principal: CanAssign
) -> list[StageOut]:
    """调整进行中的阶段的先后（列出全部进行中的阶段）。"""
    rows = await service.reorder_stages(session, principal, payload.ids, ip=client_ip(request))
    return [service.stage_out(s) for s in rows]


@router.patch("/stages/{stage_id}", response_model=StageOut)
async def update_stage(
    stage_id: UUID, payload: StageUpdate, request: Request, session: TenantDb, principal: CanAssign
) -> StageOut:
    return service.stage_out(
        await service.update_stage(session, principal, stage_id, payload, ip=client_ip(request))
    )


@router.delete(
    "/stages/{stage_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
async def delete_stage(
    stage_id: UUID,
    request: Request,
    session: TenantDb,
    principal: CanAssign,
    merge_into: Annotated[UUID | None, Query(description="这个阶段的商机并到哪个阶段")] = None,
) -> None:
    """删除一个进行中的阶段：还有商机时要指定并到哪个阶段。"""
    await service.delete_stage(session, principal, stage_id, merge_into, ip=client_ip(request))


@router.get("/customer/{customer_id}", response_model=CustomerOpportunityInfo)
async def customer_opportunity(
    customer_id: UUID, session: TenantDb, principal: CanRead
) -> CustomerOpportunityInfo:
    """客户资料里的商机：最近的一条（待确认、跟进中的优先），以及最近 30 天确认的订单。"""
    return await service.customer_info(session, principal, customer_id)


@router.get("/{opportunity_id}", response_model=OpportunityOut)
async def get_opportunity(
    opportunity_id: UUID, session: TenantDb, principal: CanRead
) -> OpportunityOut:
    opportunity = await service.get_visible(session, principal, opportunity_id)
    return await service.out(session, principal, opportunity)


@router.patch("/{opportunity_id}", response_model=OpportunityOut)
async def update_opportunity(
    opportunity_id: UUID,
    payload: OpportunityUpdate,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> OpportunityOut:
    """修改名称、等级、想要什么、顾虑、预计金额、预计成交日、概率、下次跟进日期、关联商品、
    负责人（改成别人需要分配商机的权限，会提醒新负责人）。"""
    opportunity = await service.update(
        session, principal, opportunity_id, payload, ip=client_ip(request), ctx=ctx
    )
    return await service.out(session, principal, opportunity)


@router.get("/{opportunity_id}/activities", response_model=list[OpportunityActivityOut])
async def list_activities(
    opportunity_id: UUID, session: TenantDb, principal: CanRead
) -> list[OpportunityActivityOut]:
    """时间线：跟进、备注和系统记的事件，新的在前。"""
    opportunity = await service.get_visible(session, principal, opportunity_id)
    return await service.activities(session, opportunity.id)


@router.post("/{opportunity_id}/activities", response_model=OpportunityOut)
@router.post("/{opportunity_id}/followups", response_model=OpportunityOut)
async def add_activity(
    opportunity_id: UUID, payload: FollowupCreate, session: TenantDb, principal: CanManage
) -> OpportunityOut:
    """记一次跟进（下次跟进日期不填时按默认天数）或备注。"""
    opportunity = await service.add_followup(session, principal, opportunity_id, payload)
    return await service.out(session, principal, opportunity)


@router.post("/{opportunity_id}/stage", response_model=OpportunityOut)
async def move_stage(
    opportunity_id: UUID,
    payload: StageMove,
    request: Request,
    session: TenantDb,
    principal: CanManage,
) -> OpportunityOut:
    """换阶段（看板拖拽）：拖到赢单可以带订单或合同，拖到输单要选原因。"""
    opportunity = await service.move_stage(
        session, principal, opportunity_id, payload, ip=client_ip(request)
    )
    return await service.out(session, principal, opportunity)


@router.post("/{opportunity_id}/won", response_model=OpportunityOut)
async def mark_won(
    opportunity_id: UUID,
    request: Request,
    session: TenantDb,
    principal: CanManage,
    payload: OpportunityWon | None = None,
) -> OpportunityOut:
    """手动赢单（可以关联这个客户的订单或合同）。"""
    opportunity = await service.won(
        session,
        principal,
        opportunity_id,
        order_id=payload.order_id if payload else None,
        contract_id=payload.contract_id if payload else None,
        note=payload.note if payload else None,
        ip=client_ip(request),
    )
    return await service.out(session, principal, opportunity)


@router.post("/{opportunity_id}/lost", response_model=OpportunityOut)
async def mark_lost(
    opportunity_id: UUID,
    payload: OpportunityLost,
    request: Request,
    session: TenantDb,
    principal: CanManage,
) -> OpportunityOut:
    """输单：选原因分类，可以写说明。"""
    opportunity = await service.lost(
        session,
        principal,
        opportunity_id,
        reason_code=payload.reason_code,
        reason=payload.reason,
        ip=client_ip(request),
    )
    return await service.out(session, principal, opportunity)


@router.post("/{opportunity_id}/reopen", response_model=OpportunityOut)
async def reopen(
    opportunity_id: UUID,
    request: Request,
    session: TenantDb,
    principal: CanManage,
    payload: OpportunityReopen | None = None,
) -> OpportunityOut:
    """重新跟进：输单的回到已沟通；赢单的新开一条商机（返回新的那条）。"""
    next_at = payload.next_follow_at if payload else None
    opportunity = await service.reopen(
        session, principal, opportunity_id, next_at, ip=client_ip(request)
    )
    return await service.out(session, principal, opportunity)


@router.post("/{opportunity_id}/accept", response_model=OpportunityOut)
async def accept(
    opportunity_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> OpportunityOut:
    """确认 AI 的建议，转入商机。"""
    opportunity = await service.accept(session, principal, opportunity_id, ip=client_ip(request))
    return await service.out(session, principal, opportunity)


@router.post(
    "/{opportunity_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def dismiss(
    opportunity_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> None:
    """忽略 AI 的建议（30 天内不再建议这个客户）。"""
    await service.dismiss(session, principal, opportunity_id, ip=client_ip(request))


@router.post("/{opportunity_id}/assign", response_model=OpportunityOut)
async def assign(
    opportunity_id: UUID,
    payload: OpportunityAssign,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> OpportunityOut:
    """换负责人（改成别人需要分配商机的权限，会提醒新负责人）。"""
    opportunity = await service.assign(
        session, principal, opportunity_id, payload.owner_id, ip=client_ip(request), ctx=ctx
    )
    return await service.out(session, principal, opportunity)


@router.post("/{opportunity_id}/message", response_model=OpportunityMessage)
async def write_message(
    opportunity_id: UUID, ctx: Context, session: TenantDb, principal: CanManage
) -> OpportunityMessage:
    """AI 写跟进话术（需要套餐包含 AI）：员工修改后自己发送。"""
    opportunity = await service.get_visible(session, principal, opportunity_id)
    return await ai.message(ctx, session, principal, opportunity)


@router.post("/{opportunity_id}/summary", response_model=OpportunityDigest)
async def write_summary(
    opportunity_id: UUID, ctx: Context, session: TenantDb, principal: CanManage
) -> OpportunityDigest:
    """AI 小结（需要套餐包含 AI）：现在到哪一步、客户在意什么、建议下一步；同时记进时间线。"""
    opportunity = await service.get_visible(session, principal, opportunity_id)
    return await ai.summary(ctx, session, principal, opportunity)


@router.post("/{opportunity_id}/todos", response_model=OpportunityOut)
async def schedule_next(
    opportunity_id: UUID,
    payload: NextStep,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> OpportunityOut:
    """安排下一步：建一条关联这条商机的待办（默认"回电 / 回访"，处理人默认是负责人），可以同时改
    下次跟进日期。"""
    opportunity = await service.schedule_todo(session, ctx.keys, principal, opportunity_id, payload)
    return await service.out(session, principal, opportunity)
