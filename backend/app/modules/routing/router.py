from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES, Forbidden, ServiceUnavailable
from app.core.permissions import Permission
from app.integrations.openim import OpenIMError
from app.modules.iam.deps import CurrentPrincipal, TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.routing import service
from app.modules.routing.schemas import (
    AgentIMCredentials,
    AgentList,
    AgentUpdate,
    MyAgentState,
    MyAgentStatusUpdate,
    RoutingPolicyCreate,
    RoutingPolicyList,
    RoutingPolicyOut,
    RoutingPolicyUpdate,
    SkillGroupCreate,
    SkillGroupList,
    SkillGroupOut,
    SkillGroupUpdate,
)

router = APIRouter(prefix="/api/v1", tags=["routing"], responses=ERROR_RESPONSES)

CanManage = Annotated[Principal, Depends(require_permission(Permission.ROUTING_MANAGE))]
CanServe = Annotated[Principal, Depends(require_permission(Permission.WORKBENCH_USE))]
Context = Annotated[AppContext, Depends(get_context)]


# ---- 技能组 ----


@router.get("/skill-groups", response_model=SkillGroupList)
async def list_skill_groups(session: TenantDb, _: CanManage) -> SkillGroupList:
    return SkillGroupList(items=await service.list_skill_groups(session))


@router.post("/skill-groups", response_model=SkillGroupOut, status_code=status.HTTP_201_CREATED)
async def create_skill_group(
    payload: SkillGroupCreate, request: Request, session: TenantDb, principal: CanManage
) -> SkillGroupOut:
    return await service.create_skill_group(session, principal, payload, ip=client_ip(request))


@router.patch("/skill-groups/{group_id}", response_model=SkillGroupOut)
async def update_skill_group(
    group_id: UUID,
    payload: SkillGroupUpdate,
    request: Request,
    session: TenantDb,
    principal: CanManage,
) -> SkillGroupOut:
    return await service.update_skill_group(
        session, principal, group_id, payload, ip=client_ip(request)
    )


@router.delete("/skill-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill_group(
    group_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> Response:
    await service.delete_skill_group(session, principal, group_id, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- 路由策略 ----


@router.get("/routing-policies", response_model=RoutingPolicyList)
async def list_policies(session: TenantDb, _: CanManage) -> RoutingPolicyList:
    return RoutingPolicyList(items=await service.list_policies(session))


@router.post(
    "/routing-policies", response_model=RoutingPolicyOut, status_code=status.HTTP_201_CREATED
)
async def create_policy(
    payload: RoutingPolicyCreate, request: Request, session: TenantDb, principal: CanManage
) -> RoutingPolicyOut:
    return await service.create_policy(session, principal, payload, ip=client_ip(request))


@router.patch("/routing-policies/{policy_id}", response_model=RoutingPolicyOut)
async def update_policy(
    policy_id: UUID,
    payload: RoutingPolicyUpdate,
    request: Request,
    session: TenantDb,
    principal: CanManage,
) -> RoutingPolicyOut:
    return await service.update_policy(
        session, principal, policy_id, payload, ip=client_ip(request)
    )


@router.delete("/routing-policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> Response:
    await service.delete_policy(session, principal, policy_id, ip=client_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- 坐席看板 ----


@router.get("/agents", response_model=AgentList)
async def list_agents(session: TenantDb, principal: CurrentPrincipal) -> AgentList:
    """坐席状态与负载。主管（session:read_team）只看到所带技能组的成员。"""
    if not any(
        principal.has(p)
        for p in (
            Permission.ROUTING_MANAGE,
            Permission.SESSION_READ_ALL,
            Permission.SESSION_READ_TEAM,
        )
    ):
        raise Forbidden("没有执行该操作的权限")
    return await service.list_agents(session, principal)


@router.patch("/agents/{staff_id}", status_code=status.HTTP_204_NO_CONTENT)
async def update_agent(
    staff_id: UUID,
    payload: AgentUpdate,
    request: Request,
    session: TenantDb,
    principal: CanManage,
) -> Response:
    await service.update_agent(
        session, principal, staff_id, payload.max_concurrency, ip=client_ip(request)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- 我的接待状态（坐席工作台） ----


@router.get("/agent/state", response_model=MyAgentState)
async def my_state(session: TenantDb, principal: CanServe) -> MyAgentState:
    return await service.my_state(session, principal)


@router.put("/agent/state", response_model=MyAgentState)
async def set_my_status(
    payload: MyAgentStatusUpdate, ctx: Context, session: TenantDb, principal: CanServe
) -> MyAgentState:
    """切换接待状态：online 接新会话；busy、away 不接新会话；offline 时未回复的会话退回队列。"""
    return await service.set_my_status(ctx, session, principal, payload.status)


@router.post("/agent/heartbeat", response_model=MyAgentState)
async def heartbeat(session: TenantDb, principal: CanServe) -> MyAgentState:
    """工作台每 30 秒调用一次；超过 90 秒没有心跳视为断线，自动离线。"""
    return await service.heartbeat(session, principal)


@router.post("/agent/im-token", response_model=AgentIMCredentials)
async def issue_im_token(
    ctx: Context, session: TenantDb, principal: CanServe
) -> AgentIMCredentials:
    try:
        return await service.issue_im_credentials(ctx, session, principal)
    except OpenIMError as exc:
        raise ServiceUnavailable("即时通讯服务暂时不可用，请稍后再试") from exc
