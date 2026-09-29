from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints


class CustomerOut(BaseModel):
    id: UUID
    display_name: str
    owner_id: UUID | None
    owner_display_name: str | None
    source_channel: str
    tags: list[str]
    created_at: datetime


class CustomerIdentityOut(BaseModel):
    id: UUID
    channel_account_id: UUID
    channel_type: str
    channel_name: str
    verified: bool
    profile: dict[str, Any] = Field(description="渠道提供的资料，例如访客的来源页面和浏览器")
    last_seen_at: datetime | None
    created_at: datetime


class CustomerDetail(CustomerOut):
    notes: str | None
    identities: list[CustomerIdentityOut]


class CustomerUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    tags: (
        list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]]
        | None
    ) = Field(default=None, max_length=20)


class CustomerPage(BaseModel):
    items: list[CustomerOut]
    total: int


class CustomerCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    owner_id: UUID | None = Field(
        default=None, description="归属坐席；不填时归属创建者。指定他人需要 customer:assign 权限"
    )


_SYNC_WECOM = (
    "同时变更企业微信里的添加人：原成员在职时走在职继承（90 天内每位客户最多转接 2 次，"
    "客户 24 小时后自动接替），已离职时走离职继承"
)


class CustomerTransferRequest(BaseModel):
    customer_ids: list[UUID] = Field(min_length=1, max_length=500)
    to_owner_id: UUID | None = Field(description="新的归属坐席；为空表示取消归属")
    note: str | None = Field(default=None, max_length=500)
    sync_wecom: bool = Field(default=False, description=_SYNC_WECOM)


class HandoverRequest(BaseModel):
    to_owner_id: UUID | None = Field(default=None, description="接手的员工")
    to_group_id: UUID | None = Field(default=None, description="或平均分给这个技能组的成员")
    note: str | None = Field(default=None, max_length=500)
    sync_wecom: bool = Field(default=False, description=_SYNC_WECOM)
    transfer_groups: bool = Field(
        default=False,
        description="同时把他作为群主的企业微信客户群转给接手的员工（客户群继承）",
    )


class WecomTransferSummary(BaseModel):
    requested: int = Field(description="已提交客户继承的客户数（结果稍后回收）")
    skipped: int = Field(description="不需要或无法同步的客户数（没有绑定企业微信成员等）")
    failed: int = Field(description="企业微信拒绝转接的客户数")
    resigned: int = Field(default=0, description="其中走离职继承的客户数（原成员已离职）")
    groups_transferred: int = Field(default=0, description="转给接手员工的客户群数")
    groups_failed: int = Field(default=0, description="转移失败的客户群数")


class TransferResult(BaseModel):
    transferred: int
    wecom: WecomTransferSummary | None = Field(
        default=None, description="勾选了同步企业微信时的在职继承结果"
    )


class OwnerHistoryOut(BaseModel):
    id: UUID
    from_owner_id: UUID | None
    from_owner_name: str | None
    to_owner_id: UUID | None
    to_owner_name: str | None
    actor_name: str | None
    reason: str = Field(description="session_transfer、manual、handover 或 wecom（企业微信添加人）")
    wecom_sync_status: str | None = Field(
        default=None, description="在职继承同步状态：waiting、success、failed；为空表示没有同步"
    )
    note: str | None
    created_at: datetime


class OwnerHistoryList(BaseModel):
    items: list[OwnerHistoryOut]
