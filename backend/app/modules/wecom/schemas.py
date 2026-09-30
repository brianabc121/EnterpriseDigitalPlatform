from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

DEFAULT_WELCOME = "您好，很高兴为您服务！有任何问题，可以点击下方链接随时咨询在线客服。"


class WecomSettings(BaseModel):
    """企业微信接入设置（保存在授权企业上）。"""

    welcome_enabled: bool = Field(
        default=False,
        description="员工添加新客户时自动发送欢迎语（与企业微信后台配置的欢迎语互斥）",
    )
    welcome_text: str = Field(default=DEFAULT_WELCOME, min_length=1, max_length=1000)
    welcome_kf_id: str | None = Field(
        default=None, description="欢迎语附带哪个微信客服账号的链接（open_kfid）；为空则不附带"
    )
    tag_writeback: bool = Field(default=True, description="平台上给客户打的标签写回企业微信")
    notify_agents: bool = Field(
        default=True, description="新会话分配、转接请求、必读知识通过应用消息提醒员工"
    )
    kf_handoff_menu: bool = Field(
        default=True,
        description="微信客服里 AI 的回复带一个「转人工」按钮（菜单消息），客户点选即转人工",
    )
    kf_csat_menu: bool = Field(
        default=True,
        description="微信客服的会话结束时发送满意度评价按钮（菜单消息）",
    )
    zone_enabled: bool = Field(
        default=False,
        description="从数据与智能专区取回群聊分析结果（需要企业购买会话存档并授权专区）",
    )
    zone_program_id: str | None = Field(default=None, max_length=128, description="专区程序 ID")
    zone_ability_id: str | None = Field(
        default=None, max_length=128, description="专区程序的能力 ID"
    )

    @classmethod
    def of(cls, data: dict[str, Any] | None) -> "WecomSettings":
        return cls.model_validate(data or {})


class CorpOut(BaseModel):
    corp_id: str
    corp_name: str
    agent_id: int | None
    status: str
    auth_user_id: str | None = Field(description="授权的企业管理员（成员 userid）")
    authorized_at: datetime
    cancelled_at: datetime | None


class KfAccountOut(BaseModel):
    open_kfid: str
    name: str
    avatar: str | None
    status: str
    channel_id: UUID
    contact_url: str | None = Field(description="客服链接：放在网页、公众号菜单、欢迎语里")
    synced_at: datetime | None


class CallbackUrls(BaseModel):
    command: str = Field(description="指令回调 URL（代开发应用模板）")
    data: str = Field(description="数据回调 URL（代开发应用）")


class WecomCounts(BaseModel):
    members: int
    members_bound: int
    contacts: int
    group_chats: int
    tags: int
    transfers_waiting: int


class WecomStatus(BaseModel):
    enabled: bool = Field(description="平台是否配置了企业微信服务商")
    suite_id: str | None
    callback_urls: CallbackUrls
    corp: CorpOut | None
    settings: WecomSettings | None
    sync_state: dict[str, Any]
    kf_accounts: list[KfAccountOut]
    counts: WecomCounts | None


class InstallOut(BaseModel):
    url: str = Field(description="企业微信授权页地址：管理员扫码授权后跳回控制台")


class MemberOut(BaseModel):
    userid: str
    name: str
    follow: bool = Field(description="配置了客户联系功能")
    status: str
    staff_id: UUID | None
    staff_name: str | None


class MemberList(BaseModel):
    items: list[MemberOut]


class MemberBind(BaseModel):
    staff_id: UUID | None = Field(description="绑定的员工；null 表示解除绑定")


SyncTarget = Literal["members", "kf", "tags", "contacts", "groups"]


def _all_targets() -> list[SyncTarget]:
    return ["members", "kf", "tags", "contacts", "groups"]


class SyncRequest(BaseModel):
    targets: list[SyncTarget] = Field(default_factory=_all_targets, min_length=1)


class SyncAccepted(BaseModel):
    targets: list[str]


class ReplyWindowOut(BaseModel):
    """微信客服的回复限制：客户最后一次发消息后 48 小时内最多发 5 条（设计 §3.1）。"""

    limited: bool = Field(description="false 表示这个渠道没有回复限制")
    open: bool = Field(description="现在能否回复")
    deadline: datetime | None = Field(description="回复截止时间")
    remaining: int | None = Field(description="截止前还能发的条数")
    limit: int | None
    reason: str | None = Field(description="不能回复的原因")


class FollowOut(BaseModel):
    userid: str
    member_name: str | None
    staff_id: UUID | None
    staff_name: str | None
    remark: str | None
    tags: list[str]
    added_at: datetime | None
    deleted: bool


class GroupChatOut(BaseModel):
    chat_id: str
    name: str
    owner_userid: str | None
    owner_name: str | None
    member_count: int
    status: str
    created_time: datetime | None
    summary: str | None = Field(default=None, description="专区返回的最近一次群聊摘要")
    sentiment: str | None = Field(default=None, description="专区返回的群聊情绪")


class WecomTransferOut(BaseModel):
    id: UUID
    external_userid: str
    handover_userid: str
    takeover_userid: str
    kind: str = Field(description="onjob 在职继承，resigned 离职继承")
    status: str
    errcode: int | None
    error: str | None
    created_at: datetime
    takeover_at: datetime | None


class UnassignedCustomerOut(BaseModel):
    """企业微信里待分配的离职成员客户。"""

    handover_userid: str
    handover_name: str | None
    external_userid: str
    customer_id: UUID | None = Field(description="平台上的客户档案；为空表示还没有同步")
    customer_name: str | None
    owner_name: str | None = Field(description="平台上的归属坐席")
    dimission_time: datetime | None


class UnassignedList(BaseModel):
    items: list[UnassignedCustomerOut]


class AssignUnassignedRequest(BaseModel):
    handover_userid: str = Field(min_length=1, max_length=64, description="离职成员")
    external_userids: list[str] | None = Field(
        default=None, max_length=1000, description="要分配的客户；为空表示这位成员的全部客户"
    )
    to_owner_id: UUID = Field(description="接手的员工（需要绑定企业微信成员）")
    transfer_groups: bool = Field(
        default=True, description="同时把他作为群主的客户群转给接手的员工"
    )
    note: str | None = Field(default=None, max_length=500)


class GroupTransferOut(BaseModel):
    id: UUID
    chat_id: str
    group_name: str | None
    handover_userid: str
    handover_name: str | None
    takeover_userid: str
    takeover_name: str | None
    kind: str
    status: str
    error: str | None
    created_at: datetime


class GroupTransferList(BaseModel):
    items: list[GroupTransferOut]


class JoinWayCreate(BaseModel):
    """ "加入群聊"二维码：扫码进入指定的客户群（最多 5 个），群满后可以自动建新群。"""

    name: str = Field(min_length=1, max_length=30, description="二维码名称（仅平台内显示）")
    chat_ids: list[str] = Field(min_length=1, max_length=5)
    auto_create_room: bool = Field(default=True, description="群满后自动新建群")
    room_base_name: str | None = Field(
        default=None, max_length=40, description="自动建群的群名前缀，如「VIP 客户群」"
    )
    room_base_id: int | None = Field(
        default=None, ge=1, le=100000, description="自动建群的起始序号"
    )


class JoinWayOut(BaseModel):
    id: UUID
    config_id: str
    name: str
    chat_ids: list[str]
    group_names: list[str]
    auto_create_room: bool
    room_base_name: str | None
    room_base_id: int | None
    state: str
    qr_code: str | None = Field(description="二维码图片地址（企业微信提供）")
    joined: int = Field(description="经这个二维码进群、现在仍在群里的客户数")
    created_at: datetime


class JoinWayList(BaseModel):
    items: list[JoinWayOut]


BroadcastKindLiteral = Literal["single", "group"]


class BroadcastLink(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    url: str = Field(pattern=r"^https?://", max_length=2048)
    desc: str | None = Field(default=None, max_length=256)


class BroadcastAudience(BaseModel):
    """群发对象。发给客户时按客户、标签、归属坐席筛选（取交集，只含员工可见的客户）；
    发到客户群时按群或群主筛选。"""

    customer_ids: list[UUID] | None = Field(default=None, max_length=10000)
    tags: list[str] | None = Field(default=None, max_length=20, description="带任一标签的客户")
    owner_ids: list[UUID] | None = Field(default=None, max_length=200, description="归属坐席")
    chat_ids: list[str] | None = Field(default=None, max_length=2000, description="客户群")


class BroadcastCreate(BaseModel):
    kind: BroadcastKindLiteral
    title: str = Field(min_length=1, max_length=64, description="任务名称（仅平台内显示）")
    content: str = Field(min_length=1, max_length=4000)
    link: BroadcastLink | None = None
    audience: BroadcastAudience = Field(default_factory=BroadcastAudience)


class BroadcastMemberOut(BaseModel):
    userid: str
    name: str | None
    confirmed: bool = Field(description="员工是否已在企业微信里确认发送")
    send_time: datetime | None
    sent: int
    failed: int
    unsent: int


class BroadcastOut(BaseModel):
    id: UUID
    kind: str
    title: str
    content: str
    link: BroadcastLink | None
    target_count: int
    status: str
    error: str | None
    stats: dict[str, Any]
    created_by_name: str | None
    created_at: datetime
    polled_at: datetime | None


class BroadcastDetail(BroadcastOut):
    members: list[BroadcastMemberOut]
    fail_list: list[Any] = Field(description="企业微信没有接受的客户（不是好友等）")


class BroadcastList(BaseModel):
    items: list[BroadcastOut]


class BroadcastOwner(BaseModel):
    id: UUID
    name: str


class BroadcastOptions(BaseModel):
    """创建群发时可选的对象（员工可见范围内）。"""

    tags: list[str]
    owners: list[BroadcastOwner]
    group_chats: list[GroupChatOut]


class TagOptions(BaseModel):
    items: list[str] = Field(description="企业标签（改标签时的候选）")


class CustomerWecom(BaseModel):
    """客户在企业微信里的信息（客户 360 视图）。"""

    external_userid: str | None
    unionid: str | None
    follows: list[FollowOut]
    group_chats: list[GroupChatOut]
    transfers: list[WecomTransferOut]


class JsSignature(BaseModel):
    timestamp: int
    nonce_str: str
    signature: str


class JssdkConfig(BaseModel):
    """企业微信 JS-SDK 的注入配置：wx.config 用企业签名，wx.agentConfig 用应用签名。"""

    corp_id: str
    agent_id: int | None
    config: JsSignature
    agent_config: JsSignature


class SidebarIdentity(BaseModel):
    channel_type: str
    channel_name: str
    profile: dict[str, Any]


class SidebarCustomer(BaseModel):
    id: UUID
    display_name: str
    owner_id: UUID | None
    owner_name: str | None
    tags: list[str]
    notes: str | None
    identities: list[SidebarIdentity]


class SidebarSession(BaseModel):
    id: UUID
    channel_name: str
    status: str
    summary: str | None = Field(description="转人工时的交接摘要")
    csat: int | None
    created_at: datetime
    closed_at: datetime | None


class SidebarCustomerBrief(BaseModel):
    id: UUID
    display_name: str
    tags: list[str]


class SidebarContext(BaseModel):
    """侧边栏当前聊天的上下文：单聊是一位客户，客户群是群信息和群里的客户。"""

    kind: Literal["contact", "group"]
    customer: SidebarCustomer | None
    wecom: CustomerWecom | None
    sessions: list[SidebarSession]
    group: GroupChatOut | None
    group_customers: list[SidebarCustomerBrief]


class SidebarSuggestRequest(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=2000,
        description="客户的问题（员工粘贴或输入；JS-SDK 读不到聊天内容）",
    )
    external_userid: str | None = Field(default=None, max_length=64)


SidebarOrigin = Literal["manual", "quick_reply", "suggestion", "knowledge"]


class SidebarSentRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    origin: SidebarOrigin = "manual"
    external_userid: str | None = Field(default=None, max_length=64)
    chat_id: str | None = Field(default=None, max_length=64)
    question: str | None = Field(
        default=None, max_length=2000, description="这条回复针对的客户问题（用于沉淀知识）"
    )


class SidebarTagsUpdate(BaseModel):
    tags: list[str] = Field(max_length=20)


class SidebarMemberOut(BaseModel):
    """可以拉进群的企业成员（接单员等）。"""

    userid: str
    name: str
    staff_name: str | None


class SidebarMemberList(BaseModel):
    items: list[SidebarMemberOut]


class SidebarGroupCreated(BaseModel):
    """员工在侧边栏用 openEnterpriseChat 建好群后，把群 ID 告诉平台。"""

    chat_id: str = Field(min_length=1, max_length=64)
    external_userid: str | None = Field(default=None, max_length=64)


class WecomLoginRequest(BaseModel):
    corp_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=512)


class SsoUrlOut(BaseModel):
    url: str
