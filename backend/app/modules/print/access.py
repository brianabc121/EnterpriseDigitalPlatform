"""手工打印和查看打印记录时的对象与可见性（设计文档 §29.8）。单独放在这里：service 不依赖加工和仓库
模块，加工（领取、指派）和仓库（开单）才能直接调用 service 排队。"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Forbidden, NotFound, Unprocessable
from app.core.permissions import Permission
from app.modules.iam.principal import Principal
from app.modules.orders import production
from app.modules.orders.models import Order
from app.modules.print.models import TicketKind
from app.modules.warehouse import documents
from app.modules.warehouse.models import DocumentKind, StockDocument


async def order_for_print(
    session: AsyncSession, principal: Principal, order_id: uuid.UUID
) -> Order:
    """能打印加工单的订单：自己领取的，或者有指派权限（主管）。"""
    order = await session.scalar(
        select(Order).where(Order.id == order_id, production.visible(principal))
    )
    if order is None:
        raise NotFound("订单不存在")
    if order.worker_id != principal.staff_id and not principal.has(Permission.PRODUCTION_ASSIGN):
        raise Forbidden("只能打印自己加工的订单")
    return order


async def document_for_print(
    session: AsyncSession, principal: Principal, document_id: uuid.UUID
) -> StockDocument:
    """能打印的领料单：能看到这张单据的人（开单人、仓管、仓库的员工）。"""
    document = await documents.get(session, principal, document_id)
    if document.kind != DocumentKind.REQUISITION:
        raise Unprocessable("只有领料单可以打印")
    return document


async def can_see(
    session: AsyncSession, principal: Principal, kind: str, ref_id: uuid.UUID
) -> bool:
    """没有打印机权限的人能不能看这张单据的打印记录。"""
    if kind == TicketKind.ORDER:
        order = await session.scalar(
            select(Order.id).where(Order.id == ref_id, production.visible(principal))
        )
        return order is not None
    if kind == TicketKind.REQUISITION:
        try:
            await documents.get(session, principal, ref_id)
        except NotFound:
            return False
        return True
    return False
