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
    return ChannelList(items=[ChannelOut.of(c) for c in channels])


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
    return ChannelOut.of(channel)


@router.post("/{channel_id}/identity-secret", response_model=ChannelOut)
async def rotate_identity_secret(
    channel_id: UUID, request: Request, session: TenantDb, principal: CanManage
) -> ChannelOut:
    """生成（或更换）实名访客签名密钥。网站后端用它为登录用户签名，Widget 据此识别为实名访客。"""
    channel = await service.rotate_identity_secret(
        session, principal, channel_id, ip=client_ip(request)
    )
    return ChannelOut.of(channel)
