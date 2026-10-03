"""商机的接口模型（设计文档 §40.13）。"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.modules.opportunities.settings import OpportunitySettings

OpportunityStatusValue = Literal["suggested", "active", "won", "lost", "dismissed"]
OpportunityLevelValue = Literal["high", "medium", "low"]
OpportunitySourceValue = Literal["ai", "staff", "api"]
FollowMethodValue = Literal["phone", "wechat", "chat", "visit", "other"]
StageKindValue = Literal["open", "won", "lost"]
ActivityKindValue = Literal[
    "followup",
    "note",
    "created",
    "stage",
    "owner",
    "field",
    "session",
    "todo",
    "order",
    "contract",
    "payment",
    "ai",
]
# 快捷视图：跟进中、我负责的、今天该跟进、本周要跟进、已逾期、本月预计成交、停滞、待确认
# （AI 建议）、赢单、输单、全部。
OpportunityView = Literal[
    "active",
    "mine",
    "today",
    "week",
    "overdue",
    "closing",
    "stale",
    "suggested",
    "won",
    "lost",
    "all",
]
Money = Decimal


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


class ProductRef(BaseModel):
    """关联的商品。"""

    product_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=100)
    quantity: int = Field(default=1, ge=1, le=100000)


class StageOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    position: int
    kind: StageKindValue
    probability: int
    stale_days: int | None
    color: str | None


class StageCreate(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    probability: int = Field(default=50, ge=0, le=100)
    stale_days: int | None = Field(default=7, ge=1, le=365)
    color: str | None = Field(default=None, max_length=16)
    after_id: uuid.UUID | None = Field(
        default=None, description="放在这个阶段后面；不填时放在最后一个进行中的阶段后面"
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请填写阶段的名称")
        return value


class StageUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=32)
    probability: int | None = Field(default=None, ge=0, le=100)
    stale_days: int | None = Field(default=None, ge=1, le=365)
    clear_stale_days: bool = Field(default=False, description="不算停滞")
    color: str | None = Field(default=None, max_length=16)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return _text(value)


class StageOrder(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, description="进行中的阶段的先后")


class OpportunityActivityOut(BaseModel):
    id: uuid.UUID
    kind: ActivityKindValue
    title: str | None
    method: FollowMethodValue
    content: str | None
    next_follow_at: date | None
    properties: dict[str, Any] = Field(default_factory=dict)
    linked_type: str | None
    linked_id: uuid.UUID | None
    staff_id: uuid.UUID | None
    staff_name: str | None = Field(description="记录人；为空时是系统记的")
    session_id: uuid.UUID | None
    created_at: datetime


class OpportunitySummary(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    customer_name: str
    customer_company: str | None
    name: str
    status: OpportunityStatusValue
    stage_id: uuid.UUID
    stage_code: str
    stage_name: str
    stage_kind: StageKindValue
    level: OpportunityLevelValue
    interest: str | None
    concerns: str | None
    source: OpportunitySourceValue
    session_id: uuid.UUID | None
    owner_id: uuid.UUID | None
    owner_name: str | None
    next_follow_at: date | None
    last_followed_at: datetime | None
    follow_count: int
    amount: Money | None = Field(description="预计金额；设置为只有管理者可见而自己不能看时为空")
    expected_close_at: date | None
    probability: int = Field(description="成交概率（%）：商机自己的，没有时是阶段的")
    stage_entered_at: datetime
    days_in_stage: int
    stale: bool = Field(description="跟进中、在当前阶段超过阶段的停滞天数没有动态")
    last_activity_at: datetime | None
    products: list[ProductRef] = Field(default_factory=list)
    order_id: uuid.UUID | None
    order_no: str | None
    contract_id: uuid.UUID | None
    contract_no: str | None
    lost_reason_code: str | None
    lost_reason_name: str | None
    lost_reason: str | None
    created_by_name: str | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    overdue: bool = Field(description="跟进中、下次跟进日期已过")
    due_today: bool = Field(description="跟进中、今天该跟进")


class TodoBrief(BaseModel):
    """商机上没完成的待办（安排的下一步）。"""

    id: uuid.UUID
    no: str
    title: str
    type_name: str
    status: str
    due_at: datetime | None
    assignee_name: str | None


class OpportunityOut(OpportunitySummary):
    activities: list[OpportunityActivityOut]
    todos: list[TodoBrief] = Field(default_factory=list, description="没完成的待办")
    can_manage: bool = Field(description="可以修改、跟进、换阶段")
    can_assign: bool = Field(description="可以把负责人改成别人")
    amount_visible: bool


class OpportunityPage(BaseModel):
    items: list[OpportunitySummary]
    total: int
    counts: dict[str, int] = Field(description="各快捷视图的数量")
    won_this_month: int = Field(description="本月赢单（企业时区）")
    amount_visible: bool


class BoardColumn(BaseModel):
    stage: StageOut
    total: int
    amount_sum: Money | None = Field(description="这一列的预计金额合计")
    items: list[OpportunitySummary]
    truncated: bool = Field(description="这一列还有没显示的")


class OpportunityBoard(BaseModel):
    columns: list[BoardColumn]
    amount_visible: bool


class OpportunityStats(BaseModel):
    """顶部数字。"""

    active: int
    mine: int
    today: int
    week: int = Field(description="本周（到周日）要跟进的，包括今天")
    overdue: int
    stale: int
    suggested: int
    won_this_month: int
    won_amount_this_month: Money | None


class OpportunityCreate(BaseModel):
    customer_id: uuid.UUID
    name: str | None = Field(default=None, max_length=128, description="不填时按想要什么或客户称呼")
    stage_id: uuid.UUID | None = Field(default=None, description="不填时是第一个进行中的阶段")
    level: OpportunityLevelValue = "medium"
    interest: str | None = Field(default=None, max_length=1000)
    concerns: str | None = Field(default=None, max_length=1000)
    amount: Money | None = Field(default=None, ge=0, le=Decimal("999999999999.99"))
    expected_close_at: date | None = None
    next_follow_at: date | None = Field(default=None, description="不填时按设置的默认天数")
    owner_id: uuid.UUID | None = Field(
        default=None, description="负责人；不填时是客户的归属坐席（没有时是自己）"
    )
    products: list[ProductRef] = Field(default_factory=list, max_length=20)

    @field_validator("name", "interest", "concerns")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _text(value)


class OpportunityUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    level: OpportunityLevelValue | None = None
    interest: str | None = Field(default=None, max_length=1000)
    concerns: str | None = Field(default=None, max_length=1000)
    amount: Money | None = Field(default=None, ge=0, le=Decimal("999999999999.99"))
    expected_close_at: date | None = None
    probability: int | None = Field(default=None, ge=0, le=100)
    next_follow_at: date | None = None
    owner_id: uuid.UUID | None = None
    products: list[ProductRef] | None = Field(default=None, max_length=20)

    @field_validator("name", "interest", "concerns")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return _text(value)


class FollowupCreate(BaseModel):
    """记一次跟进或备注。"""

    kind: Literal["followup", "note"] = "followup"
    method: FollowMethodValue = "phone"
    content: str = Field(min_length=1, max_length=2000)
    next_follow_at: date | None = Field(
        default=None, description="不填时按设置的默认天数（备注不改）"
    )

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请填写内容")
        return value


class StageMove(BaseModel):
    """换阶段（看板拖拽）：拖到赢单可以带订单或合同，拖到输单要选原因。"""

    stage_id: uuid.UUID
    position: int | None = Field(default=None, ge=0, description="在这一列里的位置")
    order_id: uuid.UUID | None = None
    contract_id: uuid.UUID | None = None
    lost_reason_code: str | None = Field(default=None, max_length=16)
    lost_reason: str | None = Field(default=None, max_length=500)


class OpportunityWon(BaseModel):
    order_id: uuid.UUID | None = Field(default=None, description="成交的订单（可以不填）")
    contract_id: uuid.UUID | None = Field(default=None, description="成交的合同（可以不填）")
    note: str | None = Field(default=None, max_length=500)


class OpportunityLost(BaseModel):
    reason_code: str = Field(
        min_length=1, max_length=16, description="输单原因分类（商机设置里的）"
    )
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        return _text(value)


class OpportunityReopen(BaseModel):
    next_follow_at: date | None = None


class OpportunityAssign(BaseModel):
    owner_id: uuid.UUID | None = Field(description="负责人；为空时没有负责人")


class OpportunityMessage(BaseModel):
    """AI 写的跟进话术：员工修改后自己发送。"""

    text: str
    knowledge: list[str] = Field(default_factory=list, description="参考的知识标题")


class OpportunityDigest(BaseModel):
    """AI 小结（§40.7）：现在到哪一步、客户在意什么、建议下一步；同时记进时间线。"""

    status: str = Field(description="现在到哪一步")
    cares: str = Field(description="客户在意什么")
    next: str = Field(description="建议下一步")
    text: str = Field(description="三句话连起来")
    generated_at: datetime


class NextStep(BaseModel):
    """安排下一步（§40.7）：建一条关联这条商机的待办。"""

    type_code: str = Field(
        default="callback",
        min_length=1,
        max_length=32,
        description="待办类型的代码，默认回电 / 回访",
    )
    title: str | None = Field(default=None, max_length=100, description="不填时按类型和商机名称")
    detail: str = Field(default="", max_length=4000)
    due_at: datetime | None = Field(default=None, description="截止时间；不填时按类型的时限")
    assignee_id: uuid.UUID | None = Field(default=None, description="处理人；不填时是负责人")
    next_follow_at: date | None = Field(default=None, description="同时改商机的下次跟进日期")

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        return _text(value)


class OpportunitySettingsOut(BaseModel):
    settings: OpportunitySettings
    stages: list[StageOut]
    can_edit: bool


class OpportunityBrief(BaseModel):
    """客户资料里显示的商机（最近的一条，待确认、跟进中的优先）。"""

    id: uuid.UUID
    name: str
    status: OpportunityStatusValue
    stage_name: str
    stage_kind: StageKindValue
    level: OpportunityLevelValue
    amount: Money | None
    next_follow_at: date | None
    owner_name: str | None
    overdue: bool


class CustomerOpportunityInfo(BaseModel):
    """客户资料里的商机。"""

    opportunity: OpportunityBrief | None = Field(description="最近的商机；没有时为空")
    recent_deal_at: datetime | None = Field(
        description="最近 30 天确认的订单的确认时间（转入时提示：老客户有新需求也可以转入）"
    )
