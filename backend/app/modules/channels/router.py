from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.errors import ERROR_RESPONSES
from app.core.permissions import Permission
from app.modules.channels import service
from app.modules.channels.schemas import ChannelList, ChannelOut
from app.modules.iam.deps import TenantDb, require_permission
from app.modules.iam.principal import Principal

router = APIRouter(prefix="/api/v1/channels", tags=["channels"], responses=ERROR_RESPONSES)

CanManage = Annotated[Principal, Depends(require_permission(Permission.SETTINGS_MANAGE))]


@router.get("", response_model=ChannelList)
async def list_channels(session: TenantDb, _: CanManage) -> ChannelList:
    channels = await service.list_channels(session)
    return ChannelList(items=[ChannelOut.model_validate(c) for c in channels])
