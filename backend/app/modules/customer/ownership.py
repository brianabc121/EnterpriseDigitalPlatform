"""客户归属变更：会话转接时同时转移、管理员批量转移、离职交接。每次变更都记录历史。"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound, Unprocessable
from app.modules.customer.models import Customer, CustomerOwnerHistory, OwnerChangeReason
from app.modules.customer.schemas import OwnerHistoryOut
from app.modules.iam.models import Staff, StaffStatus
from app.modules.iam.principal import Principal
from app.modules.routing.models import SkillGroupMember


async def change_owner(
    session: AsyncSession,
    customer: Customer,
    owner_id: uuid.UUID | None,
    *,
    actor_id: uuid.UUID | None,
    reason: OwnerChangeReason,
    note: str | None = None,
) -> bool:
    """变更客户归属并记录历史（由调用方提交）；归属没有变化时返回 False。"""
    if customer.owner_id == owner_id:
        return False
    session.add(
        CustomerOwnerHistory(
            tenant_id=customer.tenant_id,
            customer_id=customer.id,
            from_owner_id=customer.owner_id,
            to_owner_id=owner_id,
            actor_id=actor_id,
            reason=reason,
            note=note,
        )
    )
    customer.owner_id = owner_id
    return True


async def active_staff(session: AsyncSession, staff_id: uuid.UUID) -> Staff:
    staff = await session.get(Staff, staff_id)
    if staff is None or staff.status != StaffStatus.ACTIVE:
        raise Unprocessable("目标员工不存在或已停用")
    return staff


async def transfer_customers(
    session: AsyncSession,
    principal: Principal,
    customer_ids: list[uuid.UUID],
    to_owner_id: uuid.UUID | None,
    *,
    note: str | None,
) -> int:
    """管理员批量转移客户归属（to_owner_id 为空表示取消归属）。返回实际变更的数量。"""
    if to_owner_id is not None:
        await active_staff(session, to_owner_id)
    customers = (await session.scalars(select(Customer).where(Customer.id.in_(customer_ids)))).all()
    if len(customers) != len(set(customer_ids)):
        raise NotFound("部分客户不存在")
    changed = 0
    for customer in customers:
        changed += await change_owner(
            session,
            customer,
            to_owner_id,
            actor_id=principal.staff_id,
            reason=OwnerChangeReason.MANUAL,
            note=note,
        )
    await session.commit()
    return changed


async def hand_over(
    session: AsyncSession,
    principal: Principal,
    from_staff_id: uuid.UUID,
    *,
    to_owner_id: uuid.UUID | None,
    to_group_id: uuid.UUID | None,
    note: str | None,
) -> int:
    """离职或调岗交接：把某位员工名下的全部客户转给指定员工，或平均分给技能组的成员
    （按成员当前名下的客户数，少的优先）。"""
    if (to_owner_id is None) == (to_group_id is None):
        raise Unprocessable("请指定接手的员工或技能组")
    if await session.get(Staff, from_staff_id) is None:
        raise NotFound("员工不存在")
    if to_owner_id is not None:
        await active_staff(session, to_owner_id)
        targets = [to_owner_id]
    else:
        targets = list(
            (
                await session.scalars(
                    select(SkillGroupMember.staff_id)
                    .join(
                        Staff,
                        (Staff.tenant_id == SkillGroupMember.tenant_id)
                        & (Staff.id == SkillGroupMember.staff_id),
                    )
                    .where(
                        SkillGroupMember.skill_group_id == to_group_id,
                        SkillGroupMember.staff_id != from_staff_id,
                        Staff.status == StaffStatus.ACTIVE,
                    )
                    .order_by(SkillGroupMember.staff_id)
                )
            ).all()
        )
        if not targets:
            raise Unprocessable("技能组里没有可以接手的员工")
    if from_staff_id in targets:
        raise Unprocessable("不能交接给自己")
    customers = (
        await session.scalars(
            select(Customer).where(Customer.owner_id == from_staff_id).order_by(Customer.created_at)
        )
    ).all()
    # 平均分配：每次交给当前名下客户最少的成员（相同时按成员顺序）。
    owned = dict.fromkeys(targets, 0)
    counts = await session.execute(
        select(Customer.owner_id, func.count())
        .where(Customer.owner_id.in_(targets))
        .group_by(Customer.owner_id)
    )
    for owner_id, count in counts:
        if owner_id is not None:
            owned[owner_id] = count
    for customer in customers:
        owner = min(targets, key=lambda t: owned[t])
        owned[owner] += 1
        await change_owner(
            session,
            customer,
            owner,
            actor_id=principal.staff_id,
            reason=OwnerChangeReason.HANDOVER,
            note=note,
        )
    await session.commit()
    return len(customers)


async def owner_history(session: AsyncSession, customer_id: uuid.UUID) -> list[OwnerHistoryOut]:
    rows = await session.scalars(
        select(CustomerOwnerHistory)
        .where(CustomerOwnerHistory.customer_id == customer_id)
        .order_by(CustomerOwnerHistory.created_at.desc(), CustomerOwnerHistory.id.desc())
    )
    history = list(rows)
    staff_ids = {
        s for h in history for s in (h.from_owner_id, h.to_owner_id, h.actor_id) if s is not None
    }
    names: dict[uuid.UUID, str] = {}
    if staff_ids:
        result = await session.execute(
            select(Staff.id, Staff.display_name).where(Staff.id.in_(staff_ids))
        )
        names = {staff_id: name for staff_id, name in result}
    return [
        OwnerHistoryOut(
            id=h.id,
            from_owner_id=h.from_owner_id,
            from_owner_name=names.get(h.from_owner_id) if h.from_owner_id else None,
            to_owner_id=h.to_owner_id,
            to_owner_name=names.get(h.to_owner_id) if h.to_owner_id else None,
            actor_name=names.get(h.actor_id) if h.actor_id else None,
            reason=h.reason,
            note=h.note,
            created_at=h.created_at,
        )
        for h in history
    ]
