from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: str
    name: str
    public_key: str
    status: str
    created_at: datetime


class ChannelList(BaseModel):
    items: list[ChannelOut]
