"""个人待办接口（设计文档 §27.6）。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.tasks import service
from app.modules.tasks import settings as task_settings
from app.modules.tasks.models import TaskStatus
from app.modules.tasks.schemas import (
    StaffOptions,
    TaskCounts,
    TaskCreate,
    TaskDoneRequest,
    TaskDueFilter,
    TaskOut,
    TaskOverview,
    TaskPage,
    TaskUpdate,
    TaskView,
)
from app.modules.tasks.settings import TaskSettings

router = APIRouter(prefix="/api/v1", tags=["tasks"], responses=ERROR_RESPONSES)

CanUse = Annotated[Principal, Depends(require_permission(Permission.TASK_USE))]
CanManageSettings = Annotated[Principal, Depends(require_permission(Permission.SETTINGS_MANAGE))]
Context = Annotated[AppContext, Depends(get_context)]


@router.get("/tasks", response_model=TaskPage)
async def list_tasks(
    session: TenantDb,
    principal: CanUse,
    view: Annotated[
        TaskView,
        Query(description="mine 我的；assigned 我交办给别人的；all 全员（task:read_all）"),
    ] = "mine",
    owner_id: UUID | None = None,
    status: TaskStatus | None = None,
    due: Annotated[
        TaskDueFilter | None, Query(description="overdue 已逾期；today 今日到期；soon 24 小时内")
    ] = None,
    q: Annotated[str | None, Query(max_length=64, description="按编号或标题搜索")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TaskPage:
    return await service.list_tasks(
        session,
        principal,
        view=view,
        owner_id=owner_id,
        status=status,
        due=due,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/tasks/counts", response_model=TaskCounts)
async def counts(session: TenantDb, principal: CanUse) -> TaskCounts:
    """菜单角标：我的未完成、今日到期、已逾期；另有分派给我的客户待办数。"""
    return await service.counts(session, principal)


@router.get("/tasks/overview", response_model=TaskOverview)
async def overview(session: TenantDb, principal: CanUse) -> TaskOverview:
    """全员视图（task:read_all）：每个在职员工的未完成、今日到期、已逾期。"""
    return await service.overview(session, principal)


@router.get("/tasks/staff", response_model=StaffOptions)
async def staff_options(session: TenantDb, _: CanUse) -> StaffOptions:
    """交办时可以选择的员工。"""
    return await service.staff_options(session)


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate, ctx: Context, session: TenantDb, principal: CanUse
) -> TaskOut:
    """新建自己的事项，或交办给别人（task:assign，接收人收到提醒）。"""
    task = await service.create(ctx, session, principal, payload)
    return await service.out(session, principal, task)


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: UUID, session: TenantDb, principal: CanUse) -> TaskOut:
    return await service.out(
        session, principal, await service.get_visible(session, principal, task_id)
    )


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: UUID, payload: TaskUpdate, session: TenantDb, principal: CanUse
) -> TaskOut:
    return await service.out(
        session, principal, await service.update(session, principal, task_id, payload)
    )


@router.post("/tasks/{task_id}/done", response_model=TaskOut)
async def done_task(
    task_id: UUID, session: TenantDb, principal: CanUse, payload: TaskDoneRequest | None = None
) -> TaskOut:
    task = await service.done(session, principal, task_id, note=payload.note if payload else None)
    return await service.out(session, principal, task)


@router.post("/tasks/{task_id}/reopen", response_model=TaskOut)
async def reopen_task(task_id: UUID, session: TenantDb, principal: CanUse) -> TaskOut:
    return await service.out(session, principal, await service.reopen(session, principal, task_id))


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: UUID, session: TenantDb, principal: CanUse) -> TaskOut:
    return await service.out(session, principal, await service.cancel(session, principal, task_id))


@router.get("/tenant/tasks-settings", response_model=TaskSettings)
async def get_settings(session: TenantDb, principal: CanManageSettings) -> TaskSettings:
    return await task_settings.load(session, principal.tenant_id)


@router.put("/tenant/tasks-settings", response_model=TaskSettings)
async def put_settings(
    payload: TaskSettings, request: Request, session: TenantDb, principal: CanManageSettings
) -> TaskSettings:
    value = await task_settings.save(session, principal.tenant_id, payload, principal.staff_id)
    record_audit(
        session,
        action="tasks.settings.update",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="tenant_settings",
        resource_id=str(principal.tenant_id),
        detail=payload.model_dump(),
        ip=client_ip(request),
    )
    await session.commit()
    return value
