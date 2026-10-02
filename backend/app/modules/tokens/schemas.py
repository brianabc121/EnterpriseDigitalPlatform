import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

TokenStatusFilter = Literal["ok", "failed"]


class TokenAmounts(BaseModel):
    calls: int = Field(description="调用次数")
    failed: int = Field(description="失败的调用（不收费）")
    prompt_tokens: int = Field(description="输入 tokens")
    completion_tokens: int = Field(description="输出 tokens")
    tokens: int = Field(description="输入加输出")
    cost: float = Field(description="费用（分）：按平台给模型设置的价格在调用时算好")


class TokenDay(TokenAmounts):
    day: date


class TokenGroup(TokenAmounts):
    key: str = Field(description="场景、供应商/模型或员工 ID（system 为系统）")
    label: str


class TokenSummary(BaseModel):
    """一个月的 token 用量和费用（设计文档 §37.4）。"""

    month: str = Field(description="YYYY-MM")
    today: date = Field(description="企业时区的今天")
    timezone: str
    own_key: bool = Field(description="现在用的是企业自己的大模型接口密钥（费用记 0）")
    totals: TokenAmounts
    previous: TokenAmounts = Field(
        description="上个月的同一时段：本月看到今天为止，以前的月份是整个上月"
    )
    days: list[TokenDay] = Field(description="每天的用量（本月到今天为止）")
    by_scene: list[TokenGroup]
    by_model: list[TokenGroup]
    by_staff: list[TokenGroup]


class TokenCall(BaseModel):
    id: uuid.UUID
    created_at: datetime
    scene: str
    scene_label: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    tokens: int
    cost: float = Field(description="费用（分）")
    latency_ms: int
    status: str = Field(description="ok 成功、error 失败、busy 并发已满")
    error: str | None
    staff_id: uuid.UUID | None = Field(description="触发的员工；系统（AI 接待、定时任务）为空")
    staff_name: str | None
    session_id: uuid.UUID | None
    own_key: bool = Field(description="用的是企业自己的接口密钥")


class TokenOption(BaseModel):
    key: str
    label: str


class TokenStaff(BaseModel):
    id: str = Field(description="员工 ID；system 为系统")
    name: str


class TokenCallPage(BaseModel):
    """近 7 天的调用明细（新的在前）。"""

    items: list[TokenCall]
    total: int = Field(description="符合筛选条件的调用次数")
    tokens: int = Field(description="符合筛选条件的 tokens 合计")
    cost: float = Field(description="符合筛选条件的费用合计（分）")
    since: datetime = Field(description="明细从这个时间开始（7 天前）")
    scenes: list[TokenOption] = Field(description="近 7 天出现过的场景（筛选用）")
    staff: list[TokenStaff] = Field(description="近 7 天触发过调用的员工（筛选用）")
