from datetime import date
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


class OverviewTotals(ServiceStats):
    messages_in: int = Field(description="客户消息数（按发送时间统计）")
    agent_messages: int = Field(description="坐席消息数")
    tickets: int = Field(description="新增留言")
    transfers: int = Field(description="完成的会话转接")


class DailyStats(ServiceStats):
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
