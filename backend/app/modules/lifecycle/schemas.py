from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.iam.schemas import USERNAME_PATTERN
from app.modules.tenancy.schemas import TENANT_CODE_PATTERN


class SignupRequest(BaseModel):
    """企业自助注册：开通租户和管理员账号，按平台设置的套餐开始试用。"""

    company_name: str = Field(min_length=2, max_length=128)
    tenant_code: str = Field(pattern=TENANT_CODE_PATTERN, description="企业代码，员工登录时填写")
    admin_username: str = Field(default="admin", pattern=USERNAME_PATTERN)
    admin_display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    contact: str | None = Field(default=None, max_length=64, description="联系电话或邮箱")
    agree: bool = Field(description="同意服务协议与隐私政策")


class SignupResult(BaseModel):
    tenant_code: str
    username: str
    plan_name: str | None
    trial_days: int | None


class SignupOptions(BaseModel):
    enabled: bool
    plan_name: str | None
    trial_days: int | None


class TenantExportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str = Field(description="pending、running、done、failed")
    size: int | None
    tables: dict[str, Any] = Field(description="表名 → 导出的行数")
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    expires_at: datetime | None


class TenantExportList(BaseModel):
    items: list[TenantExportOut]


class ExportDownload(BaseModel):
    url: str = Field(description="短时有效的下载地址")
    expires_in: int


class ClosureStatus(BaseModel):
    closing: bool
    requested_at: datetime | None
    scheduled_at: datetime | None = Field(description="到这个时间删除全部数据")
    retention_days: int


class ClosureRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128, description="当前管理员的登录密码")
    confirm_code: str = Field(min_length=1, max_length=32, description="再次输入企业代码确认")
    reason: str | None = Field(default=None, max_length=500)


class PlatformClosureRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class SupportGrantCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=500, description="需要平台协助的问题")
    hours: int = Field(default=24, ge=1, le=168, description="授权有效时长（小时）")


class SupportGrantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    reason: str
    granted_by: UUID | None
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime
    active: bool = False


class SupportAccessOut(BaseModel):
    """平台运维的一次查看记录。"""

    actor_id: UUID | None
    what: str
    resource_id: str | None
    created_at: datetime


class SupportGrantList(BaseModel):
    items: list[SupportGrantOut]
    accesses: list[SupportAccessOut] = Field(description="最近的平台访问记录")


class SupportStatus(BaseModel):
    active: bool
    grant: SupportGrantOut | None


class SupportSessionOut(BaseModel):
    id: UUID
    status: str
    customer_name: str | None
    channel_name: str | None
    assignee_name: str | None
    created_at: datetime
    closed_at: datetime | None


class SupportSessionList(BaseModel):
    items: list[SupportSessionOut]


class SupportMessageOut(BaseModel):
    id: UUID
    sender_type: str
    content_type: str
    text: str | None
    sent_at: datetime


class SupportMessageList(BaseModel):
    items: list[SupportMessageOut]


class TenantDeletionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    code: str
    name: str
    requested_at: datetime | None
    scheduled_at: datetime | None
    purged_at: datetime
    export_id: UUID | None
    counts: dict[str, Any] = Field(description="各表删除的行数、对象存储删除的文件数与字节数")
    digest: str = Field(description="删除明细的 SHA-256")


class TenantDeletionList(BaseModel):
    items: list[TenantDeletionOut]


class PurgeResult(BaseModel):
    status: Literal["purged"]
    deletion: TenantDeletionOut
