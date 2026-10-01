"""WhatsApp Business Platform（Cloud API）。

- 配置 Webhook 时 Meta 发 GET 验证：hub.verify_token 与设置的一致时原样返回 hub.challenge。
- 每次回调带 X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(App Secret, 请求体)。
- 发消息：POST /{phone_number_id}/messages，员工最后一次发消息后 24 小时内可以直接发文字，
  窗口外需要模板消息（本期不发，记为发送失败）。不支持群。
"""

import hashlib
import hmac
import json
from typing import Any

import httpx

from app.core.config import Settings
from app.integrations.imbots.base import (
    BotContext,
    FieldSpec,
    Inbound,
    InboundMessage,
    ProviderSpec,
    Rejected,
    SendError,
    TargetKind,
    WebhookRequest,
    WebhookResponse,
    check_fields,
    utc_from_epoch,
)

SPEC = ProviderSpec(
    provider="whatsapp",
    name="WhatsApp",
    config_fields=(
        FieldSpec("phone_number_id", "Phone Number ID", "Meta 开发者后台 → WhatsApp → API 设置"),
    ),
    secret_fields=(
        FieldSpec("access_token", "Access Token", "系统用户的永久令牌"),
        FieldSpec("app_secret", "App Secret", "应用的 App Secret，用于校验回调签名"),
        FieldSpec("verify_token", "Verify Token", "自己设定的一串字符，配置 Webhook 时填同样的值"),
    ),
    notes=(
        "在应用的 WhatsApp → 配置里填回调地址和 Verify Token，订阅 messages 事件。",
        "员工用自己的 WhatsApp 号码给企业号发消息即可对话；24 小时内可以收到助理的通知。",
        "WhatsApp 不支持把机器人拉进群。",
    ),
    notify=True,
    groups="不支持群",
)


def signature(app_secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()


class WhatsAppAdapter:
    provider = "whatsapp"

    async def receive(self, request: WebhookRequest, bot: BotContext) -> Inbound:
        if request.method == "GET":
            if request.query.get("hub.mode") != "subscribe" or not hmac.compare_digest(
                request.query.get("hub.verify_token", ""), bot.secret("verify_token")
            ):
                raise Rejected("verify token mismatch")
            return Inbound(response=WebhookResponse(request.query.get("hub.challenge", "")))
        given = request.header("X-Hub-Signature-256")
        if not given or not hmac.compare_digest(
            signature(bot.secret("app_secret"), request.body), given
        ):
            raise Rejected("signature mismatch")
        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError as exc:
            raise Rejected("body is not json") from exc
        messages: list[InboundMessage] = []
        for entry in (payload.get("entry") or []) if isinstance(payload, dict) else []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                names = {
                    str(c.get("wa_id") or ""): str((c.get("profile") or {}).get("name") or "")
                    for c in value.get("contacts") or []
                }
                for message in value.get("messages") or []:
                    if message.get("type") != "text":
                        continue
                    text = str((message.get("text") or {}).get("body") or "").strip()
                    sender = str(message.get("from") or "")
                    if not text or not sender:
                        continue
                    messages.append(
                        InboundMessage(
                            external_message_id=str(message.get("id") or ""),
                            chat_type="private",
                            chat_id=sender,
                            chat_name="",
                            sender_id=sender,
                            sender_name=names.get(sender, ""),
                            text=text,
                            sent_at=utc_from_epoch(message.get("timestamp")),
                        )
                    )
        return Inbound(messages=messages)

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
                f"{settings.whatsapp_api_url.rstrip('/')}/{bot.setting('phone_number_id')}/messages",
                headers={"Authorization": f"Bearer {bot.secret('access_token')}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": target,
                    "type": "text",
                    "text": {"body": text},
                },
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SendError(f"WhatsApp 请求失败：{exc}") from exc
        if response.status_code >= 400 or not isinstance(data, dict) or data.get("error"):
            error = data.get("error") if isinstance(data, dict) else data
            message = error.get("message") if isinstance(error, dict) else error
            raise SendError(f"WhatsApp 拒绝：{message}")

    async def setup(
        self, http: httpx.AsyncClient, settings: Settings, bot: BotContext
    ) -> dict[str, Any]:
        check_fields(bot, SPEC)
        return {}
