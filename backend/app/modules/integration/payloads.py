"""开放接口的返回和事件推送共用的订单、待办内容（企业系统看到的数据）。

订单的收货信息是明文（企业系统发货需要），成本价不对外提供。
"""

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.contracts.models import Contract
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff
from app.modules.integration.schemas import (
    OpenCustomerRef,
    OpenOpportunity,
    OpenOrder,
    OpenOrderItem,
    OpenPayment,
    OpenReceiver,
    OpenTodo,
)
from app.modules.opportunities.models import Opportunity, PipelineStage
from app.modules.orders import service
from app.modules.orders.models import Order
from app.modules.security.keys import TenantKeyring
from app.modules.todos.models import Todo, TodoType


async def order_out(
    session: AsyncSession, keys: TenantKeyring, app_settings: Settings, order: Order
) -> OpenOrder:
    # 刚提交的订单：数据库生成的更新时间需要重新读取。
    if "updated_at" in inspect(order).unloaded:
        await session.refresh(order)
    items = await service.load_items(session, order.id)
    payments = await service.load_payments(session, order.id)
    customer = (
        await session.scalar(select(Customer.display_name).where(Customer.id == order.customer_id))
        if order.customer_id
        else None
    )
    receiver = await service.reveal_receiver(keys, order.tenant_id, order.receiver or {})
    return OpenOrder(
        id=order.id,
        no=order.no,
        external_no=order.external_no,
        status=order.status,
        source=order.source,
        customer=OpenCustomerRef(id=order.customer_id, name=customer),
        items=[
            OpenOrderItem(
                code=i.code,
                name=i.name,
                model=i.model,
                spec=i.spec,
                raw_text=i.raw_text,
                quantity=i.quantity,
                list_price=i.list_price,
                unit_price=i.unit_price,
                amount=i.amount,
                work_status=i.work_status,
                shortage_qty=i.shortage_qty,
                restock_date=i.restock_date,
            )
            for i in items
        ],
        items_amount=order.items_amount,
        discount=order.discount,
        total=order.total,
        payment_method=order.payment_method,
        deposit_amount=order.deposit_amount,
        credit_due_date=order.credit_due_date,
        payment_status=order.payment_status,
        paid_amount=order.paid_amount,
        refunded_amount=order.refunded_amount,
        outstanding=service.outstanding(order),
        payments=[
            OpenPayment(
                kind=p.kind,
                amount=p.amount,
                channel=p.channel,
                paid_at=p.paid_at,
                reference_no=p.reference_no,
                recorded_by_type=p.recorded_by_type,
                voided=p.voided_at is not None,
            )
            for p in payments
        ],
        receiver=OpenReceiver(**receiver),
        expected_at=order.expected_at,
        customer_note=order.customer_note,
        shipping_company=order.shipping_company,
        tracking_no=order.tracking_no,
        tracking_url=service.tracking_url(app_settings, order.tracking_token),
        cancel_reason=order.cancel_reason,
        version=order.version,
        created_at=order.created_at,
        updated_at=order.updated_at,
        submitted_at=order.submitted_at,
        confirmed_at=order.confirmed_at,
        started_at=order.started_at,
        processed_at=order.processed_at,
        shortage=order.shortage_at is not None,
        shipped_at=order.shipped_at,
        completed_at=order.completed_at,
        cancelled_at=order.cancelled_at,
    )


async def todo_out(session: AsyncSession, todo: Todo) -> OpenTodo:
    type_code = await session.scalar(select(TodoType.code).where(TodoType.id == todo.type_id))
    assignee = (
        await session.scalar(select(Staff.display_name).where(Staff.id == todo.assignee_id))
        if todo.assignee_id
        else None
    )
    order_no = (
        await session.scalar(select(Order.no).where(Order.id == todo.order_id))
        if todo.order_id
        else None
    )
    return OpenTodo(
        id=todo.id,
        no=todo.no,
        type=type_code or "",
        title=todo.title,
        status=todo.status,
        assignee_name=assignee,
        customer_id=todo.customer_id,
        order_no=order_no,
        due_at=todo.due_at,
        result=todo.result,
        external_ref=todo.external_ref,
        created_at=todo.created_at,
        closed_at=todo.closed_at,
    )


async def find_order(session: AsyncSession, ref: str, *, lock: bool = False) -> Order | None:
    """按平台订单号或企业系统的订单号找到订单。"""
    statement = select(Order).where((Order.no == ref) | (Order.external_no == ref)).limit(1)
    if lock:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def opportunity_out(session: AsyncSession, opportunity: Opportunity) -> OpenOpportunity:
    """商机的推送内容和开放接口的返回（设计文档 §40.13）。"""
    stage = await session.get(PipelineStage, opportunity.stage_id)
    customer = await session.get(Customer, opportunity.customer_id)
    owner = (
        (
            await session.execute(
                select(Staff.username, Staff.display_name).where(Staff.id == opportunity.owner_id)
            )
        ).first()
        if opportunity.owner_id is not None
        else None
    )
    order_no = (
        await session.scalar(select(Order.no).where(Order.id == opportunity.order_id))
        if opportunity.order_id is not None
        else None
    )
    contract_no = (
        await session.scalar(select(Contract.no).where(Contract.id == opportunity.contract_id))
        if opportunity.contract_id is not None
        else None
    )
    return OpenOpportunity(
        id=opportunity.id,
        customer_id=opportunity.customer_id,
        customer_name=customer.display_name if customer else "",
        name=opportunity.name,
        status=opportunity.status,
        stage=stage.code if stage else "",
        stage_name=stage.name if stage else "",
        level=opportunity.level,
        source=opportunity.source,
        interest=opportunity.interest,
        concerns=opportunity.concerns,
        amount=opportunity.amount,
        probability=(
            opportunity.probability
            if opportunity.probability is not None
            else (stage.probability if stage else 0)
        ),
        expected_close_at=opportunity.expected_close_at,
        next_follow_at=opportunity.next_follow_at,
        owner_username=owner[0] if owner else None,
        owner_name=owner[1] if owner else None,
        order_no=order_no,
        contract_no=contract_no,
        lost_reason_code=opportunity.lost_reason_code,
        lost_reason=opportunity.lost_reason,
        created_at=opportunity.created_at,
        updated_at=opportunity.updated_at,
        closed_at=opportunity.closed_at,
    )
