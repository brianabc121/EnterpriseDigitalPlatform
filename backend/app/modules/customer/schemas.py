from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints


class CustomerOut(BaseModel):
    id: UUID
    display_name: str
    owner_id: UUID | None
    owner_display_name: str | None
    source_channel: str
    tags: list[str]
    created_at: datetime


class CustomerIdentityOut(BaseModel):
    id: UUID
    channel_account_id: UUID
    channel_type: str
    channel_name: str
    verified: bool
    profile: dict[str, Any] = Field(description="渠道提供的资料，例如访客的来源页面和浏览器")
    last_seen_at: datetime | None
    created_at: datetime


class CustomerDetail(CustomerOut):
    notes: str | None
    identities: list[CustomerIdentityOut]


class CustomerUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    tags: (
        list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]]
        | None
    ) = Field(default=None, max_length=20)


class CustomerPage(BaseModel):
    items: list[CustomerOut]
    total: int


class CustomerCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    owner_id: UUID | None = Field(
        default=None, description="归属坐席；不填时归属创建者。指定他人需要 customer:assign 权限"
    )
