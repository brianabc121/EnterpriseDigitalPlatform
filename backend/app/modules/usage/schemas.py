from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class UsageMetricOut(BaseModel):
    key: str
    label: str
    unit: str
    kind: Literal["sum", "max", "snapshot"] = Field(
        description="合计方式：sum 按日求和；max 取最大值（活跃坐席）；snapshot 取最后一天的值"
    )


class UsageDayOut(BaseModel):
    day: date
    values: dict[str, int] = Field(description="指标 → 数值；没有列出的指标为 0")


class UsageReport(BaseModel):
    start: date
    end: date
    timezone: str = Field(description="日期按这个时区划分")
    metrics: list[UsageMetricOut]
    days: list[UsageDayOut]
    totals: dict[str, int]
    updated_at: datetime | None = Field(description="最近一次汇总的时间；每 10 分钟汇总一次")


class TenantUsageOut(BaseModel):
    tenant_id: UUID
    code: str
    name: str
    status: str
    totals: dict[str, int]


class TenantUsageList(BaseModel):
    start: date
    end: date
    timezone: str
    metrics: list[UsageMetricOut]
    items: list[TenantUsageOut]
