from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.deps import client_ip
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.customer import service
from app.modules.customer.schemas import (
    CustomerCreate,
    CustomerDetail,
    CustomerOut,
    CustomerPage,
    CustomerUpdate,
)
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal

router = APIRouter(prefix="/api/v1/customers", tags=["customers"], responses=ERROR_RESPONSES)

CanRead = Annotated[Principal, Depends(require_permission(Permission.CUSTOMER_READ))]
CanCreate = Annotated[Principal, Depends(require_permission(Permission.CUSTOMER_CREATE))]


@router.get("", response_model=CustomerPage)
async def list_customers(
    session: TenantDb,
    principal: CanRead,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CustomerPage:
    return await service.list_customers(session, principal, limit=limit, offset=offset)


@router.post("", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
async def create_customer(
    payload: CustomerCreate, request: Request, session: TenantDb, principal: CanCreate
) -> CustomerOut:
    return await service.create_customer(session, principal, payload, ip=client_ip(request))


@router.get("/{customer_id}", response_model=CustomerDetail)
async def get_customer(customer_id: UUID, session: TenantDb, principal: CanRead) -> CustomerDetail:
    """客户档案、备注、标签和各渠道身份（工作台客户面板使用）。"""
    return await service.get_customer_detail(session, principal, customer_id)


@router.patch("/{customer_id}", response_model=CustomerDetail)
async def update_customer(
    customer_id: UUID,
    payload: CustomerUpdate,
    request: Request,
    session: TenantDb,
    principal: CanRead,
) -> CustomerDetail:
    """修改客户名称、备注和标签（能看到这个客户的员工都可以修改）。"""
    return await service.update_customer(
        session, principal, customer_id, payload, ip=client_ip(request)
    )
