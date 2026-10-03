from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.channels.models import ChannelAccount
from app.modules.conversation.models import ChatSession, SessionStatus, SessionWatcher
from app.modules.customer import sensitive
from app.modules.customer.models import Customer, CustomerIdentity
from app.modules.customer.schemas import (
    CustomerCreate,
    CustomerDetail,
    CustomerIdentityOut,
    CustomerOut,
    CustomerPage,
    CustomerSensitive,
    CustomerUpdate,
)
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.opportunities.models import OPEN_STATUSES, Opportunity, PipelineStage
from app.modules.routing.scope import team_members
from app.modules.security.keys import TenantKeyring


def visible_to(principal: Principal) -> ColumnElement[bool]:
    """数据范围（DataScope，设计文档 §13.2）。

    - customer:read_all 或 session:read_all：本租户全部客户；
    - 否则：归属自己的客户，加上当前有会话分配给自己的客户（服务期间临时可见，能看到完整历史），
      以及正在旁听或协助的会话的客户；
    - 另有 session:read_team 时：组员名下的客户、组员正在接待的客户。

    租户之间的隔离由数据库 RLS 保证，这里只处理租户内部的可见性。
    """
    if principal.has(Permission.CUSTOMER_READ_ALL) or principal.has(Permission.SESSION_READ_ALL):
        return true()
    me = principal.staff_id
    conditions: list[ColumnElement[bool]] = [
        Customer.owner_id == me,
        _serving(ChatSession.assignee_id == me),
        # 正在旁听或协助这位客户的会话。
        _serving(
            ChatSession.id.in_(
                select(SessionWatcher.session_id).where(
                    SessionWatcher.staff_id == me, SessionWatcher.left_at.is_(None)
                )
            )
        ),
    ]
    if principal.has(Permission.SESSION_READ_TEAM):
        team = team_members(me)
        conditions += [Customer.owner_id.in_(team), _serving(ChatSession.assignee_id.in_(team))]
    return or_(*conditions)


def _serving(assignee: ColumnElement[bool]) -> ColumnElement[bool]:
    """客户有未结束的会话，且接待人满足条件。"""
    return (
        select(ChatSession.id)
        .where(
            ChatSession.tenant_id == Customer.tenant_id,
            ChatSession.customer_id == Customer.id,
            ChatSession.status != SessionStatus.CLOSED,
            assignee,
        )
        .exists()
    )


def _with_owner() -> Select[Customer, str]:
    # 外连接：客户没有归属坐席时，第二列在运行时为 None。
    return select(Customer, Staff.display_name).outerjoin(
        Staff, and_(Staff.tenant_id == Customer.tenant_id, Staff.id == Customer.owner_id)
    )


async def _to_out(
    keys: TenantKeyring,
    customer: Customer,
    owner_name: str | None,
    opportunity: tuple[str, str] | None = None,
) -> CustomerOut:
    phone, email = await sensitive.masked(keys, customer)
    return CustomerOut(
        id=customer.id,
        display_name=customer.display_name,
        owner_id=customer.owner_id,
        owner_display_name=owner_name,
        source_channel=customer.source_channel,
        tags=list(customer.tags or []),
        phone=phone,
        email=email,
        company=customer.company,
        created_at=customer.created_at,
        opportunity_status=opportunity[0] if opportunity else None,
        opportunity_stage=opportunity[1] if opportunity else None,
    )


async def opportunity_statuses(
    session: AsyncSession, customer_ids: list[UUID]
) -> dict[UUID, tuple[str, str]]:
    """客户的"商机"标签：待确认或跟进中的商机的状态和阶段名称（设计文档 §40.8）。"""
    if not customer_ids:
        return {}
    rows = await session.execute(
        select(Opportunity.customer_id, Opportunity.status, PipelineStage.name)
        .join(PipelineStage, PipelineStage.id == Opportunity.stage_id)
        .where(
            Opportunity.customer_id.in_(customer_ids),
            Opportunity.status.in_(OPEN_STATUSES),
        )
    )
    return {customer_id: (status, stage) for customer_id, status, stage in rows}


async def search_condition(keys: TenantKeyring, tenant_id: UUID, q: str) -> ColumnElement[bool]:
    """名称、公司包含搜索词，或者手机号、邮箱与搜索词完全一致（盲索引）。"""
    term = q.strip()
    pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    conditions: list[ColumnElement[bool]] = [
        Customer.display_name.ilike(pattern, escape="\\"),
        Customer.company.ilike(pattern, escape="\\"),
    ]
    phone_hash, email_hash = await sensitive.search_indexes(keys, tenant_id, term)
    if phone_hash:
        conditions.append(Customer.phone_hash == phone_hash)
    if email_hash:
        conditions.append(Customer.email_hash == email_hash)
    return or_(*conditions)


async def list_customers(
    session: AsyncSession,
    keys: TenantKeyring,
    principal: Principal,
    *,
    limit: int,
    offset: int,
    q: str | None = None,
) -> CustomerPage:
    scope = visible_to(principal)
    if q and q.strip():
        scope = and_(scope, await search_condition(keys, principal.tenant_id, q))
    total = await session.scalar(select(func.count()).select_from(Customer).where(scope))
    rows = (
        await session.execute(
            _with_owner()
            .where(scope)
            .order_by(Customer.created_at.desc(), Customer.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    opportunities = await opportunity_statuses(session, [c.id for c, _ in rows])
    return CustomerPage(
        items=[await _to_out(keys, c, name, opportunities.get(c.id)) for c, name in rows],
        total=total or 0,
    )


async def ensure_visible(
    session: AsyncSession, principal: Principal, customer_id: UUID
) -> Customer:
    """客户对这个员工可见时返回客户，否则 404。"""
    customer, _ = await _visible_customer(session, principal, customer_id)
    return customer


async def get_customer(
    session: AsyncSession, keys: TenantKeyring, principal: Principal, customer_id: UUID
) -> CustomerOut:
    customer, owner_name = await _visible_customer(session, principal, customer_id)
    return await _to_out(keys, customer, owner_name)


async def _visible_customer(
    session: AsyncSession, principal: Principal, customer_id: UUID
) -> tuple[Customer, str | None]:
    row = (
        await session.execute(
            _with_owner().where(Customer.id == customer_id, visible_to(principal))
        )
    ).first()
    # 看不到与不存在返回同样的 404，不暴露其他坐席客户的存在。
    if row is None:
        raise NotFound("客户不存在")
    customer, owner_name = row
    return customer, owner_name


async def get_customer_detail(
    session: AsyncSession, keys: TenantKeyring, principal: Principal, customer_id: UUID
) -> CustomerDetail:
    """客户面板：档案、备注、标签和各渠道身份。"""
    customer, owner_name = await _visible_customer(session, principal, customer_id)
    rows = await session.execute(
        select(CustomerIdentity, ChannelAccount.type, ChannelAccount.name)
        .join(
            ChannelAccount,
            and_(
                ChannelAccount.tenant_id == CustomerIdentity.tenant_id,
                ChannelAccount.id == CustomerIdentity.channel_account_id,
            ),
        )
        .where(CustomerIdentity.customer_id == customer_id)
        .order_by(CustomerIdentity.created_at)
    )
    opportunities = await opportunity_statuses(session, [customer.id])
    return CustomerDetail(
        **(await _to_out(keys, customer, owner_name, opportunities.get(customer.id))).model_dump(),
        notes=customer.notes,
        identities=[
            CustomerIdentityOut(
                id=identity.id,
                channel_account_id=identity.channel_account_id,
                channel_type=channel_type,
                channel_name=channel_name,
                verified=identity.verified,
                # 加密保存的字段（邮件渠道的发件人地址）不返回；地址见客户档案的邮箱（脱敏）。
                profile={
                    k: v for k, v in (identity.profile or {}).items() if not k.endswith("_enc")
                },
                last_seen_at=identity.last_seen_at,
                created_at=identity.created_at,
            )
            for identity, channel_type, channel_name in rows
        ],
    )


async def _apply_contact(
    keys: TenantKeyring, customer: Customer, changes: dict[str, object]
) -> None:
    if changes.get("phone") is not None:
        await sensitive.set_phone(keys, customer, str(changes["phone"]))
    if changes.get("email") is not None:
        await sensitive.set_email(keys, customer, str(changes["email"]))
    if changes.get("company") is not None:
        customer.company = str(changes["company"]).strip() or None


async def update_customer(
    session: AsyncSession,
    keys: TenantKeyring,
    principal: Principal,
    customer_id: UUID,
    payload: CustomerUpdate,
    *,
    ip: str | None,
) -> CustomerDetail:
    """能看到客户的员工（归属坐席、正在接待的坐席、主管、管理员）可以修改名称、备注、标签和
    联系方式。"""
    customer, _ = await _visible_customer(session, principal, customer_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("display_name") is not None:
        customer.display_name = changes["display_name"]
    if "notes" in changes:
        customer.notes = changes["notes"] or None
    if changes.get("tags") is not None:
        customer.tags = list(dict.fromkeys(changes["tags"]))
    await _apply_contact(keys, customer, changes)
    record_audit(
        session,
        action="customer.update",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(customer.id),
        detail={"fields": sorted(changes)},
        ip=ip,
    )
    await session.commit()
    return await get_customer_detail(session, keys, principal, customer_id)


async def create_customer(
    session: AsyncSession,
    keys: TenantKeyring,
    principal: Principal,
    payload: CustomerCreate,
    *,
    ip: str | None,
) -> CustomerOut:
    owner_id = payload.owner_id or principal.staff_id
    if owner_id != principal.staff_id:
        if not principal.has(Permission.CUSTOMER_ASSIGN):
            raise Forbidden("没有指定归属坐席的权限")
        owner = await session.get(Staff, owner_id)
        if owner is None or owner.status != StaffStatus.ACTIVE:
            raise Unprocessable("归属坐席不存在或已停用")

    customer = Customer(
        tenant_id=principal.tenant_id, display_name=payload.display_name, owner_id=owner_id
    )
    await _apply_contact(keys, customer, payload.model_dump(exclude_unset=True))
    session.add(customer)
    await session.flush()
    record_audit(
        session,
        action="customer.create",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(customer.id),
        ip=ip,
    )
    await session.commit()
    return await get_customer(session, keys, principal, customer.id)


async def reveal_sensitive(
    session: AsyncSession,
    keys: TenantKeyring,
    principal: Principal,
    customer_id: UUID,
    *,
    ip: str | None,
) -> CustomerSensitive:
    """查看手机号、邮箱明文；每次查看记一条审计。"""
    customer, _ = await _visible_customer(session, principal, customer_id)
    phone, email = await sensitive.reveal(keys, customer)
    record_audit(
        session,
        action="customer.view_sensitive",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(customer.id),
        detail={"fields": [f for f, v in (("phone", phone), ("email", email)) if v]},
        ip=ip,
    )
    await session.commit()
    return CustomerSensitive(phone=phone, email=email)
