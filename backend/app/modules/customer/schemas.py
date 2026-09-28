from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CustomerOut(BaseModel):
    id: UUID
    display_name: str
    owner_id: UUID | None
    owner_display_name: str | None
    source_channel: str
    created_at: datetime


class CustomerPage(BaseModel):
    items: list[CustomerOut]
    total: int


class CustomerCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    owner_id: UUID | None = Field(
        default=None, description="归属坐席；不填时归属创建者。指定他人需要 customer:assign 权限"
    )
