from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, field_validator

from app.modules.routing.hours import validate_business_hours
from app.modules.routing.models import AgentStatus, RoutingMode

BusinessHours = Annotated[
    dict[str, Any] | None,
    AfterValidator(lambda v: validate_business_hours(v) if v is not None else None),
    Field(
        description=(
            '工作时间，为空表示全天服务。形如 {"tz": "Asia/Shanghai", '
            '"days": {"1": [["09:00", "18:00"]]}}，1 为周一，7 为周日；没有列出的日子休息'
        ),
        examples=[{"tz": "Asia/Shanghai", "days": {"1": [["09:00", "12:00"], ["13:00", "18:00"]]}}],
    ),
]


# ---- 技能组 ----


class SkillGroupMemberIn(BaseModel):
    staff_id: UUID
    is_lead: bool = False


class SkillGroupMemberOut(BaseModel):
    staff_id: UUID
    display_name: str
    is_lead: bool


class SkillGroupOut(BaseModel):
    id: UUID
    name: str
    members: list[SkillGroupMemberOut]
    overflow_group_id: UUID | None = None
    overflow_after_seconds: int = 0
    created_at: datetime


class SkillGroupList(BaseModel):
    items: list[SkillGroupOut]


_OVERFLOW = "排队超过这么多秒仍没有分配时，改由备用技能组接待；0 表示不溢出"


class SkillGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    members: list[SkillGroupMemberIn] = Field(default_factory=list)
    overflow_group_id: UUID | None = Field(default=None, description="备用技能组")
    overflow_after_seconds: int = Field(default=0, ge=0, le=86400, description=_OVERFLOW)


class SkillGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    members: list[SkillGroupMemberIn] | None = Field(
        default=None, description="给出时整体替换成员列表"
    )
    overflow_group_id: UUID | None = Field(default=None, description="备用技能组；null 表示取消")
    overflow_after_seconds: int | None = Field(default=None, ge=0, le=86400, description=_OVERFLOW)


# ---- 路由策略 ----

Keyword = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]


class IntentRoute(BaseModel):
    """按意图分配：AI 识别出这个意图，或客户的话里出现关键词时，分配到这个技能组。"""

    intent: Keyword = Field(description="意图名称，如 售前、售后、技术、投诉")
    keywords: list[Keyword] = Field(default_factory=list, max_length=50)
    skill_group_id: UUID


def _unique_intents(routes: list[IntentRoute]) -> list[IntentRoute]:
    names = [r.intent for r in routes]
    if len(set(names)) != len(names):
        raise ValueError("意图名称不能重复")
    return routes


_PRIORITY_TAGS = "带这些标签的客户排队时排在最前（VIP）"
_URGENT = "投诉、退款等敏感诉求或情绪激动的客户排在普通客户前面"
_AI_WHILE_QUEUED = "排队期间 AI 继续回答客户的其他问题（需要启用 AI 接待）"


class RoutingPolicyOut(BaseModel):
    id: UUID
    name: str
    is_default: bool
    mode: RoutingMode
    default_skill_group_id: UUID | None
    owner_first: bool
    max_wait_seconds: int
    idle_close_minutes: int
    resume_window_minutes: int
    business_hours: dict[str, Any] | None
    priority_tags: list[str] = Field(default_factory=list, description=_PRIORITY_TAGS)
    urgent_first: bool = Field(default=True, description=_URGENT)
    ai_while_queued: bool = Field(default=False, description=_AI_WHILE_QUEUED)
    intent_routes: list[IntentRoute] = Field(default_factory=list)
    channel_ids: list[UUID] = Field(description="使用这套策略的渠道账号")
    created_at: datetime


class RoutingPolicyList(BaseModel):
    items: list[RoutingPolicyOut]


class RoutingPolicyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    mode: RoutingMode = Field(
        default=RoutingMode.HUMAN_FIRST,
        description="ai_first 在租户启用 AI 接待后生效，之前按 human_first 处理",
    )
    default_skill_group_id: UUID | None = None
    owner_first: bool = True
    max_wait_seconds: int = Field(default=300, ge=10, le=86400)
    idle_close_minutes: int = Field(default=30, ge=1, le=1440)
    resume_window_minutes: int = Field(
        default=10,
        ge=0,
        le=1440,
        description="会话结束后多少分钟内再来咨询优先分配给上次的坐席；0 关闭",
    )
    business_hours: BusinessHours = None
    priority_tags: list[Keyword] = Field(
        default_factory=lambda: ["VIP"], max_length=20, description=_PRIORITY_TAGS
    )
    urgent_first: bool = Field(default=True, description=_URGENT)
    ai_while_queued: bool = Field(default=False, description=_AI_WHILE_QUEUED)
    intent_routes: list[IntentRoute] = Field(default_factory=list, max_length=20)

    @field_validator("intent_routes")
    @classmethod
    def _intents(cls, routes: list[IntentRoute]) -> list[IntentRoute]:
        return _unique_intents(routes)


class RoutingPolicyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    is_default: Literal[True] | None = Field(
        default=None, description="设为租户默认策略（原默认策略自动取消）"
    )
    mode: RoutingMode | None = None
    default_skill_group_id: UUID | None = None
    owner_first: bool | None = None
    max_wait_seconds: int | None = Field(default=None, ge=10, le=86400)
    idle_close_minutes: int | None = Field(default=None, ge=1, le=1440)
    resume_window_minutes: int | None = Field(default=None, ge=0, le=1440)
    business_hours: BusinessHours = None
    priority_tags: list[Keyword] | None = Field(default=None, max_length=20)
    urgent_first: bool | None = None
    ai_while_queued: bool | None = None
    intent_routes: list[IntentRoute] | None = Field(default=None, max_length=20)

    @field_validator("intent_routes")
    @classmethod
    def _intents(cls, routes: list[IntentRoute] | None) -> list[IntentRoute] | None:
        return None if routes is None else _unique_intents(routes)


# ---- 坐席 ----


class AgentGroupOut(BaseModel):
    id: UUID
    name: str
    is_lead: bool


class AgentOut(BaseModel):
    staff_id: UUID
    username: str
    display_name: str
    status: AgentStatus
    max_concurrency: int
    active_sessions: int
    last_seen_at: datetime | None
    skill_groups: list[AgentGroupOut]


class AgentList(BaseModel):
    items: list[AgentOut]


class AgentUpdate(BaseModel):
    max_concurrency: int = Field(ge=1, le=50)


class MyAgentState(BaseModel):
    status: AgentStatus
    max_concurrency: int
    active_sessions: int
    last_seen_at: datetime | None


class MyAgentStatusUpdate(BaseModel):
    status: AgentStatus


class AgentIMCredentials(BaseModel):
    """坐席工作台用 OpenIM SDK 登录所需的信息。"""

    user_id: str
    token: str
    expires_in: int
    api_url: str
    ws_url: str
    platform_id: int
    system_user_id: str = Field(description="发送在线信令的系统用户，工作台据此识别信令")
