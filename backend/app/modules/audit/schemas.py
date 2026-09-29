from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class AuditOut(BaseModel):
    id: UUID
    actor_type: str = Field(description="staff（员工）、platform（平台运维）、system（系统）等")
    actor_id: UUID | None
    actor_name: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    detail: dict[str, Any]
    ip: str | None
    created_at: datetime


class AuditList(BaseModel):
    items: list[AuditOut]
    next_before: datetime | None = Field(description="下一页：把它作为 before 参数")
