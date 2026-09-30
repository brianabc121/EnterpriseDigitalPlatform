from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.conversation import service
from app.modules.conversation.schemas import MessagePage, RoomPage
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal

router = APIRouter(prefix="/api/v1/rooms", tags=["conversation"], responses=ERROR_RESPONSES)

CanRead = Annotated[Principal, Depends(require_permission(Permission.CUSTOMER_READ))]


@router.get("", response_model=RoomPage)
async def list_rooms(
    session: TenantDb,
    principal: CanRead,
    customer_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RoomPage:
    """按最近消息时间倒序列出可见的 Room。"""
    return await service.list_rooms(
        session, principal, customer_id=customer_id, limit=limit, offset=offset
    )


@router.get("/{room_id}/messages", response_model=MessagePage)
async def list_messages(
    room_id: UUID,
    session: TenantDb,
    principal: CanRead,
    before: Annotated[UUID | None, Query(description="上一页最后一条消息的 id")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MessagePage:
    return await service.list_messages(session, principal, room_id, before=before, limit=limit)
