"""AI 唤醒的接口数据（设计文档 §33.10）。"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.modules.wake.settings import WakeSettings

SeverityValue = Literal["info", "warning", "critical"]
FindingStatusValue = Literal["open", "ignored", "resolved"]
RunKindValue = Literal["hourly", "daily", "kb"]
ManualKindValue = Literal["daily", "kb"]
RunTriggerValue = Literal["schedule", "event", "manual", "continue"]
RunStatusValue = Literal["queued", "running", "done", "failed", "skipped"]
FindingView = Literal["mine", "all"]


class Person(BaseModel):
    id: UUID
    name: str


class FindingOut(BaseModel):
    id: UUID
    check_code: str
    check_title: str = Field(description="检查项的名称")
    category: str
    category_label: str
    severity: SeverityValue = Field(description="info 提示、warning 注意、critical 严重")
    status: FindingStatusValue = Field(description="open 待处理、ignored 已忽略、resolved 已消除")
    title: str
    detail: str | None
    link: str | None = Field(description="打开对象的页面（订单、单据、待办……）")
    entity_type: str | None
    entity_id: UUID | None
    data: dict[str, Any] = Field(
        description="检查时的数字；since 是开始等待的时间（页面显示已经多久）"
    )
    assignees: list[Person] = Field(description="负责人")
    first_seen_at: datetime
    last_seen_at: datetime = Field(description="最近一次检查仍然发现的时间")
    seen_count: int
    notified_at: datetime | None
    escalated_at: datetime | None = Field(description="升级给管理员的时间")
    resolved_at: datetime | None
    resolved_by: Person | None = Field(description="标记已处理的人；自动消除时为空")
    resolve_note: str | None
    ignored_by: Person | None
    ignored_until: datetime | None = Field(description="忽略到什么时候；一直忽略时为空")
    ignore_note: str | None
    mine: bool = Field(description="负责人包含自己")
    can_handle: bool = Field(description="可以忽略、标记已处理（负责人或管理员）")


class SeverityCounts(BaseModel):
    critical: int = 0
    warning: int = 0
    info: int = 0
    total: int = 0


class FindingPage(BaseModel):
    items: list[FindingOut]
    total: int
    open: SeverityCounts = Field(description="同一范围（自己负责的或全部）待处理的问题数")


class IgnoreRequest(BaseModel):
    days: int | None = Field(
        default=7, ge=1, le=365, description="几天内不再提醒；为空表示一直忽略（直到问题消除）"
    )
    note: str | None = Field(default=None, max_length=200, description="原因")


class ResolveRequest(BaseModel):
    note: str | None = Field(default=None, max_length=200)


class RunOut(BaseModel):
    id: UUID
    kind: RunKindValue = Field(description="hourly 每小时检查、daily 每日巡检、kb 知识库整理")
    kind_label: str
    trigger: RunTriggerValue = Field(
        description="schedule 定时、event 规章制度变化、manual 立即唤醒、continue 接着上次整理"
    )
    status: RunStatusValue
    not_before: datetime
    started_at: datetime | None
    finished_at: datetime | None
    stats: dict[str, Any] = Field(
        description="巡检：checks 检查项、ran 实际运行、skipped 数据没变而跳过、found 发现、"
        "new 新问题、resolved 已消除、notified 通知人数；整理：整理报告"
    )
    summary: str | None = Field(description="每日巡检的 AI 简报")
    error: str | None
    created_by: Person | None
    created_at: datetime


class RunPage(BaseModel):
    items: list[RunOut]
    total: int


class RunRequest(BaseModel):
    kind: ManualKindValue = Field(description="daily 立即巡检、kb 立即整理知识库")


class CheckParamOut(BaseModel):
    name: str
    label: str
    unit: str
    default: int
    minimum: int
    maximum: int
    value: int = Field(description="现在的值")


class CheckOut(BaseModel):
    code: str
    category: str
    category_label: str
    title: str
    description: str
    hourly: bool = Field(description="每小时检查也做；否则只在每日巡检里做")
    available: bool = Field(description="套餐包含它需要的功能")
    enabled: bool
    params: list[CheckParamOut]
    domains: list[str] = Field(description="检查时读的数据表（增量更新索引里比对它们的变化）")
    checked_at: datetime | None = Field(description="最近一次实际检查的时间")
    changed_at: datetime | None = Field(description="这些表最近一次变化的时间")


class WakeSettingsOut(BaseModel):
    settings: WakeSettings
    checks: list[CheckOut]


class DataChange(BaseModel):
    domain: str = Field(description="数据表")
    label: str
    seq: int = Field(description="最近一次变化的编号（全库递增）")
    changed_at: datetime


class WakeOverview(BaseModel):
    available: bool = Field(description="套餐包含 AI 唤醒（AI 接待与坐席助手）")
    enabled: bool
    next_hourly: datetime | None = Field(description="下一次每小时检查")
    next_daily: datetime | None = Field(description="下一次每日巡检")
    next_kb: datetime | None = Field(description="下一次知识库整理")
    open: SeverityCounts
    brief: RunOut | None = Field(description="最近一次每日巡检（含 AI 简报）")
    latest: RunOut | None = Field(description="最近一次完成的巡检（每小时或每日）")
    kb: RunOut | None = Field(description="最近一次知识库整理")
    pending: list[RunOut] = Field(description="排队中、执行中的唤醒")
    data_index: list[DataChange] = Field(
        description="增量更新索引：最近有变化的数据表（最多 12 张）"
    )
    tracked: int = Field(description="增量更新索引里有记录的数据表数")


class KbAlignment(BaseModel):
    finished_at: datetime | None = Field(description="最近一次整理完成的时间")
    report: dict[str, Any] = Field(description="整理报告（§33.7.2）")
    policies: int = Field(description="现行制度几份")
    pending: int = Field(description="待处理的制度对齐建议")
    running: bool = Field(description="正在排队或整理")
    enabled: bool = Field(description="开启了定期整理")
