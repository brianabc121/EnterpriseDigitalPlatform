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


class WecomTransferOut(BaseModel):
    id: UUID
    external_userid: str
    handover_userid: str
    takeover_userid: str
    status: str
    errcode: int | None
    error: str | None
    created_at: datetime
    takeover_at: datetime | None


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


class WecomLoginRequest(BaseModel):
    corp_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=512)


class SsoUrlOut(BaseModel):
    url: str
