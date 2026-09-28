from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.channels.models import ChannelStatus


class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: str
    name: str
    public_key: str
    status: str
    routing_policy_id: UUID | None = Field(description="为空时使用租户的默认路由策略")
    created_at: datetime


class ChannelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    status: ChannelStatus | None = None
    routing_policy_id: UUID | None = Field(
        default=None, description="绑定的路由策略；显式传 null 表示改用默认策略"
    )


class ChannelList(BaseModel):
    items: list[ChannelOut]
