from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

SecurityValue = Literal["ssl", "starttls", "none"]
MailStatusValue = Literal["active", "paused", "disabled"]


class MailServer(BaseModel):
    host: str = Field(max_length=255, description="服务器地址")
    port: int = Field(ge=1, le=65535)
    security: SecurityValue = Field(
        description="ssl 连接时就加密；starttls 连接后升级为加密；none 不加密（只用于测试环境）"
    )


class MailProviderOut(BaseModel):
    key: str
    name: str
    domains: list[str] = Field(description="按地址的域名自动选择这个邮箱类型")
    imap: MailServer
    smtp: MailServer
    secret_label: str = Field(description="密码输入框的名称：授权码、应用专用密码……")
    help: str = Field(description="怎么开启 IMAP/SMTP、获取授权码")


class MailProviderList(BaseModel):
    items: list[MailProviderOut]
    allow_insecure: bool = Field(description="可以选择不加密的连接（只在测试环境）")


class MailAccountIn(BaseModel):
    """添加或修改邮箱。修改时不填授权码表示不变。"""

    name: str = Field(min_length=1, max_length=64, description="渠道名称，例如：售后邮箱")
    address: str = Field(min_length=3, max_length=254, description="邮箱地址")
    display_name: str = Field(default="", max_length=64, description="回复邮件时的发件人名称")
    provider: str = Field(max_length=16, description="邮箱类型（见 /mail/providers）")
    username: str | None = Field(default=None, max_length=254, description="登录名，默认是邮箱地址")
    secret: str | None = Field(
        default=None, max_length=256, description="授权码或密码；修改时不填表示不变"
    )
    imap: MailServer | None = Field(default=None, description="收信服务器；不填时用邮箱类型的预设")
    smtp: MailServer | None = Field(default=None, description="发信服务器；不填时用邮箱类型的预设")
    signature: str | None = Field(default=None, max_length=2000, description="回复邮件末尾的签名")
    ignore_senders: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="不导入的发件人：完整地址或 @域名",
    )


class MailTestIn(MailAccountIn):
    account_id: UUID | None = Field(
        default=None, description="修改已有邮箱时：没填授权码就用保存的授权码测试"
    )


class MailTestOut(BaseModel):
    ok: bool
    imap_error: str | None = Field(description="收信（IMAP）的错误；为空表示正常")
    smtp_error: str | None = Field(description="发信（SMTP）的错误；为空表示正常")
    inbox: int | None = Field(description="收件箱里的邮件数（收信正常时）")


class MailAccountOut(BaseModel):
    id: UUID
    channel_account_id: UUID
    name: str
    address: str
    display_name: str
    provider: str
    username: str
    imap: MailServer
    smtp: MailServer
    signature: str | None
    ignore_senders: list[str]
    status: MailStatusValue = Field(
        description="active 正常收信；paused 登录连续失败已暂停；disabled 已停用"
    )
    last_polled_at: datetime | None = Field(description="最近一次收信")
    last_received_at: datetime | None = Field(description="最近收到邮件的时间")
    next_poll_at: datetime
    failures: int = Field(description="连续失败的次数")
    last_error: str | None
    ignored: int = Field(description="没有导入的邮件数（自动回复、退信、群发、忽略的发件人）")
    created_at: datetime
    updated_at: datetime


class MailAccountList(BaseModel):
    items: list[MailAccountOut]


class MailFetchOut(BaseModel):
    imported: int = Field(description="这次导入的邮件数")
    ignored: int = Field(description="这次没有导入的邮件数")
    error: str | None
    account: MailAccountOut


class MailOriginalOut(BaseModel):
    """原邮件：在沙箱 iframe 里显示的 HTML（不执行脚本、不加载外部资源）。"""

    subject: str
    html: str
