from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from app.core.deps import client_ip
from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.channels import service
from app.modules.channels.schemas import ChannelList, ChannelOut, ChannelUpdate
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal

router = APIRouter(prefix="/api/v1/channels", tags=["channels"], responses=ERROR_RESPONSES)

CanManage = Annotated[Principal, Depends(require_permission(Permission.SETTINGS_MANAGE))]


@router.get("", response_model=ChannelList)
async def list_channels(session: TenantDb, _: CanManage) -> ChannelList:
    channels = await service.list_channels(session)
    return ChannelList(items=[ChannelOut.model_validate(c) for c in channels])


@router.patch("/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: UUID,
    payload: ChannelUpdate,
    request: Request,
    session: TenantDb,
    principal: CanManage,
) -> ChannelOut:
    channel = await service.update_channel(
        session, principal, channel_id, payload, ip=client_ip(request)
    )
    return ChannelOut.model_validate(channel)
