from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.modules.billing.entitlements import FeatureKey, LimitKey

PLAN_CODE_PATTERN = r"^[a-z][a-z0-9_-]{1,31}$"
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


class PlanLimits(BaseModel):
    """额度；为空表示不限。"""

    seats: int | None = Field(default=None, ge=0, le=100_000, description="启用的员工账号数")
    ai_replies_monthly: int | None = Field(
        default=None, ge=0, le=100_000_000, description="每月 AI 回复条数"
    )
    kb_items: int | None = Field(default=None, ge=0, le=10_000_000, description="知识条目数")
    channels: int | None = Field(default=None, ge=0, le=10_000, description="启用的接入渠道数")


class PlanFeatures(BaseModel):
    ai: bool = True
    wecom: bool = True
    broadcast: bool = True
    extraction: bool = True
    zone: bool = False


class PlanOverage(BaseModel):
    policy: Literal["degrade", "warn"] = Field(
        default="degrade",
        description="AI 回复超出额度后：degrade 停止 AI 接待（转人工）；warn 继续回复并按条计费",
    )
    ai_reply_price: int = Field(
        default=0, ge=0, le=100_000, description="超出额度后每条 AI 回复的价格（分）"
    )


class PlanOut(BaseModel):
    id: UUID
    code: str
    name: str
    description: str | None
    price_monthly: int = Field(description="月费（分）")
    limits: PlanLimits
    features: PlanFeatures
    overage: PlanOverage
    trial_days: int
    public: bool
    status: str
    sort: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, plan: Any) -> "PlanOut":
        return cls(
            id=plan.id,
            code=plan.code,
            name=plan.name,
            description=plan.description,
            price_monthly=plan.price_monthly,
            limits=PlanLimits.model_validate(
                {k: v for k, v in (plan.limits or {}).items() if k in PlanLimits.model_fields}
            ),
            features=PlanFeatures.model_validate(
                {k: v for k, v in (plan.features or {}).items() if k in PlanFeatures.model_fields}
            ),
            overage=PlanOverage.model_validate(
                {k: v for k, v in (plan.overage or {}).items() if k in PlanOverage.model_fields}
            ),
            trial_days=plan.trial_days,
            public=plan.public,
            status=plan.status,
            sort=plan.sort,
            created_at=plan.created_at,
            updated_at=plan.updated_at,
        )


class PlanList(BaseModel):
    items: list[PlanOut]


class PlanCreate(BaseModel):
    code: str = Field(pattern=PLAN_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    price_monthly: int = Field(default=0, ge=0, le=100_000_000, description="月费（分）")
    limits: PlanLimits = Field(default_factory=PlanLimits)
    features: PlanFeatures = Field(default_factory=PlanFeatures)
    overage: PlanOverage = Field(default_factory=PlanOverage)
    trial_days: int = Field(default=0, ge=0, le=365, description="大于 0 时开通即试用这么多天")
    public: bool = Field(default=False, description="在租户的套餐页和自助注册页展示")
    sort: int = Field(default=0, ge=-1000, le=1000)


class PlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    price_monthly: int | None = Field(default=None, ge=0, le=100_000_000)
    limits: PlanLimits | None = None
    features: PlanFeatures | None = None
    overage: PlanOverage | None = None
    trial_days: int | None = Field(default=None, ge=0, le=365)
    public: bool | None = None
    sort: int | None = Field(default=None, ge=-1000, le=1000)
    status: Literal["active", "archived"] | None = None


class SubscriptionOut(BaseModel):
    id: UUID
    plan_id: UUID
    plan_code: str
    plan_name: str
    status: str
    period_start: date
    period_end: date
    note: str | None
    created_at: datetime


class SubscriptionList(BaseModel):
    items: list[SubscriptionOut]


class SubscriptionCreate(BaseModel):
    """开始一个新订阅（试用转正式、升级、降级都用它）：当前订阅随之取消。"""

    plan_code: str = Field(pattern=PLAN_CODE_PATTERN)
    status: Literal["trial", "active"] = "active"
    period_start: date | None = Field(default=None, description="默认今天")
    months: int | None = Field(default=None, ge=1, le=60, description="订阅月数")
    period_end: date | None = Field(default=None, description="到期日（含）；与 months 二选一")
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _one_period(self) -> "SubscriptionCreate":
        if self.months is not None and self.period_end is not None:
            raise ValueError("months 与 period_end 只能填一个")
        return self


class SubscriptionRenew(BaseModel):
    months: int | None = Field(default=None, ge=1, le=60, description="从原到期日起续订的月数")
    period_end: date | None = Field(default=None, description="新的到期日（含）")
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _one_period(self) -> "SubscriptionRenew":
        if (self.months is None) == (self.period_end is None):
            raise ValueError("请填写 months 或 period_end 其中一个")
        return self


class LimitUsage(BaseModel):
    key: str
    label: str
    unit: str
    limit: int | None = Field(description="上限；为空表示不限")
    used: int
    overridden: bool = Field(description="平台为本租户单独设置了这项额度")


class FeatureOut(BaseModel):
    key: str
    label: str
    enabled: bool
    overridden: bool


class BillingOverview(BaseModel):
    metered: bool = Field(description="是否按套餐计费；启用计费前开通的租户不限额")
    plan: PlanOut | None
    subscription: SubscriptionOut | None
    days_left: int | None = Field(description="当前订阅剩余天数（含今天）；已到期为 0")
    notice: str | None = Field(description="试用、即将到期或已到期的提醒")
    limits: list[LimitUsage]
    features: list[FeatureOut]
    overage_policy: str
    ai_reply_price: int


class TenantOverrides(BaseModel):
    """按租户覆盖套餐。limits 中值为 null 表示不限；没有列出的项按套餐。"""

    limits: dict[LimitKey, int | None] = Field(default_factory=dict)
    features: dict[FeatureKey, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ranges(self) -> "TenantOverrides":
        for key, value in self.limits.items():
            if value is not None and not 0 <= value <= 100_000_000:
                raise ValueError(f"{key} 超出范围")
        return self


class InvoiceItem(BaseModel):
    kind: Literal["plan", "ai_overage"]
    description: str
    quantity: int
    unit: str
    unit_price: int | None = Field(default=None, description="单价（分）")
    amount: int = Field(description="金额（分）")


class InvoiceOut(BaseModel):
    id: UUID
    tenant_id: UUID
    tenant_code: str | None = None
    tenant_name: str | None = None
    number: str
    period_start: date
    period_end: date
    plan_name: str
    items: list[InvoiceItem]
    amount: int = Field(description="金额（分）")
    status: str
    issued_at: datetime
    paid_at: datetime | None
    note: str | None


class InvoiceList(BaseModel):
    items: list[InvoiceOut]


class InvoiceUpdate(BaseModel):
    status: Literal["issued", "paid", "void"] | None = None
    note: str | None = Field(default=None, max_length=500)


class InvoiceGenerate(BaseModel):
    month: str = Field(pattern=MONTH_PATTERN, examples=["2026-09"], description="账单月份")


class InvoiceGenerateResult(BaseModel):
    created: int
    updated: int
    unchanged: int


class TenantBilling(BillingOverview):
    """平台运营看到的租户套餐：另有订阅历史、账单和覆盖值。"""

    subscriptions: list[SubscriptionOut]
    invoices: list[InvoiceOut]
    overrides: TenantOverrides


class BillingSettings(BaseModel):
    grace_days: int = Field(default=7, ge=0, le=90, description="订阅到期后多少天停用租户")
