"""企业 token 计费的接口（设计文档 §37.6）：/api/v1/tokens，需要 token:view。"""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.tokens import service
from app.modules.tokens.schemas import TokenCallPage, TokenStatusFilter, TokenSummary

router = APIRouter(prefix="/api/v1/tokens", tags=["tokens"], responses=ERROR_RESPONSES)

Context = Annotated[AppContext, Depends(get_context)]
CanView = Annotated[Principal, Depends(require_permission(Permission.TOKEN_VIEW))]
Scene = Annotated[str | None, Query(max_length=32, description="场景")]
Status = Annotated[TokenStatusFilter | None, Query(description="ok 成功、failed 失败")]
StaffFilter = Annotated[
    str | None, Query(max_length=36, description="员工 ID；system 为系统（AI 接待、定时任务）")
]


@router.get("/summary", response_model=TokenSummary)
async def get_summary(
    session: TenantDb,
    principal: CanView,
    month: Annotated[
        str | None, Query(pattern=r"^\d{4}-\d{2}$", description="YYYY-MM，默认本月")
    ] = None,
) -> TokenSummary:
    """一个月的 tokens 和费用：合计、上月同期、每天、按场景、按模型、按员工。"""
    return await service.summary(session, principal, month)


@router.get("/calls", response_model=TokenCallPage)
async def list_calls(
    session: TenantDb,
    principal: CanView,
    scene: Scene = None,
    status: Status = None,
    staff_id: StaffFilter = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TokenCallPage:
    """近 7 天每一次大模型调用（新的在前）。"""
    filters = service.CallFilters(scene=scene, status=status, staff_id=staff_id)
    return await service.calls(session, filters, limit=limit, offset=offset)


@router.get("/calls/export", response_class=StreamingResponse)
async def export_calls(
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanView,
    scene: Scene = None,
    status: Status = None,
    staff_id: StaffFilter = None,
) -> StreamingResponse:
    """导出近 7 天的明细（CSV，Excel 直接打开）。每次导出记操作日志。"""
    filters = service.CallFilters(scene=scene, status=status, staff_id=staff_id)
    rows = await service.export_count(session, filters)
    record_audit(
        session,
        action="token.export",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="llm_call",
        detail={"rows": rows, "scene": scene, "status": status, "staff_id": staff_id},
        ip=client_ip(request),
    )
    await session.commit()
    name = quote("token明细-近7天.csv")
    return StreamingResponse(
        service.export_rows(ctx, principal, filters),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"},
    )
