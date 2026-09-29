"""企业微信接入的管理接口、登录、客户信息和侧边栏接口。"""

from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import Select, func, select

from app.context import AppContext
from app.core.deps import client_ip, get_context
from app.core.errors import ERROR_RESPONSES, AppError, NotFound, Unprocessable
from app.core.permissions import Permission
from app.integrations.wecom import WeComError
from app.modules.ai.schemas import SuggestionList
from app.modules.audit.service import record_audit
from app.modules.customer.schemas import TransferResult
from app.modules.customer.service import get_customer
from app.modules.iam import service as iam_service
from app.modules.iam.deps import CurrentPrincipal, TenantDb, require_permission
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.iam.router import set_refresh_cookie
from app.modules.iam.schemas import TokenResponse
from app.modules.wecom import auth, inherit, login, marketing, sidebar
from app.modules.wecom.auth import cancel_corp
from app.modules.wecom.models import (
    MemberStatus,
    TransferStatus,
    WecomContactFollow,
    WecomGroupChat,
    WecomKfAccount,
    WecomMember,
    WecomTag,
    WecomTransfer,
)
from app.modules.wecom.schemas import (
    AssignUnassignedRequest,
    BroadcastCreate,
    BroadcastDetail,
    BroadcastList,
    BroadcastOptions,
    BroadcastOut,
    CorpOut,
    CustomerWecom,
    GroupChatOut,
    GroupTransferList,
    InstallOut,
    JoinWayCreate,
    JoinWayList,
    JoinWayOut,
    JssdkConfig,
    KfAccountOut,
    MemberBind,
    MemberList,
    MemberOut,
    SidebarContext,
    SidebarCustomer,
    SidebarGroupCreated,
    SidebarMemberList,
    SidebarSentRequest,
    SidebarSuggestRequest,
    SidebarTagsUpdate,
    SsoUrlOut,
    SyncAccepted,
    SyncRequest,
    TagOptions,
    UnassignedList,
    WecomCounts,
    WecomLoginRequest,
    WecomSettings,
    WecomStatus,
)
from app.modules.wecom.service import (
    active_corp,
    callback_urls,
    request_sync,
    require_corp,
)

router = APIRouter(prefix="/api/v1", tags=["wecom"], responses=ERROR_RESPONSES)

CanManage = Annotated[Principal, Depends(require_permission(Permission.SETTINGS_MANAGE))]
CanRead = Annotated[Principal, Depends(require_permission(Permission.CUSTOMER_READ))]
Context = Annotated[AppContext, Depends(get_context)]
ADMIN = "/admin/integrations/wecom"


# ---- 授权与设置 ----


@router.get(ADMIN, response_model=WecomStatus)
async def wecom_status(ctx: Context, session: TenantDb, _: CanManage) -> WecomStatus:
    """企业微信授权状态、客服账号、同步情况和需要在服务商后台配置的回调地址。"""
    corp = await active_corp(session)
    accounts = (
        await session.scalars(select(WecomKfAccount).order_by(WecomKfAccount.created_at))
    ).all()
    counts = None
    if corp is not None:

        async def count(query: Select[int]) -> int:
            return int(await session.scalar(query) or 0)

        counts = WecomCounts(
            members=await count(
                select(func.count())
                .select_from(WecomMember)
                .where(WecomMember.status == MemberStatus.ACTIVE)
            ),
            members_bound=await count(
                select(func.count()).select_from(Staff).where(Staff.wecom_userid.is_not(None))
            ),
            contacts=await count(
                select(func.count(func.distinct(WecomContactFollow.customer_id))).where(
                    WecomContactFollow.deleted_at.is_(None)
                )
            ),
            group_chats=await count(
                select(func.count())
                .select_from(WecomGroupChat)
                .where(WecomGroupChat.status == "normal")
            ),
            tags=await count(
                select(func.count()).select_from(WecomTag).where(WecomTag.deleted.is_(False))
            ),
            transfers_waiting=await count(
                select(func.count())
                .select_from(WecomTransfer)
                .where(WecomTransfer.status == TransferStatus.WAITING)
            ),
        )
    return WecomStatus(
        enabled=ctx.settings.wecom_enabled,
        suite_id=ctx.settings.wecom_suite_id or None,
        callback_urls=callback_urls(ctx.settings),
        corp=CorpOut.model_validate(corp, from_attributes=True) if corp else None,
        settings=WecomSettings.of(corp.settings) if corp else None,
        sync_state=(corp.sync_state or {}) if corp else {},
        kf_accounts=[
            KfAccountOut(
                open_kfid=a.open_kfid,
                name=a.name,
                avatar=a.avatar,
                status=a.status,
                channel_id=a.channel_account_id,
                contact_url=a.contact_url,
                synced_at=a.synced_at,
            )
            for a in accounts
        ],
        counts=counts,
    )


@router.post(f"{ADMIN}/install", response_model=InstallOut)
async def start_install(ctx: Context, session: TenantDb, principal: CanManage) -> InstallOut:
    """生成企业微信授权链接：管理员扫码授权代开发应用后跳回控制台。"""
    corp = await active_corp(session)
    if corp is not None:
        raise Unprocessable(f"已经绑定了企业微信「{corp.corp_name}」")
    return InstallOut(url=await auth.start_install(ctx, principal))


@router.get("/wecom/install/callback", include_in_schema=False)
async def install_callback(
    ctx: Context,
    auth_code: Annotated[str, Query()] = "",
    state: Annotated[str, Query()] = "",
) -> RedirectResponse:
    """授权完成后企业微信把浏览器跳回这里，完成绑定后回到控制台的企业微信页面。"""
    target = f"{ctx.settings.console_public_url.rstrip('/')}/integrations/wecom"
    try:
        if not auth_code:
            raise Unprocessable("企业微信没有返回授权码")
        await auth.complete_install(ctx, auth_code=auth_code, state=state or None)
    except AppError as exc:
        return RedirectResponse(f"{target}?{urlencode({'error': exc.message})}", status_code=302)
    except WeComError as exc:
        message = f"企业微信暂时不可用，请稍后重试（{exc.errmsg or exc.errcode}）"
        return RedirectResponse(f"{target}?{urlencode({'error': message})}", status_code=302)
    return RedirectResponse(f"{target}?installed=1", status_code=302)


@router.put(f"{ADMIN}/settings", response_model=WecomSettings)
async def update_settings(
    payload: WecomSettings, request: Request, session: TenantDb, principal: CanManage
) -> WecomSettings:
    corp = await require_corp(session)
    if payload.welcome_kf_id is not None:
        exists = await session.scalar(
            select(WecomKfAccount.id).where(WecomKfAccount.open_kfid == payload.welcome_kf_id)
        )
        if exists is None:
            raise Unprocessable("欢迎语附带的客服账号不存在")
    corp.settings = payload.model_dump()
    record_audit(
        session,
        action="wecom.settings",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="wecom_corp",
        resource_id=corp.corp_id,
        detail=payload.model_dump(),
        ip=client_ip(request),
    )
    await session.commit()
    return payload


@router.post(f"{ADMIN}/sync", response_model=SyncAccepted, status_code=status.HTTP_202_ACCEPTED)
async def sync(
    payload: SyncRequest, ctx: Context, session: TenantDb, principal: CanManage
) -> SyncAccepted:
    """立即全量同步（在后台执行）：成员、客服账号、标签、客户、客户群。"""
    corp = await require_corp(session)
    targets = list(dict.fromkeys(payload.targets))
    await request_sync(ctx, principal.tenant_id, corp.corp_id, targets)
    return SyncAccepted(targets=targets)


@router.delete(ADMIN, status_code=status.HTTP_204_NO_CONTENT)
async def unbind(ctx: Context, session: TenantDb, principal: CanManage) -> Response:
    """解除绑定：停用微信客服渠道，已同步的客户和消息保留。企业微信里的授权需要企业管理员另行取消。"""
    corp = await require_corp(session)
    await session.commit()
    await cancel_corp(ctx, corp.corp_id, actor_id=principal.staff_id, reason="unbind")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- 成员绑定 ----


@router.get(f"{ADMIN}/members", response_model=MemberList)
async def list_members(session: TenantDb, _: CanManage) -> MemberList:
    rows = (
        await session.execute(
            select(WecomMember, Staff.id, Staff.display_name)
            .outerjoin(Staff, Staff.wecom_userid == WecomMember.userid)
            .order_by(WecomMember.status, WecomMember.name, WecomMember.userid)
        )
    ).all()
    return MemberList(
        items=[
            MemberOut(
                userid=m.userid,
                name=m.name,
                follow=m.follow,
                status=m.status,
                staff_id=staff_id,
                staff_name=staff_name,
            )
            for m, staff_id, staff_name in rows
        ]
    )


@router.put(f"{ADMIN}/members/{{userid}}", response_model=MemberOut)
async def bind_member(
    userid: str, payload: MemberBind, request: Request, session: TenantDb, principal: CanManage
) -> MemberOut:
    """把企业成员绑定到平台员工（staff_id 为 null 时解除绑定）。"""
    member = await session.scalar(select(WecomMember).where(WecomMember.userid == userid))
    if member is None:
        raise NotFound("企业成员不存在，请先同步成员")
    current = await session.scalar(select(Staff).where(Staff.wecom_userid == userid))
    if current is not None:
        current.wecom_userid = None
        await session.flush()
    staff = None
    if payload.staff_id is not None:
        staff = await session.get(Staff, payload.staff_id)
        if staff is None:
            raise NotFound("员工不存在")
        await login.bind_member(session, staff, userid)
    record_audit(
        session,
        action="wecom.bind_member",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="staff",
        resource_id=str(payload.staff_id) if payload.staff_id else None,
        detail={"userid": userid},
        ip=client_ip(request),
    )
    await session.commit()
    return MemberOut(
        userid=member.userid,
        name=member.name,
        follow=member.follow,
        status=member.status,
        staff_id=staff.id if staff else None,
        staff_name=staff.display_name if staff else None,
    )


# ---- 登录 ----


@router.get("/auth/wecom/sso", response_model=SsoUrlOut)
async def sso_url(
    ctx: Context,
    tenant_code: Annotated[str, Query(min_length=3, max_length=32)],
    state: Annotated[str, Query(pattern=r"^[A-Za-z0-9]{8,64}$")],
) -> SsoUrlOut:
    """企业微信扫码登录页地址（state 由控制台生成，回来时核对）。"""
    return SsoUrlOut(url=await login.sso_url(ctx, tenant_code, state))


@router.get("/auth/wecom/oauth", response_model=SsoUrlOut)
async def oauth_entry(
    ctx: Context,
    corp_id: Annotated[str, Query(min_length=1, max_length=64)],
    next: Annotated[str, Query(max_length=512)] = "/",
) -> SsoUrlOut:
    """企业微信内打开控制台页面（如侧边栏）时的网页授权地址（免登后回到 next）。"""
    return SsoUrlOut(url=await login.oauth_entry(ctx, corp_id, next))


@router.post("/auth/wecom", response_model=TokenResponse)
async def wecom_login(
    payload: WecomLoginRequest, request: Request, response: Response, ctx: Context
) -> TokenResponse:
    """扫码登录或企业微信内免登：用 code 换取成员身份，登录绑定了这个成员的员工。"""
    tenant_id, userid = await login.member_of_code(ctx, payload.corp_id, payload.code)
    async with ctx.db.tenant_session(tenant_id) as session:
        staff = await login.staff_of_member(session, userid)
        tokens, _ = iam_service.issue_tokens(session, ctx.settings, staff)
        record_audit(
            session,
            action="auth.login",
            actor_type="staff",
            actor_id=staff.id,
            tenant_id=tenant_id,
            detail={"method": "wecom", "userid": userid},
            ip=client_ip(request),
        )
        await session.commit()
    set_refresh_cookie(response, ctx.settings, tokens.refresh_token)
    return TokenResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)


@router.post("/me/wecom", status_code=status.HTTP_204_NO_CONTENT)
async def bind_self(
    payload: WecomLoginRequest, ctx: Context, session: TenantDb, principal: CurrentPrincipal
) -> Response:
    """已登录的员工扫码绑定自己的企业微信账号（之后可以扫码登录、接收应用消息）。"""
    tenant_id, userid = await login.member_of_code(ctx, payload.corp_id, payload.code)
    if tenant_id != principal.tenant_id:
        raise Unprocessable("这个企业微信不属于当前企业")
    staff = await session.get(Staff, principal.staff_id)
    assert staff is not None
    await login.bind_member(session, staff, userid)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- 客户 ----


@router.get("/customers/{customer_id}/wecom", response_model=CustomerWecom)
async def customer_wecom(customer_id: UUID, session: TenantDb, principal: CanRead) -> CustomerWecom:
    """客户在企业微信里的添加人、标签、所在客户群和在职继承记录（客户 360 视图）。"""
    await get_customer(session, principal, customer_id)
    return await sidebar.customer_wecom(session, customer_id)


# ---- 侧边栏 ----


@router.get("/wecom/jssdk-config", response_model=JssdkConfig)
async def jssdk_config(
    ctx: Context,
    session: TenantDb,
    _: CurrentPrincipal,
    url: Annotated[str, Query(min_length=8, max_length=2048)],
) -> JssdkConfig:
    """JS-SDK 注入配置：url 是当前页面地址（不含 # 之后的部分）。"""
    return await sidebar.jssdk_config(ctx, session, url)


@router.get("/sidebar/context", response_model=SidebarContext)
async def sidebar_context(
    session: TenantDb,
    principal: CurrentPrincipal,
    external_userid: Annotated[str | None, Query(max_length=64)] = None,
    chat_id: Annotated[str | None, Query(max_length=64)] = None,
) -> SidebarContext:
    """当前聊天的客户档案（单聊）或客户群信息（群聊），按数据范围校验。"""
    return await sidebar.context(
        session, principal, external_userid=external_userid, chat_id=chat_id
    )


@router.post("/sidebar/suggestions", response_model=SuggestionList)
async def sidebar_suggestions(
    payload: SidebarSuggestRequest, ctx: Context, session: TenantDb, principal: CurrentPrincipal
) -> SuggestionList:
    return await sidebar.suggestions(ctx, session, principal, payload)


@router.post("/sidebar/sent", status_code=status.HTTP_204_NO_CONTENT)
async def sidebar_sent(
    payload: SidebarSentRequest, session: TenantDb, principal: CurrentPrincipal
) -> Response:
    """记录员工经侧边栏发出的内容（渠道记为 wecom_sidebar）。"""
    await sidebar.record_sent(session, principal, payload)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/sidebar/customers/{customer_id}/tags", response_model=SidebarCustomer)
async def sidebar_tags(
    customer_id: UUID,
    payload: SidebarTagsUpdate,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CurrentPrincipal,
) -> SidebarCustomer:
    """在侧边栏里修改客户标签（员工能看到的客户，或自己在企业微信里添加的客户）。"""
    return await sidebar.update_tags(
        ctx, session, principal, customer_id, payload, ip=client_ip(request)
    )


@router.get("/sidebar/tags", response_model=TagOptions)
async def sidebar_tags_options(session: TenantDb, _: CurrentPrincipal) -> TagOptions:
    """企业标签（侧边栏改标签时的候选）。"""
    rows = await session.scalars(
        select(WecomTag.name)
        .where(WecomTag.deleted.is_(False))
        .order_by(WecomTag.group_id, WecomTag.sort, WecomTag.name)
    )
    return TagOptions(items=list(dict.fromkeys(rows.all())))


@router.get("/sidebar/members", response_model=SidebarMemberList)
async def sidebar_members(session: TenantDb, _: CurrentPrincipal) -> SidebarMemberList:
    """一键建群时可以拉进群的企业成员（接单员等）。"""
    return await sidebar.members(session)


@router.post("/sidebar/groups", response_model=GroupChatOut)
async def sidebar_group_created(
    payload: SidebarGroupCreated,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CurrentPrincipal,
) -> GroupChatOut:
    """侧边栏一键建群（JS-SDK openEnterpriseChat）后同步这个客户群，关联到客户档案。"""
    return await sidebar.group_created(ctx, session, principal, payload, ip=client_ip(request))


# ---- 离职继承、客户群继承 ----


@router.get(f"{ADMIN}/unassigned", response_model=UnassignedList)
async def unassigned_customers(ctx: Context, session: TenantDb, _: CanManage) -> UnassignedList:
    """企业微信里待分配的离职成员客户。"""
    return UnassignedList(items=await inherit.unassigned(ctx, session))


@router.post(f"{ADMIN}/unassigned/assign", response_model=TransferResult)
async def assign_unassigned(
    payload: AssignUnassignedRequest,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanManage,
) -> TransferResult:
    """把离职成员的客户分配给接手的员工：平台归属变更，企业微信里离职继承（可同时转移客户群）。"""
    result = await inherit.assign_unassigned(ctx, session, principal, payload)
    record_audit(
        session,
        action="wecom.assign_unassigned",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="staff",
        resource_id=str(payload.to_owner_id),
        detail={
            "handover_userid": payload.handover_userid,
            "customers": len(payload.external_userids or []) or None,
            "transfer_groups": payload.transfer_groups,
        },
        ip=client_ip(request),
    )
    await session.commit()
    return result


@router.get(f"{ADMIN}/group-transfers", response_model=GroupTransferList)
async def group_transfers(session: TenantDb, _: CanManage) -> GroupTransferList:
    """最近的客户群继承记录。"""
    return GroupTransferList(items=await inherit.recent_group_transfers(session))


# ---- 客户群活码 ----


@router.get(f"{ADMIN}/join-ways", response_model=JoinWayList)
async def join_ways(session: TenantDb, _: CanManage) -> JoinWayList:
    return JoinWayList(items=await marketing.list_join_ways(session))


@router.post(f"{ADMIN}/join-ways", response_model=JoinWayOut, status_code=status.HTTP_201_CREATED)
async def create_join_way(
    payload: JoinWayCreate, ctx: Context, session: TenantDb, principal: CanManage
) -> JoinWayOut:
    """创建"加入群聊"二维码：扫码进入指定客户群，群满后可以自动建新群。"""
    return await marketing.create_join_way(ctx, session, principal, payload)


@router.delete(f"{ADMIN}/join-ways/{{way_id}}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_join_way(way_id: UUID, ctx: Context, session: TenantDb, _: CanManage) -> Response:
    await marketing.delete_join_way(ctx, session, way_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- 群发任务 ----

CanBroadcast = Annotated[Principal, Depends(require_permission(Permission.BROADCAST_MANAGE))]


@router.get("/wecom/broadcast-options", response_model=BroadcastOptions)
async def broadcast_options(session: TenantDb, principal: CanBroadcast) -> BroadcastOptions:
    """群发表单的候选：客户标签、归属坐席、客户群（限于可见范围）。"""
    return await marketing.broadcast_options(session, principal)


@router.get("/wecom/broadcasts", response_model=BroadcastList)
async def list_broadcasts(session: TenantDb, principal: CanBroadcast) -> BroadcastList:
    return BroadcastList(items=await marketing.list_broadcasts(session, principal))


@router.post("/wecom/broadcasts", response_model=BroadcastOut, status_code=status.HTTP_201_CREATED)
async def create_broadcast(
    payload: BroadcastCreate,
    request: Request,
    ctx: Context,
    session: TenantDb,
    principal: CanBroadcast,
) -> BroadcastOut:
    """创建群发任务：员工（发给客户）或群主（发到客户群）在企业微信里确认后发出。"""
    out = await marketing.create_broadcast(ctx, session, principal, payload)
    record_audit(
        session,
        action="wecom.broadcast",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="wecom_broadcast",
        resource_id=str(out.id),
        detail={"kind": out.kind, "targets": out.target_count, "status": out.status},
        ip=client_ip(request),
    )
    await session.commit()
    return out


@router.get("/wecom/broadcasts/{broadcast_id}", response_model=BroadcastDetail)
async def broadcast_detail(
    broadcast_id: UUID, session: TenantDb, principal: CanBroadcast
) -> BroadcastDetail:
    broadcast = await marketing.get_broadcast(session, principal, broadcast_id)
    return await marketing.broadcast_detail(session, broadcast)


@router.post("/wecom/broadcasts/{broadcast_id}/refresh", response_model=BroadcastDetail)
async def refresh_broadcast(
    broadcast_id: UUID, ctx: Context, session: TenantDb, principal: CanBroadcast
) -> BroadcastDetail:
    """立即回收发送结果（调度进程也会定时回收）。"""
    broadcast = await marketing.get_broadcast(session, principal, broadcast_id)
    await session.commit()
    await marketing.refresh_broadcast(ctx, principal.tenant_id, broadcast.id)
    await session.refresh(broadcast)
    return await marketing.broadcast_detail(session, broadcast)


@router.post("/wecom/broadcasts/{broadcast_id}/cancel", response_model=BroadcastDetail)
async def cancel_broadcast(
    broadcast_id: UUID, ctx: Context, session: TenantDb, principal: CanBroadcast
) -> BroadcastDetail:
    """停止群发：还没确认发送的员工不能再发送。"""
    broadcast = await marketing.get_broadcast(session, principal, broadcast_id)
    await marketing.cancel_broadcast(ctx, session, broadcast)
    return await marketing.broadcast_detail(session, broadcast)


@router.post("/wecom/broadcasts/{broadcast_id}/remind", status_code=status.HTTP_204_NO_CONTENT)
async def remind_broadcast(
    broadcast_id: UUID, ctx: Context, session: TenantDb, principal: CanBroadcast
) -> Response:
    """提醒还没确认的员工发送。"""
    broadcast = await marketing.get_broadcast(session, principal, broadcast_id)
    await marketing.remind_broadcast(ctx, session, broadcast)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
