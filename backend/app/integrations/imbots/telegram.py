"""Telegram Bot API（Webhook）。

- 保存机器人时调用 setWebhook，把平台的回调地址和随机令牌（secret_token）登记到 Telegram；
  之后每次回调带 X-Telegram-Bot-Api-Secret-Token，与令牌不一致的拒绝。
- 群里要收到全部消息需要在 BotFather 关闭隐私模式（/setprivacy）或把机器人设为群管理员。
- 主动发消息：员工先给机器人发过消息，Telegram 才允许机器人发给他（私聊的 chat_id 就是用户 ID）。
"""

import hmac
import json
from typing import Any

import httpx

from app.core.config import Settings
from app.integrations.imbots.base import (
    BotContext,
    ChatType,
    FieldSpec,
    Inbound,
    InboundMessage,
    ProviderSpec,
    Rejected,
    SendError,
    TargetKind,
    WebhookRequest,
    check_fields,
    utc_from_epoch,
)

SPEC = ProviderSpec(
    provider="telegram",
    name="Telegram",
    config_fields=(
        FieldSpec(
            "bot_username", "机器人用户名", "保存后自动从 Telegram 获取，用于识别群里的 @", False
        ),
    ),
    secret_fields=(FieldSpec("bot_token", "Bot Token", "在 BotFather 里创建机器人后得到"),),
    notes=(
        "保存后平台会自动把回调地址登记到 Telegram（setWebhook），"
        "回调地址必须是公网可访问的 HTTPS。",
        "群里要收到全部消息：在 BotFather 里 /setprivacy 选择 Disable，或把机器人设为群管理员。",
        "员工先给机器人发一条消息（或在私聊里绑定），之后助理才能主动给他发通知。",
    ),
    notify=True,
    groups="关闭隐私模式或设为群管理员后收到全部消息",
)


def _name(user: dict[str, Any]) -> str:
    parts = [str(user.get("first_name") or ""), str(user.get("last_name") or "")]
    full = " ".join(p for p in parts if p).strip()
    return full or str(user.get("username") or user.get("id") or "")


class TelegramAdapter:
    provider = "telegram"

    def _base(self, settings: Settings, bot: BotContext) -> str:
        return f"{settings.telegram_api_url.rstrip('/')}/bot{bot.secret('bot_token')}"

    async def receive(self, request: WebhookRequest, bot: BotContext) -> Inbound:
        if request.method != "POST":
            raise Rejected("telegram only posts updates")
        given = request.header("X-Telegram-Bot-Api-Secret-Token")
        if not given or not hmac.compare_digest(given, bot.webhook_token):
            raise Rejected("secret token mismatch")
        try:
            update = json.loads(request.body or b"{}")
        except json.JSONDecodeError as exc:
            raise Rejected("body is not json") from exc
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            return Inbound()
        text = message.get("text")
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if not isinstance(text, str) or not text.strip() or sender.get("is_bot"):
            return Inbound()
        chat_type: ChatType = "private" if chat.get("type") == "private" else "group"
        username = bot.setting("bot_username").lstrip("@").lower()
        mentioned = chat_type == "group" and bool(username) and f"@{username}" in text.lower()
        if username:
            text = text.replace(f"@{bot.setting('bot_username').lstrip('@')}", "").strip()
        return Inbound(
            messages=[
                InboundMessage(
                    external_message_id=str(message.get("message_id") or ""),
                    chat_type=chat_type,
                    chat_id=str(chat.get("id") or ""),
                    chat_name=str(chat.get("title") or ""),
                    sender_id=str(sender.get("id") or ""),
                    sender_name=_name(sender),
                    text=text,
                    sent_at=utc_from_epoch(message.get("date")),
                    mentioned=mentioned,
                )
            ]
        )

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
    ) -> None:
        try:
            response = await http.post(
                f"{self._base(settings, bot)}/sendMessage",
                json={"chat_id": target, "text": text},
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SendError(f"Telegram 请求失败：{exc}") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            raise SendError(
                f"Telegram 拒绝：{data.get('description') if isinstance(data, dict) else data}"
            )

    async def setup(
        self, http: httpx.AsyncClient, settings: Settings, bot: BotContext
    ) -> dict[str, Any]:
        check_fields(bot, SPEC)
        base = self._base(settings, bot)
        try:
            me = (await http.get(f"{base}/getMe")).json()
            hook = (
                await http.post(
                    f"{base}/setWebhook",
                    json={
                        "url": bot.webhook_url,
                        "secret_token": bot.webhook_token,
                        "allowed_updates": ["message"],
                    },
                )
            ).json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SendError(f"Telegram 请求失败：{exc}") from exc
        if not isinstance(me, dict) or not me.get("ok"):
            raise SendError("Bot Token 无效")
        if not isinstance(hook, dict) or not hook.get("ok"):
            raise SendError(
                f"登记回调地址失败：{hook.get('description') if isinstance(hook, dict) else hook}"
            )
        username = str((me.get("result") or {}).get("username") or "")
        return {"bot_username": username}
