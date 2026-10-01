"""IM 机器人适配器（设计文档 §27.3.1）：企业微信、钉钉、飞书、Telegram、WhatsApp。"""

from app.integrations.imbots import dingtalk, feishu, telegram, wecom, whatsapp
from app.integrations.imbots.base import (
    Adapter,
    BotContext,
    Inbound,
    InboundMessage,
    ProviderSpec,
    Rejected,
    SendError,
    WebhookRequest,
    WebhookResponse,
)

ADAPTERS: dict[str, Adapter] = {
    "wecom": wecom.WecomBotAdapter(),
    "dingtalk": dingtalk.DingTalkAdapter(),
    "feishu": feishu.FeishuAdapter(),
    "telegram": telegram.TelegramAdapter(),
    "whatsapp": whatsapp.WhatsAppAdapter(),
}
SPECS: dict[str, ProviderSpec] = {
    spec.provider: spec
    for spec in (wecom.SPEC, dingtalk.SPEC, feishu.SPEC, telegram.SPEC, whatsapp.SPEC)
}
PROVIDERS = tuple(ADAPTERS)


def adapter_for(provider: str) -> Adapter:
    try:
        return ADAPTERS[provider]
    except KeyError as exc:
        raise ValueError(f"不支持的平台：{provider}") from exc


__all__ = [
    "ADAPTERS",
    "PROVIDERS",
    "SPECS",
    "Adapter",
    "BotContext",
    "Inbound",
    "InboundMessage",
    "ProviderSpec",
    "Rejected",
    "SendError",
    "WebhookRequest",
    "WebhookResponse",
    "adapter_for",
]
