"""客户转移申请（设计文档 §14.1）：坐席申请变更客户的归属坐席（转给自己或同事），
有 customer:assign 权限的员工审批；通过后变更归属并记录历史，可以同时在企业微信里在职继承。
同一个客户同时只有一条待审批的申请。
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import Conflict, Forbidden, NotFound, Unprocessable
from app.core.ids import new_id
from app.core.permissions import Permission
from app.modules.audit.service import record_audit
from app.modules.customer.models import (
    Customer,
    CustomerOwnerHistory,
    CustomerTransferRequest,
    OwnerChangeReason,
    TransferRequestStatus,
)
from app.modules.customer.ownership import active_staff, record_owner_change
from app.modules.customer.schemas import (
    TransferRequestCreate,
    TransferRequestList,
    TransferRequestOut,
)
from app.modules.customer.service import ensure_visible
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal

_From = aliased(Staff)
_To = aliased(Staff)
_By = aliased(Staff)
_Decider = aliased(Staff)


def _now() -> datetime:
    return datetime.now(UTC)


async def create_request(
    session: AsyncSession,
    principal: Principal,
    customer_id: UUID,
    payload: TransferRequestCreate,
    *,
    ip: str | None,
) -> TransferRequestOut:
    customer = await ensure_visible(session, principal, customer_id)
    to_owner_id = payload.to_owner_id or principal.staff_id
    await active_staff(session, to_owner_id)
    if customer.owner_id == to_owner_id:
        raise Unprocessable("客户已经归属于这位员工")
    request = CustomerTransferRequest(
        id=new_id(),
        tenant_id=principal.tenant_id,
        customer_id=customer.id,
        from_owner_id=customer.owner_id,
        to_owner_id=to_owner_id,
        requested_by=principal.staff_id,
        reason=payload.reason,
    )
    session.add(request)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise Conflict("这个客户已有待审批的转移申请") from exc
    record_audit(
        session,
        action="customer.transfer_request",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(customer.id),
        detail={"request_id": str(request.id), "to_owner_id": str(to_owner_id)},
        ip=ip,
    )
    await session.commit()
    return await _one(session, request.id)


def _query() -> Select[CustomerTransferRequest, str, str, str, str, str]:
    """申请与客户名、原归属、目标、申请人、审批人的姓名（外连接，运行时可能为空）。"""
    return (
        select(
            CustomerTransferRequest,
            Customer.display_name,
            _From.display_name,
            _To.display_name,
            _By.display_name,
            _Decider.display_name,
        )
        .join(
            Customer,
            and_(
                Customer.tenant_id == CustomerTransferRequest.tenant_id,
                Customer.id == CustomerTransferRequest.customer_id,
            ),
        )
        .outerjoin(_From, _From.id == CustomerTransferRequest.from_owner_id)
        .outerjoin(_To, _To.id == CustomerTransferRequest.to_owner_id)
        .outerjoin(_By, _By.id == CustomerTransferRequest.requested_by)
        .outerjoin(_Decider, _Decider.id == CustomerTransferRequest.decided_by)
    )


def _out(row: tuple[Any, ...]) -> TransferRequestOut:
    request, customer_name, from_name, to_name, by_name, decider_name = row
    return TransferRequestOut(
        id=request.id,
        customer_id=request.customer_id,
        customer_name=customer_name,
        from_owner_id=request.from_owner_id,
        from_owner_name=from_name,
        to_owner_id=request.to_owner_id,
        to_owner_name=to_name,
        requested_by=request.requested_by,
        requested_by_name=by_name,
        reason=request.reason,
        status=request.status,
        decided_by_name=decider_name,
        decided_at=request.decided_at,
        decision_note=request.decision_note,
        created_at=request.created_at,
    )


async def _one(session: AsyncSession, request_id: UUID) -> TransferRequestOut:
    row = (await session.execute(_query().where(CustomerTransferRequest.id == request_id))).first()
    if row is None:
        raise NotFound("转移申请不存在")
    return _out(tuple(row))


async def list_requests(
    session: AsyncSession, principal: Principal, *, status: str | None
) -> TransferRequestList:
    """有分配权限：全部申请；否则只看自己提交的。"""
    query = _query()
    counted = (
        select(func.count())
        .select_from(CustomerTransferRequest)
        .where(CustomerTransferRequest.status == TransferRequestStatus.PENDING)
    )
    if not principal.has(Permission.CUSTOMER_ASSIGN):
        query = query.where(CustomerTransferRequest.requested_by == principal.staff_id)
        counted = counted.where(CustomerTransferRequest.requested_by == principal.staff_id)
    if status:
        query = query.where(CustomerTransferRequest.status == status)
    rows = await session.execute(
        query.order_by(CustomerTransferRequest.created_at.desc()).limit(200)
    )
    return TransferRequestList(
        items=[_out(tuple(row)) for row in rows], pending=int(await session.scalar(counted) or 0)
    )


async def _pending(session: AsyncSession, request_id: UUID) -> CustomerTransferRequest:
    request = await session.scalar(
        select(CustomerTransferRequest)
        .where(CustomerTransferRequest.id == request_id)
        .with_for_update()
    )
    if request is None:
        raise NotFound("转移申请不存在")
    if request.status != TransferRequestStatus.PENDING:
        raise Conflict("这条申请已经处理过了")
    return request


async def approve(
    session: AsyncSession,
    principal: Principal,
    request_id: UUID,
    *,
    note: str | None,
    ip: str | None,
) -> tuple[TransferRequestOut, CustomerOwnerHistory | None]:
    """通过申请：变更归属并记录历史。返回（申请，归属变更记录）。"""
    request = await _pending(session, request_id)
    customer = await session.get(Customer, request.customer_id, with_for_update=True)
    assert customer is not None
    if request.to_owner_id is not None:
        await active_staff(session, request.to_owner_id)
    history = await record_owner_change(
        session,
        customer,
        request.to_owner_id,
        actor_id=principal.staff_id,
        reason=OwnerChangeReason.REQUEST,
        note=request.reason,
    )
    _decide(request, principal, TransferRequestStatus.APPROVED, note)
    record_audit(
        session,
        action="customer.transfer_approve",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(customer.id),
        detail={"request_id": str(request.id), "to_owner_id": str(request.to_owner_id)},
        ip=ip,
    )
    await session.commit()
    return await _one(session, request.id), history


def _decide(
    request: CustomerTransferRequest,
    principal: Principal,
    status: TransferRequestStatus,
    note: str | None,
) -> None:
    request.status = status
    request.decided_by = principal.staff_id
    request.decided_at = _now()
    request.decision_note = note


async def reject(
    session: AsyncSession,
    principal: Principal,
    request_id: UUID,
    *,
    note: str | None,
    ip: str | None,
) -> TransferRequestOut:
    request = await _pending(session, request_id)
    _decide(request, principal, TransferRequestStatus.REJECTED, note)
    record_audit(
        session,
        action="customer.transfer_reject",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(request.customer_id),
        detail={"request_id": str(request.id)},
        ip=ip,
    )
    await session.commit()
    return await _one(session, request.id)


async def cancel(
    session: AsyncSession, principal: Principal, request_id: UUID
) -> TransferRequestOut:
    request = await _pending(session, request_id)
    if request.requested_by != principal.staff_id:
        raise Forbidden("只能撤回自己的申请")
    request.status = TransferRequestStatus.CANCELLED
    request.decided_at = _now()
    await session.commit()
    return await _one(session, request.id)
