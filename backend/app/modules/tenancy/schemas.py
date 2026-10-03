from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.iam.schemas import USERNAME_PATTERN

TENANT_CODE_PATTERN = r"^[a-z][a-z0-9-]{2,31}$"


class PlatformLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    otp: str | None = Field(
        default=None, max_length=10, description="二次验证码（启用了二次验证的账号必填）"
    )


class PlatformTokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    mfa_setup_required: bool = Field(
        default=False, description="平台要求二次验证而这个账号还没有设置：先完成设置才能使用"
    )


class PlatformMe(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    display_name: str
    mfa_enabled: bool = False
    mfa_required: bool = False


class MfaSetupOut(BaseModel):
    secret: str = Field(description="Base32 密钥，也可以手动输入到验证器应用")
    otpauth_uri: str = Field(description="生成二维码用的 otpauth:// 地址")


class MfaCode(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class MfaDisable(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=6, max_length=10)


class TenantAdminCreate(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class TenantCreate(BaseModel):
    code: str = Field(pattern=TENANT_CODE_PATTERN, description="企业代码，员工登录时填写")
    name: str = Field(min_length=1, max_length=128)
    admin: TenantAdminCreate
    plan_code: str | None = Field(
        default=None,
        max_length=32,
        description="套餐；有试用天数的套餐先试用，否则开通正式订阅。为空时不按套餐计费",
    )
    months: int = Field(default=12, ge=1, le=60, description="正式订阅的月数")


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    status: Literal["active", "suspended"] | None = None
    ai_monthly_quota: int | None = Field(
        default=None,
        ge=0,
        le=10_000_000,
        description="单独给本租户设置的每月 AI 回复条数上限（覆盖套餐）；显式传 null 表示按套餐",
    )


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    status: str
    created_at: datetime
    ai_monthly_quota: int | None = Field(
        default=None, description="单独设置的每月 AI 回复条数上限；为空表示按套餐"
    )
    plan_code: str | None = None
    plan_name: str | None = None
    subscription_status: str | None = None
    period_end: date | None = None
    suspended_reason: str | None = Field(default=None, description="因订阅到期停用时为该原因")
    closing_requested_at: datetime | None = None
    deletion_scheduled_at: datetime | None = None
    purged_at: datetime | None = None

    @classmethod
    def of(cls, tenant: Any, subscription: Any = None, plan: Any = None) -> "TenantOut":
        out = cls.model_validate(tenant)
        settings = tenant.settings or {}
        limits = settings.get("limits") or {}
        out.ai_monthly_quota = limits.get("ai_replies_monthly", settings.get("ai_monthly_quota"))
        out.suspended_reason = settings.get("suspended_reason")
        if subscription is not None:
            out.subscription_status = subscription.status
            out.period_end = subscription.period_end
        if plan is not None:
            out.plan_code = plan.code
            out.plan_name = plan.name
        return out


class TenantList(BaseModel):
    items: list[TenantOut]


class TenantAdminOut(BaseModel):
    """企业的管理员账号（设计文档 §38.2）。"""

    id: UUID
    username: str
    display_name: str
    status: str
    owner: bool = Field(
        description="企业拥有者：开通企业时创建的管理员账号（企业里最早创建的账号）"
    )
    created_at: datetime
    last_login_at: datetime | None = Field(description="最近登录（账号密码或企业微信）")
    password_changed_at: datetime | None = Field(
        description="密码最近修改或重置的时间；为空表示创建以来没有改过"
    )
    must_change_password: bool = Field(description="密码被重置后还没有设置新密码")


class TenantAdminList(BaseModel):
    items: list[TenantAdminOut]


class AdminPasswordReset(BaseModel):
    reason: str = Field(
        min_length=2,
        max_length=200,
        description="重置的原因：记入企业的操作日志，企业的其他管理员会收到提醒",
    )
    password: str | None = Field(
        default=None, min_length=8, max_length=128, description="新密码；不填时自动生成"
    )
