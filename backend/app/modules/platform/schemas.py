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


class LlmCapabilities(BaseModel):
    """模型的能力标签（设计文档 §11.5）：网关按场景需要选择可用的能力。"""

    tools: bool = Field(default=False, description="支持函数调用（tools），AI 接待可以调用工具")
    json_schema: bool = Field(default=False, description="支持 JSON Schema 结构化输出")
    context_tokens: int = Field(default=0, ge=0, le=10_000_000, description="上下文长度，0 为未知")
    batch: bool = Field(default=False, description="提供批量接口（离线知识提炼可以使用）")


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
    rerank_model: str = Field(default="", max_length=128, description="重排序模型（/rerank）")
    prices: LlmPrices = Field(default_factory=LlmPrices)
    capabilities: LlmCapabilities = Field(default_factory=LlmCapabilities)
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
    rerank_model: str | None = Field(default=None, max_length=128)
    prices: LlmPrices | None = None
    capabilities: LlmCapabilities | None = None
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
    rerank_model: str
    prices: LlmPrices
    capabilities: LlmCapabilities
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
    concurrency: int | None = Field(
        default=None,
        ge=1,
        le=200,
        description="同时进行的大模型调用上限；为空表示平台默认值。不传表示不修改",
    )


class TenantLlmOut(BaseModel):
    provider_id: UUID | None
    source: str = Field(description="tenant 自带密钥、provider 指定供应商、default 默认、env、none")
    provider_name: str | None
    concurrency: int | None = Field(default=None, description="单独设置的并发上限")
    default_concurrency: int = Field(description="平台默认的并发上限")
    in_use: int = Field(default=0, description="正在进行的调用数")


class LlmUsageRow(BaseModel):
    key: str = Field(description="供应商/模型，或租户代码")
    label: str
    calls: int
    errors: int
    tokens: int
    cost: float = Field(description="估算费用（分）")


class LlmUsage(BaseModel):
    days: int
    total_calls: int
    total_tokens: int
    total_cost: float
    by_model: list[LlmUsageRow]
    by_tenant: list[LlmUsageRow]
    by_scene: list[LlmUsageRow]


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


# ---- 提示词版本 ----


class PromptVersionOut(BaseModel):
    version: int
    content: str
    note: str | None
    active: bool
    created_at: datetime


class PromptOut(BaseModel):
    key: str
    name: str
    variables: list[str] = Field(description="模板里可以使用的变量，写成 {name}")
    builtin: str = Field(description="内置模板")
    active_version: int | None = Field(description="启用的版本；为空表示使用内置模板")
    versions: list[PromptVersionOut]


class PromptList(BaseModel):
    items: list[PromptOut]


class PromptVersionCreate(BaseModel):
    content: str = Field(min_length=10, max_length=8000)
    note: str | None = Field(default=None, max_length=200, description="这个版本改了什么")
    activate: bool = Field(default=False, description="保存后立即启用")


class PromptActivate(BaseModel):
    version: int | None = Field(description="要启用的版本；为空表示改回内置模板")


# ---- 运维：死信与 IM 发件箱 ----

DeadLetterId = Annotated[str, Field(pattern=r"^\d{1,20}-\d{1,20}$", examples=["1727650000000-0"])]


class DeadLetterOut(BaseModel):
    id: DeadLetterId = Field(description="死信流里的条目 ID")
    failed_at: datetime
    type: str
    tenant_id: UUID | None = None
    tenant_code: str | None = None
    key: str = Field(description="分区键（通常是 Room ID）")
    data: dict[str, Any]
    error: str


class DeadLetterList(BaseModel):
    items: list[DeadLetterOut]
    total: int = Field(description="死信流里的事件总数")
    next_before: str | None = Field(default=None, description="翻页：下一页传 before")


class DeadLetterAction(BaseModel):
    ids: list[DeadLetterId] = Field(min_length=1, max_length=500)


class OpsResult(BaseModel):
    done: int
    skipped: int = Field(description="已经不存在或不能处理的条目")


class ImOpOut(BaseModel):
    id: int
    tenant_id: UUID
    tenant_code: str
    room_id: UUID
    op: str
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error: str | None
    created_at: datetime
    done_at: datetime | None
    detail: dict[str, Any] = Field(description="操作参数（不含消息正文）")
    retryable: bool = Field(description="在线信令过期就没有意义，不能重试")


class ImOpCounts(BaseModel):
    pending: int
    stuck: int = Field(description="到期 5 分钟以上仍未执行成功")
    failed: int = Field(description="最终失败（保留 7 天）")


class ImOpList(BaseModel):
    items: list[ImOpOut]
    counts: ImOpCounts
    next_before_id: int | None = Field(default=None, description="翻页：下一页传 before_id")


class ImOpAction(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
