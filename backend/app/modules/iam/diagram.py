"""先生成图形卡片，再通过常规员工创建事务完善账号。"""

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, Unprocessable
from app.core.ids import new_id
from app.modules.audit.service import record_audit
from app.modules.iam import owner
from app.modules.iam.models import Staff, StaffDiagramNode
from app.modules.iam.principal import Principal
from app.modules.iam.schemas import StaffDiagramNodeCreate, StaffDiagramNodeOut


async def list_nodes(session: AsyncSession, tenant_id: UUID) -> list[StaffDiagramNodeOut]:
    rows = await session.scalars(
        select(StaffDiagramNode)
        .where(StaffDiagramNode.tenant_id == tenant_id)
        .order_by(StaffDiagramNode.created_at, StaffDiagramNode.id)
    )
    return [StaffDiagramNodeOut.model_validate(row) for row in rows]


async def create_node(
    session: AsyncSession,
    principal: Principal,
    payload: StaffDiagramNodeCreate,
    *,
    ip: str | None,
) -> StaffDiagramNodeOut:
    if payload.parent_id is not None:
        source_node_id = await session.scalar(
            select(StaffDiagramNode.id)
            .where(
                StaffDiagramNode.id == payload.parent_id,
                StaffDiagramNode.tenant_id == principal.tenant_id,
            )
            .with_for_update()
        )
        staff = (
            await session.scalar(
                select(Staff.id)
                .where(
                    Staff.id == payload.parent_id,
                    Staff.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
            if source_node_id is None
            else None
        )
        if source_node_id is None and staff is None:
            raise Unprocessable("来源卡片不存在或不属于当前企业")
    pending = await session.scalar(
        select(func.count())
        .select_from(StaffDiagramNode)
        .where(
            StaffDiagramNode.tenant_id == principal.tenant_id,
            StaffDiagramNode.staff_id.is_(None),
        )
    )
    if (pending or 0) >= 500:
        raise Unprocessable("待完善卡片已达 500 张，请先完善已有卡片")
    node = StaffDiagramNode(
        id=new_id(),
        tenant_id=principal.tenant_id,
        parent_id=payload.parent_id,
        direction=payload.direction,
    )
    session.add(node)
    record_audit(
        session,
        action="staff.diagram.create",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="staff_diagram_node",
        resource_id=str(node.id),
        detail={
            "parent_id": str(payload.parent_id) if payload.parent_id else None,
            "direction": payload.direction,
        },
        ip=ip,
    )
    await session.commit()
    await session.refresh(node)
    return StaffDiagramNodeOut.model_validate(node)


async def pending_node(session: AsyncSession, tenant_id: UUID, node_id: UUID) -> StaffDiagramNode:
    node = await session.scalar(
        select(StaffDiagramNode)
        .where(
            StaffDiagramNode.id == node_id,
            StaffDiagramNode.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    if node is None:
        raise Unprocessable("待完善卡片不存在或不属于当前企业")
    if node.staff_id is not None:
        raise Conflict("这张卡片已经创建了员工，请刷新后编辑")
    return node


async def delete_card(
    session: AsyncSession, principal: Principal, card_id: UUID, *, ip: str | None
) -> None:
    """删除账号及卡片；子分支接到原父节点，业务引用阻止账号删除。"""
    from app.modules.iam import manage
    from app.modules.iam.service import roles_of

    await session.scalars(
        select(Staff.id)
        .where(Staff.tenant_id == principal.tenant_id)
        .order_by(Staff.id)
        .with_for_update()
    )
    await session.scalars(
        select(StaffDiagramNode.id)
        .where(StaffDiagramNode.tenant_id == principal.tenant_id)
        .order_by(StaffDiagramNode.id)
        .with_for_update()
    )
    node = await session.scalar(
        select(StaffDiagramNode)
        .where(
            StaffDiagramNode.id == card_id,
            StaffDiagramNode.tenant_id == principal.tenant_id,
        )
        .with_for_update()
    )
    if (
        node is None
        and await session.scalar(
            select(Staff.id).where(Staff.id == card_id, Staff.tenant_id == principal.tenant_id)
        )
        is None
    ):
        raise NotFound("卡片不存在或不属于当前企业，企业卡片不能删除")
    staff_id = node.staff_id if node else card_id
    staff = None
    if node is None or staff_id is not None:
        staff = await session.scalar(
            select(Staff)
            .where(
                Staff.id == staff_id,
                Staff.tenant_id == principal.tenant_id,
            )
            .with_for_update()
        )
        if staff is None:
            raise NotFound("卡片不存在或不属于当前企业，企业卡片不能删除")
        if staff.id == principal.staff_id:
            raise Conflict("不能删除自己的账号")
        await owner.refuse_others(session, principal, staff.id, "不能删除企业所有者")
        # 锁住当前企业所有员工，使并发删除管理员仍至少保留一人。
        await manage._target(session, principal, staff.id)
        roles = await roles_of(session, staff.id)
        if (
            any(role.code == "tenant_admin" for role in roles)
            and await manage._other_active_admins(session, staff.id) == 0
        ):
            raise Conflict("至少保留一名启用的企业管理员")
    elif node is None:
        raise NotFound("卡片不存在或不属于当前企业，企业卡片不能删除")
    parent_id = node.parent_id if node else staff.diagram_parent_id if staff else None
    sources = [card_id]
    if staff is not None:
        sources.append(staff.id)
    try:
        await session.execute(
            update(StaffDiagramNode)
            .where(
                StaffDiagramNode.tenant_id == principal.tenant_id,
                StaffDiagramNode.parent_id.in_(sources),
            )
            .values(parent_id=parent_id)
        )
        if staff is not None:
            await session.execute(
                update(Staff)
                .where(
                    Staff.tenant_id == principal.tenant_id,
                    Staff.diagram_parent_id == staff.id,
                )
                .values(diagram_parent_id=staff.diagram_parent_id)
            )
        record_audit(
            session,
            action="staff.diagram.delete",
            actor_type="staff",
            actor_id=principal.staff_id,
            tenant_id=principal.tenant_id,
            resource_type="staff_diagram_node",
            resource_id=str(card_id),
            detail={"staff_id": str(staff.id) if staff else None},
            ip=ip,
        )
        if node is not None:
            await session.delete(node)
        if staff is not None:
            await session.delete(staff)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("员工仍关联客户、会话或业务记录，请先完成交接和清理后再删除") from exc
