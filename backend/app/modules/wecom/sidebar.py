"""聊天工具栏侧边栏（设计文档 §10.5）与 JS-SDK 签名。

- 侧边栏运行在员工企业微信客户端的单聊、客户群聊天窗口右侧，先用 wx.config / wx.agentConfig
  注入权限（jssdk_config），再用 getCurExternalContact / getCurExternalChat 取当前客户或群。
- 数据范围双重校验（设计 §13.3）：员工能看到的客户（DataScope），或企业微信里员工自己添加的客户。
- JS-SDK 读不到聊天内容：员工把客户的问题粘贴到侧边栏，AI 结合客户在微信客服和网页的历史
  给出建议回复；员工点击后用 sendChatMessage 发到当前聊天，发出的内容记入平台（wecom_sidebar）。
"""

import hashlib
import secrets
import time
from uuid import UUID

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import NotFound
from app.modules.ai.assist import draft
from app.modules.ai.prompts import Turn
from app.modules.ai.schemas import SuggestionList
from app.modules.audit.service import record_audit
from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.conversation.models import ChatSession, Message, SenderType
from app.modules.customer.models import Customer, CustomerIdentity
from app.modules.customer.service import visible_to
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.wecom.contacts import refresh_group, write_back_tags
from app.modules.wecom.models import (
    MemberStatus,
    WecomContactFollow,
    WecomGroupChat,
    WecomGroupMember,
    WecomMember,
    WecomSidebarMessage,
    WecomTag,
    WecomTransfer,
)
from app.modules.wecom.schemas import (
    CustomerWecom,
    FollowOut,
    GroupChatOut,
    JssdkConfig,
    JsSignature,
    SidebarContext,
    SidebarCustomer,
    SidebarCustomerBrief,
    SidebarGroupCreated,
    SidebarIdentity,
    SidebarMemberList,
    SidebarMemberOut,
    SidebarSentRequest,
    SidebarSession,
    SidebarSuggestRequest,
    SidebarTagsUpdate,
    WecomTransferOut,
)
from app.modules.wecom.service import client_of, require_corp
from app.modules.wecom.zone import latest_analysis

WECOM_CHANNELS = (ChannelType.WECOM_KF, ChannelType.WECOM_CONTACT)
CUSTOMER_NOT_VISIBLE = "客户不存在或不在您的可见范围内"
_HISTORY = 12
_ROLES: dict[str, str] = {
    SenderType.CUSTOMER: "customer",
    SenderType.AGENT: "agent",
    SenderType.BOT: "bot",
}


def js_signature(ticket: str, nonce: str, timestamp: int, url: str) -> str:
    raw = f"jsapi_ticket={ticket}&noncestr={nonce}&timestamp={timestamp}&url={url}"
    return hashlib.sha1(raw.encode()).hexdigest()


async def jssdk_config(ctx: AppContext, session: AsyncSession, url: str) -> JssdkConfig:
    corp = await require_corp(session)
    wecom = client_of(ctx)
    page = url.split("#", 1)[0]
    timestamp = int(time.time())
    nonce = secrets.token_hex(8)
    corp_ticket = await wecom.corp_ticket(corp.corp_id, "jsapi")
    agent_ticket = await wecom.corp_ticket(corp.corp_id, "agent")
    return JssdkConfig(
        corp_id=corp.corp_id,
        agent_id=corp.agent_id,
        config=JsSignature(
            timestamp=timestamp,
            nonce_str=nonce,
            signature=js_signature(corp_ticket, nonce, timestamp, page),
        ),
        agent_config=JsSignature(
            timestamp=timestamp,
            nonce_str=nonce,
            signature=js_signature(agent_ticket, nonce, timestamp, page),
        ),
    )


async def _my_userid(session: AsyncSession, principal: Principal) -> str | None:
    return await session.scalar(select(Staff.wecom_userid).where(Staff.id == principal.staff_id))


def _customers_of(external_userid: str) -> Select[UUID]:
    return (
        select(CustomerIdentity.customer_id)
        .join(ChannelAccount, ChannelAccount.id == CustomerIdentity.channel_account_id)
        .where(
            CustomerIdentity.external_id == external_userid,
            ChannelAccount.type.in_(WECOM_CHANNELS),
        )
    )


async def sidebar_customer(
    session: AsyncSession, principal: Principal, external_userid: str
) -> Customer:
    """员工能看到的客户，或企业微信里员工自己添加的客户。看不到与不存在同样返回 404。"""
    conditions = [visible_to(principal)]
    userid = await _my_userid(session, principal)
    if userid:
        conditions.append(
            Customer.id.in_(
                select(WecomContactFollow.customer_id).where(
                    WecomContactFollow.userid == userid,
                    WecomContactFollow.external_userid == external_userid,
                    WecomContactFollow.deleted_at.is_(None),
                )
            )
        )
    customer = await session.scalar(
        select(Customer)
        .where(Customer.id.in_(_customers_of(external_userid)), or_(*conditions))
        .limit(1)
    )
    if customer is None:
        raise NotFound(CUSTOMER_NOT_VISIBLE)
    return customer


async def customer_wecom(session: AsyncSession, customer_id: UUID) -> CustomerWecom:
    """客户在企业微信里的添加人、所在客户群和在职继承记录。"""
    external = await session.scalar(
        select(CustomerIdentity.external_id)
        .join(ChannelAccount, ChannelAccount.id == CustomerIdentity.channel_account_id)
        .where(
            CustomerIdentity.customer_id == customer_id,
            ChannelAccount.type.in_(WECOM_CHANNELS),
        )
        .limit(1)
    )
    unionid = await session.scalar(
        select(CustomerIdentity.profile["unionid"].astext)
        .where(
            CustomerIdentity.customer_id == customer_id,
            CustomerIdentity.profile.has_key("unionid"),
        )
        .limit(1)
    )
    tag_names = dict((await session.execute(select(WecomTag.tag_id, WecomTag.name))).all())
    follows = (
        await session.execute(
            select(WecomContactFollow, WecomMember.name, Staff.id, Staff.display_name)
            .outerjoin(WecomMember, WecomMember.userid == WecomContactFollow.userid)
            .outerjoin(Staff, Staff.wecom_userid == WecomContactFollow.userid)
            .where(WecomContactFollow.customer_id == customer_id)
            .order_by(WecomContactFollow.deleted_at.nulls_first(), WecomContactFollow.added_at)
        )
    ).all()
    groups = (
        await session.execute(
            select(WecomGroupChat, WecomMember.name)
            .join(
                WecomGroupMember,
                and_(
                    WecomGroupMember.chat_id == WecomGroupChat.chat_id,
                    WecomGroupMember.customer_id == customer_id,
                ),
            )
            .outerjoin(WecomMember, WecomMember.userid == WecomGroupChat.owner_userid)
            .order_by(WecomGroupChat.created_time.desc().nulls_last())
        )
    ).all()
    transfers = (
        await session.scalars(
            select(WecomTransfer)
            .where(WecomTransfer.customer_id == customer_id)
            .order_by(WecomTransfer.created_at.desc())
            .limit(10)
        )
    ).all()
    analysis = await latest_analysis(session, {g.chat_id for g, _ in groups})
    return CustomerWecom(
        external_userid=external,
        unionid=unionid,
        follows=[
            FollowOut(
                userid=f.userid,
                member_name=member_name or None,
                staff_id=staff_id,
                staff_name=staff_name,
                remark=f.remark,
                tags=[tag_names[t] for t in f.tag_ids if t in tag_names],
                added_at=f.added_at,
                deleted=f.deleted_at is not None,
            )
            for f, member_name, staff_id, staff_name in follows
        ],
        group_chats=[group_out(g, owner_name, analysis.get(g.chat_id)) for g, owner_name in groups],
        transfers=[WecomTransferOut.model_validate(t, from_attributes=True) for t in transfers],
    )


def group_out(
    group: WecomGroupChat,
    owner_name: str | None,
    analysis: dict[str, str | None] | None = None,
) -> GroupChatOut:
    return GroupChatOut(
        chat_id=group.chat_id,
        name=group.name,
        owner_userid=group.owner_userid,
        owner_name=owner_name or None,
        member_count=group.member_count,
        status=group.status,
        created_time=group.created_time,
        summary=(analysis or {}).get("summary"),
        sentiment=(analysis or {}).get("sentiment"),
    )


async def context(
    session: AsyncSession,
    principal: Principal,
    *,
    external_userid: str | None,
    chat_id: str | None,
) -> SidebarContext:
    if external_userid:
        customer = await sidebar_customer(session, principal, external_userid)
        return SidebarContext(
            kind="contact",
            customer=await _customer_out(session, customer),
            wecom=await customer_wecom(session, customer.id),
            sessions=await _recent_sessions(session, customer.id),
            group=None,
            group_customers=[],
        )
    if not chat_id:
        raise NotFound("请在客户单聊或客户群里打开侧边栏")
    row = (
        await session.execute(
            select(WecomGroupChat, WecomMember.name)
            .outerjoin(WecomMember, WecomMember.userid == WecomGroupChat.owner_userid)
            .where(WecomGroupChat.chat_id == chat_id)
        )
    ).first()
    if row is None:
        raise NotFound("客户群还没有同步，请稍后再试")
    group, owner_name = row
    customers = (
        await session.scalars(
            select(Customer)
            .where(
                Customer.id.in_(
                    select(WecomGroupMember.customer_id).where(
                        WecomGroupMember.chat_id == chat_id,
                        WecomGroupMember.customer_id.is_not(None),
                    )
                ),
                visible_to(principal),
            )
            .order_by(Customer.display_name)
        )
    ).all()
    analysis = await latest_analysis(session, {chat_id})
    return SidebarContext(
        kind="group",
        customer=None,
        wecom=None,
        sessions=[],
        group=group_out(group, owner_name, analysis.get(chat_id)),
        group_customers=[
            SidebarCustomerBrief(id=c.id, display_name=c.display_name, tags=list(c.tags or []))
            for c in customers
        ],
    )


async def _customer_out(session: AsyncSession, customer: Customer) -> SidebarCustomer:
    owner_name = (
        await session.scalar(select(Staff.display_name).where(Staff.id == customer.owner_id))
        if customer.owner_id
        else None
    )
    identities = (
        await session.execute(
            select(CustomerIdentity.profile, ChannelAccount.type, ChannelAccount.name)
            .join(ChannelAccount, ChannelAccount.id == CustomerIdentity.channel_account_id)
            .where(CustomerIdentity.customer_id == customer.id)
            .order_by(CustomerIdentity.created_at)
        )
    ).all()
    return SidebarCustomer(
        id=customer.id,
        display_name=customer.display_name,
        owner_id=customer.owner_id,
        owner_name=owner_name,
        tags=list(customer.tags or []),
        notes=customer.notes,
        identities=[
            SidebarIdentity(channel_type=kind, channel_name=name, profile=profile or {})
            for profile, kind, name in identities
        ],
    )


async def _recent_sessions(session: AsyncSession, customer_id: UUID) -> list[SidebarSession]:
    rows = (
        await session.execute(
            select(ChatSession, ChannelAccount.name)
            .join(ChannelAccount, ChannelAccount.id == ChatSession.channel_account_id)
            .where(ChatSession.customer_id == customer_id)
            .order_by(ChatSession.created_at.desc())
            .limit(5)
        )
    ).all()
    return [
        SidebarSession(
            id=chat.id,
            channel_name=name,
            status=chat.status,
            summary=chat.ai_summary,
            csat=chat.csat,
            created_at=chat.created_at,
            closed_at=chat.closed_at,
        )
        for chat, name in rows
    ]


async def suggestions(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: SidebarSuggestRequest
) -> SuggestionList:
    """针对员工粘贴的问题给建议回复；知道是哪位客户时，带上他在微信客服和网页的最近对话。"""
    history: list[Turn] = []
    if payload.external_userid:
        customer = await sidebar_customer(session, principal, payload.external_userid)
        rows = (
            await session.scalars(
                select(Message)
                .join(ChatSession, ChatSession.id == Message.session_id)
                .where(ChatSession.customer_id == customer.id, Message.text_plain.is_not(None))
                .order_by(Message.sent_at.desc(), Message.id.desc())
                .limit(_HISTORY)
            )
        ).all()
        history = [
            Turn(_ROLES[m.sender_type], m.text_plain or "")
            for m in reversed(rows)
            if m.sender_type in _ROLES
        ]
    items, knowledge = await draft(ctx, session, principal, history, payload.question)
    return SuggestionList(suggestions=items, knowledge=knowledge)


async def record_sent(
    session: AsyncSession, principal: Principal, payload: SidebarSentRequest
) -> None:
    """员工经侧边栏发到企业微信聊天的内容（也是 AI 建议的采纳反馈）。"""
    customer_id = None
    if payload.external_userid:
        customer_id = await session.scalar(
            select(Customer.id).where(Customer.id.in_(_customers_of(payload.external_userid)))
        )
    session.add(
        WecomSidebarMessage(
            tenant_id=principal.tenant_id,
            staff_id=principal.staff_id,
            customer_id=customer_id,
            external_userid=payload.external_userid,
            chat_id=payload.chat_id,
            content=payload.content,
            origin=payload.origin,
            question=(payload.question or "").strip() or None,
        )
    )
    await session.commit()


async def sidebar_customer_by_id(
    session: AsyncSession, principal: Principal, customer_id: UUID
) -> Customer:
    """侧边栏里按客户 ID 操作（改标签等）：员工能看到的客户，或企业微信里员工自己添加的客户。"""
    conditions = [visible_to(principal)]
    userid = await _my_userid(session, principal)
    if userid:
        conditions.append(
            Customer.id.in_(
                select(WecomContactFollow.customer_id).where(
                    WecomContactFollow.userid == userid,
                    WecomContactFollow.deleted_at.is_(None),
                )
            )
        )
    customer = await session.scalar(
        select(Customer).where(Customer.id == customer_id, or_(*conditions))
    )
    if customer is None:
        raise NotFound(CUSTOMER_NOT_VISIBLE)
    return customer


async def update_tags(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    customer_id: UUID,
    payload: SidebarTagsUpdate,
    *,
    ip: str | None,
) -> SidebarCustomer:
    """在侧边栏里修改客户标签；企业标签里有的写回企业微信。"""
    customer = await sidebar_customer_by_id(session, principal, customer_id)
    tags = list(dict.fromkeys(t.strip()[:32] for t in payload.tags if t.strip()))
    before = set(customer.tags or [])
    customer.tags = tags
    record_audit(
        session,
        action="customer.update",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(customer.id),
        detail={"tags": tags, "via": "wecom_sidebar"},
        ip=ip,
    )
    await session.commit()
    out = await _customer_out(session, customer)
    await write_back_tags(
        ctx, principal.tenant_id, customer.id, set(tags) - before, before - set(tags)
    )
    return out


async def members(session: AsyncSession) -> SidebarMemberList:
    """一键建群时可以拉进群的企业成员（在职）。"""
    rows = (
        await session.execute(
            select(WecomMember.userid, WecomMember.name, Staff.display_name)
            .outerjoin(Staff, Staff.wecom_userid == WecomMember.userid)
            .where(WecomMember.status == MemberStatus.ACTIVE)
            .order_by(WecomMember.name, WecomMember.userid)
        )
    ).all()
    return SidebarMemberList(
        items=[
            SidebarMemberOut(userid=userid, name=name or userid, staff_name=staff_name)
            for userid, name, staff_name in rows
        ]
    )


async def group_created(
    ctx: AppContext,
    session: AsyncSession,
    principal: Principal,
    payload: SidebarGroupCreated,
    *,
    ip: str | None,
) -> GroupChatOut:
    """员工在侧边栏一键建群（openEnterpriseChat）后，立即同步这个群，群里的客户关联到档案。"""
    customer_id = None
    if payload.external_userid:
        customer_id = (await sidebar_customer(session, principal, payload.external_userid)).id
    record_audit(
        session,
        action="wecom.create_group",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="wecom_group_chat",
        resource_id=payload.chat_id,
        detail={"customer_id": str(customer_id) if customer_id else None},
        ip=ip,
    )
    await session.commit()
    await refresh_group(ctx, principal.tenant_id, payload.chat_id)
    row = (
        await session.execute(
            select(WecomGroupChat, WecomMember.name)
            .outerjoin(WecomMember, WecomMember.userid == WecomGroupChat.owner_userid)
            .where(WecomGroupChat.chat_id == payload.chat_id)
        )
    ).first()
    if row is None:
        raise NotFound("企业微信里没有找到这个客户群")
    return group_out(row[0], row[1])
