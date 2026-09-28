from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

USERNAME_PATTERN = r"^[A-Za-z0-9_.-]{3,64}$"


class LoginRequest(BaseModel):
    tenant_code: str = Field(min_length=3, max_length=32, examples=["demo"])
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Access Token 有效期（秒）")


class TenantBrief(BaseModel):
    id: UUID
    code: str
    name: str


class MeResponse(BaseModel):
    id: UUID
    username: str
    display_name: str
    tenant: TenantBrief
    roles: list[str]
    permissions: list[str]


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    permissions: list[str]
    is_system: bool


class RoleList(BaseModel):
    items: list[RoleOut]


class StaffOut(BaseModel):
    id: UUID
    username: str
    display_name: str
    status: str
    roles: list[str]
    created_at: datetime


class StaffList(BaseModel):
    items: list[StaffOut]


class StaffCreate(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    role_codes: list[str] = Field(min_length=1)
