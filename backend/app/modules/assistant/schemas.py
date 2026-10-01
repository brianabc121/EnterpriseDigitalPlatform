import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Provider = Literal["wecom", "dingtalk", "feishu", "telegram", "whatsapp"]
BotStatusValue = Literal["active", "disabled"]
ReplyModeValue = Literal["silent", "mentioned"]


class FieldSpecOut(BaseModel):
    key: str
    label: str
    help: str = ""
    required: bool = True


class ProviderOut(BaseModel):
    provider: Provider
    name: str
    config_fields: list[FieldSpecOut]
    secret_fields: list[FieldSpecOut]
    notes: list[str] = Field(description="要在平台后台配置什么")
    notify: bool = Field(description="能不能主动给员工发通知")
    groups: str = Field(description="群消息的范围说明")


class ProviderList(BaseModel):
    items: list[ProviderOut]
    webhook_base: str = Field(description="回调地址的前缀（平台对外地址）")


class BotIn(BaseModel):
    """添加或修改机器人。修改时没给的密钥保持不变。"""

    provider: Provider
    name: str = Field(min_length=1, max_length=64)
    config: dict[str, str] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict, description="密钥；修改时不填表示不变")


class BotOut(BaseModel):
    id: uuid.UUID
    provider: Provider
    name: str
    config: dict[str, Any]
    secret_keys: list[str] = Field(description="已经保存了哪些密钥（不回显内容）")
    webhook_url: str
    status: BotStatusValue
    last_received_at: datetime | None
    last_sent_at: datetime | None
    last_error: str | None
    failures: int
    identities: int = Field(description="绑定到这个机器人的员工数")
    groups: int = Field(description="记录的群数")
    created_at: datetime
    updated_at: datetime


class BotList(BaseModel):
    items: list[BotOut]


class BotTestIn(BaseModel):
    text: str = Field(default="这是一条来自 AI 助理的测试消息。", min_length=1, max_length=500)


class BotTestOut(BaseModel):
    ok: bool
    sent: int = Field(description="发给了几位已绑定的员工")
    error: str | None


class IdentityOut(BaseModel):
    id: uuid.UUID
    bot_id: uuid.UUID
    bot_name: str
    provider: Provider
    external_user_id: str
    display_name: str
    staff_id: uuid.UUID | None
    staff_name: str | None
    bound_at: datetime | None
    last_seen_at: datetime | None


class IdentityList(BaseModel):
    items: list[IdentityOut]


class IdentityBind(BaseModel):
    staff_id: uuid.UUID


class BindingCodeOut(BaseModel):
    code: str
    expires_at: datetime
    hint: str = Field(description="告诉员工怎么用这个码")


class GroupOut(BaseModel):
    id: uuid.UUID
    bot_id: uuid.UUID
    bot_name: str
    provider: Provider
    external_chat_id: str
    name: str
    recording: bool
    reply_mode: ReplyModeValue | None = Field(description="为空时按租户设置")
    extract: bool
    message_count: int
    unextracted: int = Field(description="还没提炼的消息数")
    last_message_at: datetime | None
    last_extracted_at: datetime | None
    extracted_candidates: int
    created_at: datetime


class GroupList(BaseModel):
    items: list[GroupOut]


class GroupUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    recording: bool | None = None
    reply_mode: ReplyModeValue | None = None
    clear_reply_mode: bool = Field(default=False, description="改回按租户设置")
    extract: bool | None = None


class GroupMessageOut(BaseModel):
    id: uuid.UUID
    sender_name: str
    staff_id: uuid.UUID | None
    staff_name: str | None
    text: str
    sent_at: datetime
    extracted: bool


class GroupMessagePage(BaseModel):
    items: list[GroupMessageOut]
    total: int


class GroupExtractOut(BaseModel):
    messages: int = Field(description="这次提炼了几条消息")
    candidates: int = Field(description="记录了几条知识候选")
    error: str | None


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ChatOut(BaseModel):
    reply: str
    tools: list[str] = Field(description="这次回答用到的工具")


class ChatMessageOut(BaseModel):
    id: uuid.UUID
    role: Literal["user", "assistant"]
    text: str
    tools: list[str]
    created_at: datetime


class ChatHistory(BaseModel):
    items: list[ChatMessageOut]


class MyBindingOut(BaseModel):
    id: uuid.UUID
    bot_id: uuid.UUID
    bot_name: str
    provider: Provider
    display_name: str
    bound_at: datetime | None


class MyBindings(BaseModel):
    items: list[MyBindingOut]
    bots: list[BotOut] = Field(description="企业接入的机器人（员工据此知道去哪里找助理）")
