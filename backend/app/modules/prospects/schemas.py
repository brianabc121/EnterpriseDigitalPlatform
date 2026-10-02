import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.modules.prospects.settings import ProspectSettings

ProspectStatusValue = Literal["suggested", "active", "won", "lost", "dismissed"]
ProspectLevelValue = Literal["high", "medium", "low"]
ProspectSourceValue = Literal["ai", "staff"]
FollowMethodValue = Literal["phone", "wechat", "chat", "visit", "other"]
# 列表的页签：跟进中、今天该跟进、已逾期、待确认（AI 建议）、已成交、已放弃、全部。
ProspectView = Literal["active", "today", "overdue", "suggested", "won", "lost", "all"]


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


class ProspectFollowupOut(BaseModel):
    id: uuid.UUID
    method: FollowMethodValue
    content: str
    next_follow_at: date | None
    staff_id: uuid.UUID | None
    staff_name: str | None = Field(description="记录人；为空时是系统记的")
    session_id: uuid.UUID | None
    created_at: datetime


class ProspectSummary(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str
    customer_company: str | None
    status: ProspectStatusValue
    level: ProspectLevelValue
    interest: str | None
    concerns: str | None
    source: ProspectSourceValue
    session_id: uuid.UUID | None
    follower_id: uuid.UUID | None
    follower_name: str | None
    next_follow_at: date | None
    last_followed_at: datetime | None
    follow_count: int
    order_id: uuid.UUID | None
    order_no: str | None
    lost_reason: str | None
    created_by_name: str | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    overdue: bool = Field(description="跟进中、下次跟进日期已过")
    due_today: bool = Field(description="跟进中、今天该跟进")


class ProspectOut(ProspectSummary):
    followups: list[ProspectFollowupOut]
    can_assign: bool = Field(description="可以把跟进人改成别人")


class ProspectPage(BaseModel):
    items: list[ProspectSummary]
    total: int
    counts: dict[str, int] = Field(description="各页签的数量")
    won_this_month: int = Field(description="本月成交（企业时区）")


class ProspectCreate(BaseModel):
    customer_id: uuid.UUID
    level: ProspectLevelValue = "medium"
    interest: str | None = Field(default=None, max_length=1000)
    concerns: str | None = Field(default=None, max_length=1000)
    next_follow_at: date | None = Field(default=None, description="不填时按设置的默认天数")
    follower_id: uuid.UUID | None = Field(
        default=None, description="跟进人；不填时是客户的归属坐席（没有时是自己）"
    )

    @field_validator("interest", "concerns")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _text(value)


class ProspectUpdate(BaseModel):
    level: ProspectLevelValue | None = None
    interest: str | None = Field(default=None, max_length=1000)
    concerns: str | None = Field(default=None, max_length=1000)
    next_follow_at: date | None = None
    follower_id: uuid.UUID | None = None

    @field_validator("interest", "concerns")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _text(value)


class FollowupCreate(BaseModel):
    method: FollowMethodValue = "phone"
    content: str = Field(min_length=1, max_length=2000)
    next_follow_at: date | None = Field(default=None, description="不填时按设置的默认天数")

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请填写跟进的内容")
        return value


class ProspectWon(BaseModel):
    order_id: uuid.UUID | None = Field(default=None, description="成交的订单（可以不填）")


class ProspectLost(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请填写放弃的原因")
        return value


class ProspectReopen(BaseModel):
    next_follow_at: date | None = None


class ProspectMessage(BaseModel):
    """AI 写的跟进话术：员工修改后自己发送。"""

    text: str
    knowledge: list[str] = Field(default_factory=list, description="参考的知识标题")


class ProspectSettingsOut(BaseModel):
    settings: ProspectSettings
    can_edit: bool


class ProspectBrief(BaseModel):
    """客户资料里显示的意向（最近的一条，待确认、跟进中的优先）。"""

    id: uuid.UUID
    status: ProspectStatusValue
    level: ProspectLevelValue
    next_follow_at: date | None
    follower_name: str | None
    overdue: bool


class CustomerProspectInfo(BaseModel):
    """客户资料里的意向客户。"""

    prospect: ProspectBrief | None = Field(description="最近的意向记录；没有时为空")
    recent_deal_at: datetime | None = Field(
        description="最近 30 天确认的订单的确认时间（转入时提示：老客户有新需求也可以转入）"
    )
