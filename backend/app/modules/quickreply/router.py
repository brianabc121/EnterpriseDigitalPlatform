from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal
from app.modules.quickreply import service
from app.modules.quickreply.schemas import (
    QuickReplyCreate,
    QuickReplyList,
    QuickReplyOut,
    QuickReplyUpdate,
)

router = APIRouter(
    prefix="/api/v1/quick-replies", tags=["quick-replies"], responses=ERROR_RESPONSES
)

CanServe = Annotated[Principal, Depends(require_permission(Permission.WORKBENCH_USE))]


@router.get("", response_model=QuickReplyList)
async def list_replies(session: TenantDb, principal: CanServe) -> QuickReplyList:
    """全员共享的话术和自己的话术。"""
    return QuickReplyList(items=await service.list_replies(session, principal))


@router.post("", response_model=QuickReplyOut, status_code=status.HTTP_201_CREATED)
async def create_reply(
    payload: QuickReplyCreate, session: TenantDb, principal: CanServe
) -> QuickReplyOut:
    return await service.create_reply(session, principal, payload)


@router.patch("/{reply_id}", response_model=QuickReplyOut)
async def update_reply(
    reply_id: UUID, payload: QuickReplyUpdate, session: TenantDb, principal: CanServe
) -> QuickReplyOut:
    return await service.update_reply(session, principal, reply_id, payload)


@router.delete("/{reply_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_reply(reply_id: UUID, session: TenantDb, principal: CanServe) -> Response:
    await service.delete_reply(session, principal, reply_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
