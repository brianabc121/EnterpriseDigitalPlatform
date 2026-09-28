from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.customer.models import Customer
from app.modules.customer.schemas import CustomerCreate, CustomerOut, CustomerPage
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal


def visible_to(principal: Principal) -> ColumnElement[bool]:
    """数据范围（DataScope）：坐席只能看到归属自己的客户；有 customer:read_all 的角色看全部。

    租户之间的隔离由数据库 RLS 保证，这里只处理租户内部的可见性。
    P1 会加入"当前分配给我的会话中的客户"。
    """
    if principal.has(Permission.CUSTOMER_READ_ALL):
        return true()
    return Customer.owner_id == principal.staff_id


def _with_owner() -> Select[Customer, str]:
    # 外连接：客户没有归属坐席时，第二列在运行时为 None。
    return select(Customer, Staff.display_name).outerjoin(
        Staff, and_(Staff.tenant_id == Customer.tenant_id, Staff.id == Customer.owner_id)
    )


def _to_out(customer: Customer, owner_name: str | None) -> CustomerOut:
    return CustomerOut(
        id=customer.id,
        display_name=customer.display_name,
        owner_id=customer.owner_id,
        owner_display_name=owner_name,
        source_channel=customer.source_channel,
        created_at=customer.created_at,
    )


async def list_customers(
    session: AsyncSession, principal: Principal, *, limit: int, offset: int
) -> CustomerPage:
    scope = visible_to(principal)
    total = await session.scalar(select(func.count()).select_from(Customer).where(scope))
    rows = await session.execute(
        _with_owner()
        .where(scope)
        .order_by(Customer.created_at.desc(), Customer.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return CustomerPage(items=[_to_out(c, name) for c, name in rows], total=total or 0)


async def get_customer(
    session: AsyncSession, principal: Principal, customer_id: UUID
) -> CustomerOut:
    row = (
        await session.execute(
            _with_owner().where(Customer.id == customer_id, visible_to(principal))
        )
    ).first()
    # 看不到与不存在返回同样的 404，不暴露其他坐席客户的存在。
    if row is None:
        raise NotFound("客户不存在")
    customer, owner_name = row
    return _to_out(customer, owner_name)


async def create_customer(
    session: AsyncSession, principal: Principal, payload: CustomerCreate, *, ip: str | None
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
    return await get_customer(session, principal, customer.id)
