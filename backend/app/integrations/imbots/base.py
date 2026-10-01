"""IM 机器人适配器的公共接口（设计文档 §27.3.1）。

每个平台实现一个 Adapter：
- receive：验签、解析回调，返回收到的消息和要直接返回给平台的响应（地址验证）；
- send_text：给一个会话或一个人发文字；
- setup：保存机器人后在平台侧做的准备（例如 Telegram 的 setWebhook），可选。

适配器只做协议转换，不碰数据库；机器人的配置、密钥由调用方给出。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

import httpx

from app.core.config import Settings

ChatType = Literal["private", "group"]
TargetKind = Literal["chat", "user"]


class Rejected(Exception):
    """验签失败、密文无法解密或内容不是本机器人的：回调返回 403，不记录任何数据。"""


class SendError(Exception):
    """发送失败（平台拒绝、网络错误、这个平台不支持这种发送）。"""


@dataclass(frozen=True)
class WebhookRequest:
    method: str
    query: dict[str, str]
    headers: dict[str, str]  # 键为小写
    body: bytes

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


@dataclass(frozen=True)
class WebhookResponse:
    """要直接返回给平台的响应。"""

    body: str
    media_type: str = "text/plain"
    status_code: int = 200


@dataclass(frozen=True)
class InboundMessage:
    external_message_id: str
    chat_type: ChatType
    # 私聊：回复用的会话 ID（Telegram 里就是用户 ID）；群：群 ID。
    chat_id: str
    chat_name: str
    sender_id: str
    sender_name: str
    text: str
    sent_at: datetime
    # 群里 @ 了机器人（企业微信、钉钉的机器人在群里只收到 @ 它的消息，一律为 True）。
    mentioned: bool = False
    # 平台专用的回复句柄（钉钉 sessionWebhook、企业微信 response_url），有效期有限。
    reply_to: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Inbound:
    messages: list[InboundMessage] = field(default_factory=list)
    response: WebhookResponse | None = None


@dataclass(frozen=True)
class BotContext:
    """一个机器人的配置：非密配置、解密后的密钥、平台给它分配的回调地址和令牌。"""

    config: dict[str, Any]
    secrets: dict[str, str]
    webhook_url: str
    webhook_token: str

    def secret(self, key: str) -> str:
        return str(self.secrets.get(key) or "")

    def setting(self, key: str) -> str:
        return str(self.config.get(key) or "")


class Adapter(Protocol):
    provider: str

    async def receive(self, request: WebhookRequest, bot: BotContext) -> Inbound: ...

    async def send_text(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        bot: BotContext,
        *,
        kind: TargetKind,
        target: str,
        text: str,
        reply_to: dict[str, str] | None = None,
    ) -> None: ...

    async def setup(
        self, http: httpx.AsyncClient, settings: Settings, bot: BotContext
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    help: str = ""
    required: bool = True


@dataclass(frozen=True)
class ProviderSpec:
    """给控制台的接入向导：要填哪些配置和密钥、要在平台后台配置什么。"""

    provider: str
    name: str
    config_fields: tuple[FieldSpec, ...]
    secret_fields: tuple[FieldSpec, ...]
    notes: tuple[str, ...]
    # 能不能主动给员工发通知；群消息的范围说明。
    notify: bool
    groups: str


def check_fields(bot: BotContext, spec: ProviderSpec) -> None:
    missing = [f.label for f in spec.config_fields if f.required and not bot.setting(f.key)]
    missing += [f.label for f in spec.secret_fields if f.required and not bot.secret(f.key)]
    if missing:
        raise ValueError("缺少：" + "、".join(missing))


def utc_from_epoch(value: Any, *, millis: bool = False) -> datetime:
    from datetime import UTC

    try:
        number = float(value)
    except (TypeError, ValueError):
        return datetime.now(UTC)
    if millis:
        number /= 1000
    return datetime.fromtimestamp(number, UTC)
