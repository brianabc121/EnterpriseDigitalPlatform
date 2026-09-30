from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from app.core.errors import ERROR_RESPONSES
from app.modules.iam.deps import CurrentPrincipal, TenantDb
from app.modules.notifications import service
from app.modules.notifications.schemas import NotificationList

router = APIRouter(
    prefix="/api/v1/notifications", tags=["notifications"], responses=ERROR_RESPONSES
)


@router.get("", response_model=NotificationList)
async def list_notifications(
    session: TenantDb,
    principal: CurrentPrincipal,
    unread: Annotated[bool, Query(description="只看未读的")] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> NotificationList:
    """我的站内信（新的在前）和未读数。"""
    return await service.list_for(session, principal.staff_id, unread_only=unread, limit=limit)


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def read_all(session: TenantDb, principal: CurrentPrincipal) -> Response:
    await service.mark_all_read(session, principal.staff_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def read_one(
    notification_id: UUID, session: TenantDb, principal: CurrentPrincipal
) -> Response:
    await service.mark_read(session, principal.staff_id, notification_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
