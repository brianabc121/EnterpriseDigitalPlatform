"""AI 唤醒的接口（设计文档 §33.10）：/api/v1/wake。

"AI 唤醒"页面给有"设置"权限的员工（默认管理员），需要套餐包含 AI；首页"需要我处理的问题"每个人
都能看自己负责的、处理自己负责的。
"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.core.deps import client_ip
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.billing.entitlements import require_feature
from app.modules.iam.deps import CurrentPrincipal, TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.wake import service
from app.modules.wake.schemas import (
    FindingOut,
    FindingPage,
    FindingStatusValue,
    FindingView,
    IgnoreRequest,
    ResolveRequest,
    RunKindValue,
    RunOut,
    RunPage,
    RunRequest,
    SeverityValue,
    WakeOverview,
    WakeSettingsOut,
)
from app.modules.wake.settings import WakeSettings

router = APIRouter(prefix="/api/v1/wake", tags=["wake"], responses=ERROR_RESPONSES)


async def _admin(
    principal: Annotated[Principal, Depends(require_permission(Permission.SETTINGS_MANAGE))],
    session: TenantDb,
) -> Principal:
    await require_feature(session, principal.tenant_id, "ai")
    return principal


Admin = Annotated[Principal, Depends(_admin)]


@router.get("/overview", response_model=WakeOverview)
async def overview(session: TenantDb, principal: Admin) -> WakeOverview:
    """下次唤醒的时间、最近一次简报和知识库整理、各级别的问题数、增量更新索引最近的变化。"""
    return await service.overview(session, principal, datetime.now(UTC))


@router.get("/findings", response_model=FindingPage)
async def list_findings(
    session: TenantDb,
    principal: CurrentPrincipal,
    view: Annotated[FindingView, Query(description="mine 自己负责的；all 全部（管理员）")] = "mine",
    status: Annotated[FindingStatusValue | None, Query()] = None,
    category: Annotated[str | None, Query(max_length=16)] = None,
    severity: Annotated[SeverityValue | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FindingPage:
    """巡检发现的问题：待处理的在前，按级别和发现的先后排列。"""
    return await service.list_findings(
        session,
        principal,
        view=view,
        status=status,
        category=category,
        severity=severity,
        limit=limit,
        offset=offset,
    )


@router.post("/findings/{finding_id}/ignore", response_model=FindingOut)
async def ignore_finding(
    finding_id: UUID,
    payload: IgnoreRequest,
    request: Request,
    session: TenantDb,
    principal: CurrentPrincipal,
) -> FindingOut:
    """忽略：几天内不再提醒（到期后仍然存在就重新打开），或者一直忽略。负责人或管理员。"""
    out = await service.ignore(
        session, principal, finding_id, payload, datetime.now(UTC), client_ip(request)
    )
    await session.commit()
    return out


@router.post("/findings/{finding_id}/resolve", response_model=FindingOut)
async def resolve_finding(
    finding_id: UUID,
    request: Request,
    session: TenantDb,
    principal: CurrentPrincipal,
    payload: ResolveRequest | None = None,
) -> FindingOut:
    """标记已处理：下次检查仍然发现就重新打开。负责人或管理员。"""
    out = await service.resolve(
        session,
        principal,
        finding_id,
        payload or ResolveRequest(),
        datetime.now(UTC),
        client_ip(request),
    )
    await session.commit()
    return out


@router.get("/runs", response_model=RunPage)
async def list_runs(
    session: TenantDb,
    _: Admin,
    kind: Annotated[RunKindValue | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunPage:
    """唤醒记录（新的在前）。"""
    return await service.list_runs(session, kind=kind, limit=limit, offset=offset)


@router.post("/runs", response_model=RunOut)
async def run_now(
    payload: RunRequest, request: Request, session: TenantDb, principal: Admin
) -> RunOut:
    """立即唤醒：daily 立即巡检（全部检查项重新检查、写简报），kb 立即整理知识库。实时消费
    进程几秒内开始执行。"""
    out = await service.run_now(
        session, principal, payload.kind, datetime.now(UTC), client_ip(request)
    )
    await session.commit()
    return out


@router.get("/settings", response_model=WakeSettingsOut)
async def get_settings(session: TenantDb, principal: Admin) -> WakeSettingsOut:
    """设置，以及检查项的目录（名称、说明、数字的默认值和范围、读的数据表、最近检查的时间）。"""
    return await service.settings_out(session, principal.tenant_id)


@router.put("/settings", response_model=WakeSettingsOut)
async def update_settings(
    payload: WakeSettings, request: Request, session: TenantDb, principal: Admin
) -> WakeSettingsOut:
    out = await service.save_settings(session, principal, payload, client_ip(request))
    await session.commit()
    return out
