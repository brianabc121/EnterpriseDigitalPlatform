from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class ServiceStats(BaseModel):
    """一组会话的服务指标。时长均为秒；没有样本时为空。"""

    sessions: int = Field(description="新会话数（按会话创建时间统计）")
    human_sessions: int = Field(description="分配给坐席的会话")
    closed_sessions: int
    missed_sessions: int = Field(description="排队超时或非工作时间转为留言的会话")
    avg_wait_seconds: float | None = Field(description="平均排队时长：首次排队到首次分配")
    avg_first_response_seconds: float | None = Field(
        description="平均首次响应时长：首次分配到坐席第一条回复"
    )
    avg_handle_seconds: float | None = Field(description="平均处理时长：首次分配到会话结束")
    csat_avg: float | None = Field(description="平均满意度（1-5）")
    csat_count: int = Field(description="已评价的会话数")
    satisfied_rate: float | None = Field(description="满意率：4 分及以上占已评价的比例")


class AiStats(BaseModel):
    """AI 接待指标（P3）。"""

    ai_sessions: int = Field(description="由 AI 接待的会话")
    ai_resolved: int = Field(description="AI 独立解决的会话（AI 接待中结束、未转人工）")
    ai_handoffs: int = Field(description="AI 接待后转人工的会话")
    ai_resolution_rate: float | None = Field(
        description="AI 独立解决率：AI 解决的会话占 AI 接待会话的比例"
    )


class OverviewTotals(ServiceStats, AiStats):
    messages_in: int = Field(description="客户消息数（按发送时间统计）")
    agent_messages: int = Field(description="坐席消息数")
    tickets: int = Field(description="新增留言")
    transfers: int = Field(description="完成的会话转接")


class DailyStats(ServiceStats, AiStats):
    day: date


class Overview(BaseModel):
    start: date
    end: date
    timezone: str
    totals: OverviewTotals
    days: list[DailyStats]


class AgentStats(ServiceStats):
    staff_id: UUID
    display_name: str
    status: str = Field(description="当前接待状态")
    messages: int = Field(description="发出的消息数")
    transfers_out: int = Field(description="转出的会话")


class AgentReport(BaseModel):
    start: date
    end: date
    timezone: str
    items: list[AgentStats]


class Realtime(BaseModel):
    """首页实时数据。坐席只看到自己的接待数据；主管看到所带团队；管理员看到全部。"""

    queued: int = Field(description="正在排队的会话（全租户）")
    ai_serving: int = Field(description="AI 正在接待的会话（全租户）")
    longest_wait_seconds: int | None = Field(description="排队最久的会话已等待的秒数")
    serving: int = Field(description="可见范围内正在接待的会话")
    agents_online: int = Field(description="可见范围内在线的坐席")
    agents_busy: int
    agents_away: int
    today_sessions: int = Field(description="可见范围内今天的新会话")
    today_closed: int
    today_csat_avg: float | None
    today_csat_count: int
    my_serving: int = Field(description="我正在接待的会话")
    my_today_sessions: int = Field(description="今天分配给我的会话")


# ---- 待办与订单（设计文档 §24.10、§25.10） ----


class Bucket(BaseModel):
    key: str
    label: str
    count: int
    amount: Decimal | None = None


class TodoTotals(BaseModel):
    created: int
    done: int
    cancelled: int
    rejected: int
    pending_now: int = Field(description="当前待确认")
    overdue_now: int = Field(description="当前已逾期未完成")
    unclaimed_now: int = Field(description="当前待认领")
    oldest_unclaimed_minutes: int | None


class TodoTimeliness(BaseModel):
    avg_confirm_minutes: float | None = Field(description="AI 生成到确认的平均时长")
    avg_first_response_minutes: float | None
    avg_resolve_minutes: float | None
    on_time_rate: float | None = Field(description="有截止时间的已完成待办中按时完成的比例（%）")


class TodoAiQuality(BaseModel):
    ai_created: int
    confirmed_direct: int = Field(description="直接确认")
    confirmed_modified: int = Field(description="修改后确认")
    rejected: int
    reject_reasons: list[Bucket]
    cancelled_after_confirm: int


class TodoDaily(BaseModel):
    day: date
    created: int
    done: int


class TodoReport(BaseModel):
    start: date
    end: date
    totals: TodoTotals
    by_source: list[Bucket]
    by_type: list[Bucket]
    timeliness: TodoTimeliness
    ai: TodoAiQuality
    daily: list[TodoDaily]


class OrderAiStats(BaseModel):
    intent_sessions: int = Field(description="AI 采集过订单的会话")
    submitted: int = Field(description="AI 提交审核的订单")
    approval_rate: float | None = Field(description="已处理的 AI 订单中确认的比例（%）")
    completed_rate: float | None = Field(description="AI 订单最终完成的比例（%）")
    modified_rate: float | None = Field(description="AI 订单被员工修改过的比例（%）")
    modify_reasons: list[Bucket]


class OrderBusiness(BaseModel):
    orders: int
    amount: Decimal = Field(description="未取消订单的合计金额")
    by_source: list[Bucket]
    by_channel: list[Bucket]
    by_agent: list[Bucket]
    top_products: list[Bucket] = Field(description="销量前 10 的商品（count 为数量）")
    avg_review_minutes: float | None = Field(description="提交到确认的平均时长")
    cancel_reasons: list[Bucket]
    product_gaps: list[Bucket] = Field(description="客户问到、商品库里没有的商品前 10")


class OrderPayments(BaseModel):
    by_method: list[Bucket]
    receivable: Decimal = Field(description="已确认订单中未收的金额")
    overdue_receivable: Decimal = Field(description="暂欠逾期未收的金额")
    overdue_orders: int


class OrderSecurity(BaseModel):
    price_probes: int = Field(description="套价识别次数")
    replies_blocked: int = Field(description="回复拦截次数")


class OrderNow(BaseModel):
    pending_review: int
    oldest_pending_minutes: int | None


class OrderReport(BaseModel):
    start: date
    end: date
    ai: OrderAiStats
    business: OrderBusiness
    payments: OrderPayments
    security: OrderSecurity
    now: OrderNow
