from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class QuickReplyOut(BaseModel):
    id: UUID
    shared: bool = Field(description="全员共享（true）或个人话术（false）")
    category: str
    title: str
    content: str
    sort: int
    updated_at: datetime


class QuickReplyList(BaseModel):
    items: list[QuickReplyOut]


class QuickReplyCreate(BaseModel):
    shared: bool = Field(default=False, description="全员共享需要 quick_reply:manage 权限")
    category: str = Field(default="", max_length=32)
    title: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=2000)
    sort: int = Field(default=0, ge=0, le=10000)


class QuickReplyUpdate(BaseModel):
    category: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, min_length=1, max_length=64)
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    sort: int | None = Field(default=None, ge=0, le=10000)
