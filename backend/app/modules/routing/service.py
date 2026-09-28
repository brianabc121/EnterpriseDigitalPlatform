"""技能组、路由策略、坐席状态与并发。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.permissions import Permission
from app.integrations.openim import WEB_PLATFORM_ID
from app.modules.audit.service import record_audit
from app.modules.channels.models import ChannelAccount
from app.modules.conversation import imids, outbox
from app.modules.conversation.models import ChatSession
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.routing.assign import LOAD_STATUSES
from app.modules.routing.models import (
    AgentState,
    AgentStatus,
    RoutingMode,
    RoutingPolicy,
    SkillGroup,
    SkillGroupMember,
)
from app.modules.routing.schemas import (
    AgentGroupOut,
    AgentIMCredentials,
    AgentList,
    AgentOut,
    MyAgentState,
    RoutingPolicyCreate,
    RoutingPolicyOut,
    RoutingPolicyUpdate,
    SkillGroupCreate,
    SkillGroupMemberIn,
    SkillGroupMemberOut,
    SkillGroupOut,
    SkillGroupUpdate,
)
from app.modules.routing.scope import team_members
from app.modules.sessions.engine import assign_queued, lock_tenant_routing, requeue_unanswered

SKILL_GROUP_NOT_FOUND = "技能组不存在"
POLICY_NOT_FOUND = "路由策略不存在"
STAFF_NOT_FOUND = "员工不存在"
DEFAULT_MAX_CONCURRENCY = 5


def _now() -> datetime:
    return datetime.now(UTC)


def _audit(
    session: AsyncSession,
    principal: Principal,
    action: str,
    resource_type: str,
    resource_id: UUID,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    record_audit(
        session,
        action=action,
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type=resource_type,
        resource_id=str(resource_id),
        detail=detail,
        ip=ip,
    )


# ---- 技能组 ----


async def list_skill_groups(session: AsyncSession) -> list[SkillGroupOut]:
    groups = (await session.scalars(select(SkillGroup).order_by(SkillGroup.name))).all()
    rows = await session.execute(
        select(
            SkillGroupMember.skill_group_id,
            SkillGroupMember.staff_id,
            Staff.display_name,
            SkillGroupMember.is_lead,
        )
        .join(
            Staff,
            and_(
                Staff.tenant_id == SkillGroupMember.tenant_id, Staff.id == SkillGroupMember.staff_id
            ),
        )
        .order_by(SkillGroupMember.is_lead.desc(), Staff.display_name)
    )
    members: dict[UUID, list[SkillGroupMemberOut]] = {}
    for group_id, staff_id, name, is_lead in rows:
        members.setdefault(group_id, []).append(
            SkillGroupMemberOut(staff_id=staff_id, display_name=name, is_lead=is_lead)
        )
    return [
        SkillGroupOut(id=g.id, name=g.name, members=members.get(g.id, []), created_at=g.created_at)
        for g in groups
    ]


async def _get_group(session: AsyncSession, group_id: UUID) -> SkillGroupOut:
    for group in await list_skill_groups(session):
        if group.id == group_id:
            return group
    raise NotFound(SKILL_GROUP_NOT_FOUND)


async def _replace_members(
    session: AsyncSession, group_id: UUID, members: list[SkillGroupMemberIn]
) -> None:
    staff_ids = {m.staff_id for m in members}
    if len(staff_ids) != len(members):
        raise Unprocessable("成员不能重复")
    if staff_ids:
        found = set(
            (
                await session.scalars(
                    select(Staff.id).where(
                        Staff.id.in_(staff_ids), Staff.status == StaffStatus.ACTIVE
                    )
                )
            ).all()
        )
        if found != staff_ids:
            raise Unprocessable("成员中有不存在或已停用的员工")
    await session.execute(
        delete(SkillGroupMember).where(SkillGroupMember.skill_group_id == group_id)
    )
    for m in members:
        session.add(
            SkillGroupMember(skill_group_id=group_id, staff_id=m.staff_id, is_lead=m.is_lead)
        )


async def create_skill_group(
    session: AsyncSession, principal: Principal, payload: SkillGroupCreate, *, ip: str | None
) -> SkillGroupOut:
    group = SkillGroup(name=payload.name)
    session.add(group)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict("技能组名称已存在") from exc
    await _replace_members(session, group.id, payload.members)
    _audit(
        session, principal, "skill_group.create", "skill_group", group.id, {"name": group.name}, ip
    )
    await session.commit()
    return await _get_group(session, group.id)


async def update_skill_group(
    session: AsyncSession,
    principal: Principal,
    group_id: UUID,
    payload: SkillGroupUpdate,
    *,
    ip: str | None,
) -> SkillGroupOut:
    group = await session.get(SkillGroup, group_id)
    if group is None:
        raise NotFound(SKILL_GROUP_NOT_FOUND)
    if payload.name is not None:
        group.name = payload.name
        try:
            await session.flush()
        except IntegrityError as exc:
            raise Conflict("技能组名称已存在") from exc
    if payload.members is not None:
        await _replace_members(session, group.id, payload.members)
    _audit(
        session,
        principal,
        "skill_group.update",
        "skill_group",
        group.id,
        payload.model_dump(mode="json", exclude_unset=True),
        ip,
    )
    await session.commit()
    return await _get_group(session, group.id)


async def delete_skill_group(
    session: AsyncSession, principal: Principal, group_id: UUID, *, ip: str | None
) -> None:
    group = await session.get(SkillGroup, group_id)
    if group is None:
        raise NotFound(SKILL_GROUP_NOT_FOUND)
    await session.delete(group)
    _audit(
        session, principal, "skill_group.delete", "skill_group", group_id, {"name": group.name}, ip
    )
    await session.commit()


# ---- 路由策略 ----


async def list_policies(session: AsyncSession) -> list[RoutingPolicyOut]:
    policies = (
        await session.scalars(
            select(RoutingPolicy).order_by(
                RoutingPolicy.is_default.desc(), RoutingPolicy.created_at
            )
        )
    ).all()
    bindings = await session.execute(
        select(ChannelAccount.routing_policy_id, ChannelAccount.id).where(
            ChannelAccount.routing_policy_id.is_not(None)
        )
    )
    channels: dict[UUID, list[UUID]] = {}
    for policy_id, channel_id in bindings:
        if policy_id is not None:
            channels.setdefault(policy_id, []).append(channel_id)
    return [_policy_out(p, channels.get(p.id, [])) for p in policies]


def _policy_out(policy: RoutingPolicy, channel_ids: list[UUID]) -> RoutingPolicyOut:
    return RoutingPolicyOut(
        id=policy.id,
        name=policy.name,
        is_default=policy.is_default,
        mode=RoutingMode(policy.mode),
        default_skill_group_id=policy.default_skill_group_id,
        owner_first=policy.owner_first,
        max_wait_seconds=policy.max_wait_seconds,
        idle_close_minutes=policy.idle_close_minutes,
        business_hours=policy.business_hours,
        channel_ids=channel_ids,
        created_at=policy.created_at,
    )


async def _get_policy(session: AsyncSession, policy_id: UUID) -> RoutingPolicyOut:
    for policy in await list_policies(session):
        if policy.id == policy_id:
            return policy
    raise NotFound(POLICY_NOT_FOUND)


async def _check_group(session: AsyncSession, group_id: UUID | None) -> None:
    if group_id is not None and await session.get(SkillGroup, group_id) is None:
        raise Unprocessable(SKILL_GROUP_NOT_FOUND)


async def create_policy(
    session: AsyncSession, principal: Principal, payload: RoutingPolicyCreate, *, ip: str | None
) -> RoutingPolicyOut:
    await _check_group(session, payload.default_skill_group_id)
    policy = RoutingPolicy(**payload.model_dump(), is_default=False)
    session.add(policy)
    await session.flush()
    _audit(
        session,
        principal,
        "routing_policy.create",
        "routing_policy",
        policy.id,
        {"name": policy.name},
        ip,
    )
    await session.commit()
    return await _get_policy(session, policy.id)


async def update_policy(
    session: AsyncSession,
    principal: Principal,
    policy_id: UUID,
    payload: RoutingPolicyUpdate,
    *,
    ip: str | None,
) -> RoutingPolicyOut:
    policy = await session.get(RoutingPolicy, policy_id)
    if policy is None:
        raise NotFound(POLICY_NOT_FOUND)
    changes = payload.model_dump(exclude_unset=True)
    if "default_skill_group_id" in changes:
        await _check_group(session, changes["default_skill_group_id"])
    if changes.pop("is_default", None) and not policy.is_default:
        await session.execute(
            update(RoutingPolicy)
            .where(RoutingPolicy.is_default, RoutingPolicy.id != policy.id)
            .values(is_default=False)
        )
        policy.is_default = True
    for field, value in changes.items():
        if value is None and field not in ("default_skill_group_id", "business_hours"):
            continue
        setattr(policy, field, value)
    _audit(
        session,
        principal,
        "routing_policy.update",
        "routing_policy",
        policy.id,
        payload.model_dump(mode="json", exclude_unset=True),
        ip,
    )
    await session.commit()
    return await _get_policy(session, policy.id)


async def delete_policy(
    session: AsyncSession, principal: Principal, policy_id: UUID, *, ip: str | None
) -> None:
    policy = await session.get(RoutingPolicy, policy_id)
    if policy is None:
        raise NotFound(POLICY_NOT_FOUND)
    if policy.is_default:
        raise Conflict("默认策略不能删除，请先把其他策略设为默认")
    # 绑定了这套策略的渠道改用默认策略（外键 ON DELETE SET NULL）。
    await session.delete(policy)
    _audit(
        session,
        principal,
        "routing_policy.delete",
        "routing_policy",
        policy_id,
        {"name": policy.name},
        ip,
    )
    await session.commit()


# ---- 坐席 ----


async def _active_sessions(session: AsyncSession, staff_ids: list[UUID]) -> dict[UUID, int]:
    if not staff_ids:
        return {}
    rows = await session.execute(
        select(ChatSession.assignee_id, func.count())
        .where(ChatSession.assignee_id.in_(staff_ids), ChatSession.status.in_(LOAD_STATUSES))
        .group_by(ChatSession.assignee_id)
    )
    return {staff_id: count for staff_id, count in rows if staff_id is not None}


async def list_agents(session: AsyncSession, principal: Principal) -> AgentList:
    """坐席看板。只有 session:read_team 的主管只看到所带技能组的成员。"""
    query = (
        select(Staff, AgentState)
        .outerjoin(
            AgentState,
            and_(AgentState.tenant_id == Staff.tenant_id, AgentState.staff_id == Staff.id),
        )
        .where(Staff.status == StaffStatus.ACTIVE)
        .order_by(Staff.display_name, Staff.id)
    )
    if not (principal.has(Permission.ROUTING_MANAGE) or principal.has(Permission.SESSION_READ_ALL)):
        query = query.where(Staff.id.in_(team_members(principal.staff_id)))
    rows = (await session.execute(query)).all()
    staff_ids = [staff.id for staff, _ in rows]
    loads = await _active_sessions(session, staff_ids)
    groups: dict[UUID, list[AgentGroupOut]] = {}
    if staff_ids:
        memberships = await session.execute(
            select(
                SkillGroupMember.staff_id, SkillGroup.id, SkillGroup.name, SkillGroupMember.is_lead
            )
            .join(
                SkillGroup,
                and_(
                    SkillGroup.tenant_id == SkillGroupMember.tenant_id,
                    SkillGroup.id == SkillGroupMember.skill_group_id,
                ),
            )
            .where(SkillGroupMember.staff_id.in_(staff_ids))
            .order_by(SkillGroup.name)
        )
        for staff_id, group_id, name, is_lead in memberships:
            groups.setdefault(staff_id, []).append(
                AgentGroupOut(id=group_id, name=name, is_lead=is_lead)
            )
    return AgentList(
        items=[
            AgentOut(
                staff_id=staff.id,
                username=staff.username,
                display_name=staff.display_name,
                status=AgentStatus(state.status) if state else AgentStatus.OFFLINE,
                max_concurrency=state.max_concurrency if state else DEFAULT_MAX_CONCURRENCY,
                active_sessions=loads.get(staff.id, 0),
                last_seen_at=state.last_seen_at if state else None,
                skill_groups=groups.get(staff.id, []),
            )
            for staff, state in rows
        ]
    )


async def _state_for_update(session: AsyncSession, staff_id: UUID) -> AgentState:
    """坐席状态行（不存在时创建），并锁定。"""
    state = await session.scalar(
        select(AgentState).where(AgentState.staff_id == staff_id).with_for_update()
    )
    if state is None:
        state = AgentState(
            staff_id=staff_id,
            status=AgentStatus.OFFLINE,
            max_concurrency=DEFAULT_MAX_CONCURRENCY,
            status_changed_at=_now(),
        )
        session.add(state)
        await session.flush()
    return state


async def update_agent(
    session: AsyncSession,
    principal: Principal,
    staff_id: UUID,
    max_concurrency: int,
    *,
    ip: str | None,
) -> None:
    staff = await session.get(Staff, staff_id)
    if staff is None:
        raise NotFound(STAFF_NOT_FOUND)
    state = await _state_for_update(session, staff_id)
    state.max_concurrency = max_concurrency
    _audit(
        session,
        principal,
        "agent.update",
        "staff",
        staff_id,
        {"max_concurrency": max_concurrency},
        ip,
    )
    await session.commit()


async def my_state(session: AsyncSession, principal: Principal) -> MyAgentState:
    state = await session.scalar(
        select(AgentState).where(AgentState.staff_id == principal.staff_id)
    )
    loads = await _active_sessions(session, [principal.staff_id])
    return MyAgentState(
        status=AgentStatus(state.status) if state else AgentStatus.OFFLINE,
        max_concurrency=state.max_concurrency if state else DEFAULT_MAX_CONCURRENCY,
        active_sessions=loads.get(principal.staff_id, 0),
        last_seen_at=state.last_seen_at if state else None,
    )


async def set_my_status(
    ctx: AppContext, session: AsyncSession, principal: Principal, status: AgentStatus
) -> MyAgentState:
    """切换接待状态（同时算一次心跳）。上线后立即分配排队中的会话；
    离线时，分配给自己但还没回复过的会话退回队列。"""
    now = _now()
    await lock_tenant_routing(session, principal.tenant_id)
    state = await _state_for_update(session, principal.staff_id)
    rooms: set[UUID] = set()
    if state.status != status:
        state.status = status
        state.status_changed_at = now
        if status == AgentStatus.OFFLINE:
            rooms = await requeue_unanswered(
                session, principal.staff_id, now, reason="agent_offline"
            )
    state.last_seen_at = now
    await session.commit()
    await outbox.flush_rooms(ctx, principal.tenant_id, rooms)
    if status == AgentStatus.ONLINE or rooms:
        await assign_queued(ctx, principal.tenant_id)
    return await my_state(session, principal)


async def heartbeat(session: AsyncSession, principal: Principal) -> MyAgentState:
    state = await _state_for_update(session, principal.staff_id)
    state.last_seen_at = _now()
    await session.commit()
    return await my_state(session, principal)


async def issue_im_credentials(
    ctx: AppContext, session: AsyncSession, principal: Principal
) -> AgentIMCredentials:
    """开通员工的 IM 用户（与系统用户互为好友，能收到信令）并签发登录令牌。"""
    user_id = await ctx.provisioner.ensure_staff(
        principal.tenant_code, principal.staff_id, nickname=principal.display_name
    )
    token = await ctx.im.get_user_token(user_id, WEB_PLATFORM_ID)
    state = await _state_for_update(session, principal.staff_id)
    state.im_ready_at = state.im_ready_at or _now()
    await session.commit()
    return AgentIMCredentials(
        user_id=user_id,
        token=token.token,
        expires_in=token.expires_in,
        api_url=ctx.settings.openim_public_api_url,
        ws_url=ctx.settings.openim_public_ws_url,
        platform_id=WEB_PLATFORM_ID,
        system_user_id=imids.system_user(principal.tenant_code),
    )
