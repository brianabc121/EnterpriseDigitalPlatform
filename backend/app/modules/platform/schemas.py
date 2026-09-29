from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.modules.billing.schemas import PLAN_CODE_PATTERN


class TenantPolicy(BaseModel):
    """租户生命周期策略（平台设置 tenant_policy）。"""

    signup_enabled: bool = Field(default=True, description="允许企业自助注册试用")
    signup_plan_code: str = Field(
        default="trial", pattern=PLAN_CODE_PATTERN, description="自助注册使用的套餐"
    )
    grace_days: int = Field(default=7, ge=0, le=90, description="订阅到期后多少天停用租户")
    retention_days: int = Field(
        default=30, ge=0, le=365, description="申请注销后保留多少天再删除数据"
    )
    export_ttl_days: int = Field(default=7, ge=1, le=30, description="数据导出文件保留天数")


# ---- 内容安全 ----

Word = Annotated[str, Field(min_length=1, max_length=32)]


class ContentPolicy(BaseModel):
    """全局敏感词与内容安全策略（平台设置 content_policy），在各租户自己的敏感词之外生效。"""

    words: list[Word] = Field(default_factory=list, max_length=5000, description="平台敏感词")
    apply_to_ai: bool = Field(
        default=True, description="客户提到时 AI 转人工；AI 回复中出现时不发送"
    )
    block_agent_messages: bool = Field(default=True, description="坐席消息中出现时拒绝发送")


# ---- 大模型供应商 ----


class LlmPrices(BaseModel):
    input: float = Field(default=0, ge=0, le=100_000, description="输入每千 tokens 的价格（分）")
    output: float = Field(default=0, ge=0, le=100_000, description="输出每千 tokens 的价格（分）")


BaseUrl = Annotated[str, Field(min_length=8, max_length=500, pattern=r"^https?://\S+$")]


class LlmProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    base_url: BaseUrl = Field(examples=["https://api.deepseek.com/v1"])
    api_key: str = Field(default="", max_length=500)
    chat_model: str = Field(min_length=1, max_length=128)
    fast_model: str = Field(default="", max_length=128)
    embed_model: str = Field(default="", max_length=128)
    embed_dim: int = Field(default=1024, ge=1, le=8192)
    send_dimensions: bool = False
    prices: LlmPrices = Field(default_factory=LlmPrices)
    is_default: bool = False
    enabled: bool = True


class LlmProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: BaseUrl | None = None
    api_key: str | None = Field(default=None, max_length=500, description="不传表示不修改")
    chat_model: str | None = Field(default=None, min_length=1, max_length=128)
    fast_model: str | None = Field(default=None, max_length=128)
    embed_model: str | None = Field(default=None, max_length=128)
    embed_dim: int | None = Field(default=None, ge=1, le=8192)
    send_dimensions: bool | None = None
    prices: LlmPrices | None = None
    is_default: bool | None = None
    enabled: bool | None = None


class LlmProviderOut(BaseModel):
    id: UUID
    name: str
    base_url: str
    api_key_set: bool
    api_key_hint: str | None = Field(description="密钥末 4 位")
    chat_model: str
    fast_model: str
    embed_model: str
    embed_dim: int
    send_dimensions: bool
    prices: LlmPrices
    is_default: bool
    enabled: bool
    tenants: int = Field(description="指定使用这个供应商的租户数")
    created_at: datetime
    updated_at: datetime


class LlmProviderList(BaseModel):
    items: list[LlmProviderOut]


class LlmCheck(BaseModel):
    ok: bool
    latency_ms: int | None = None
    model: str | None = None
    error: str | None = None


class LlmTestResult(BaseModel):
    chat: LlmCheck
    embed: LlmCheck | None = Field(description="配置了向量模型时检查")


class LlmRoutesOut(BaseModel):
    routes: dict[str, UUID] = Field(description="场景 → 供应商；没有列出的场景用默认供应商")
    scenes: dict[str, str] = Field(description="场景 → 名称")


class LlmRoutesUpdate(BaseModel):
    routes: dict[str, UUID] = Field(default_factory=dict)


class TenantLlmAssign(BaseModel):
    provider_id: UUID | None = Field(description="为空表示使用平台默认供应商")


class TenantLlmOut(BaseModel):
    provider_id: UUID | None
    source: str = Field(description="tenant 自带密钥、provider 指定供应商、default 默认、env、none")
    provider_name: str | None


# ---- 系统健康 ----


class ComponentHealth(BaseModel):
    key: str
    name: str
    status: Literal["ok", "degraded", "down", "disabled"]
    detail: str | None = None
    latency_ms: int | None = None


class HealthReport(BaseModel):
    status: Literal["ok", "degraded", "down"]
    checked_at: datetime
    components: list[ComponentHealth]
    metrics: dict[str, int | float] = Field(description="近期的业务指标")


# ---- 审计 ----


class PlatformAuditOut(BaseModel):
    id: UUID
    tenant_id: UUID | None
    tenant_code: str | None
    actor_type: str
    actor_id: UUID | None
    actor_name: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    detail: dict[str, Any]
    ip: str | None
    created_at: datetime


class PlatformAuditList(BaseModel):
    items: list[PlatformAuditOut]
    next_before: datetime | None = Field(description="下一页：把它作为 before 参数")


# ---- 渠道授权状态 ----


class WecomBinding(BaseModel):
    corp_id: str
    corp_name: str
    status: str
    authorized_at: datetime
    cancelled_at: datetime | None
    kf_accounts: int
    members: int
    last_sync_at: datetime | None
    sync_errors: list[str]


class TenantChannels(BaseModel):
    tenant_id: UUID
    code: str
    name: str
    status: str
    channels: dict[str, int] = Field(description="渠道类型 → 启用的渠道数")
    disabled_channels: int
    wecom: WecomBinding | None


class ChannelOverview(BaseModel):
    wecom_configured: bool = Field(description="平台是否配置了企业微信服务商")
    items: list[TenantChannels]
