from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.iam.schemas import USERNAME_PATTERN
from app.modules.tenancy.models import TenantStatus

TENANT_CODE_PATTERN = r"^[a-z][a-z0-9-]{2,31}$"


class PlatformLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class PlatformTokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class PlatformMe(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str


class TenantAdminCreate(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class TenantCreate(BaseModel):
    code: str = Field(pattern=TENANT_CODE_PATTERN, description="企业代码，员工登录时填写")
    name: str = Field(min_length=1, max_length=128)
    admin: TenantAdminCreate


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    status: TenantStatus | None = None


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    status: str
    created_at: datetime


class TenantList(BaseModel):
    items: list[TenantOut]
