"""钉钉企业内部应用的机器人（HTTP 回调模式）。

- 回调带 timestamp 和 sign 头：sign = Base64(HMAC-SHA256(AppSecret, "timestamp\\nAppSecret"))，
  时间戳与当前相差超过 1 小时的拒绝。
- 回复：回调里有 sessionWebhook（一段时间内有效），直接向它 POST 文字；主动通知员工用
  机器人单聊接口 oToMessages/batchSend（robotCode + userIds），向群发用 groupMessages/send。
- 群里机器人只收到 @ 它的消息。
"""

import base64
import hashlib
import hmac
import json
import time
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
    check_fields,
    utc_from_epoch,
)

SPEC = ProviderSpec(
    provider="dingtalk",
    name="钉钉",
    config_fields=(
        FieldSpec("app_key", "AppKey (Client ID)", "钉钉开发者后台 → 应用 → 凭证与基础信息"),
        FieldSpec("robot_code", "RobotCode", "机器人的 RobotCode，通常与 AppKey 相同", False),
    ),
    secret_fields=(FieldSpec("app_secret", "AppSecret (Client Secret)"),),
    notes=(
        "在应用里添加「机器人」能力，消息接收模式选 HTTP 模式，消息接收地址填回调地址。",
        "权限：机器人发送消息（qyapi_robot_sendmsg）、企业内机器人发送消息权限。",
        "员工在钉钉里找到这个机器人发消息即可对话；群里只有 @ 机器人的消息会送到平台。",
    ),
    notify=True,
    groups="只收到 @ 机器人的消息",
)

_SKEW = 3600
_TOKEN_TTL = 90 * 60
_tokens: dict[str, tuple[float, str]] = {}


def sign(app_secret: str, timestamp: str) -> str:
    digest = hmac.new(
        app_secret.encode(), f"{timestamp}\n{app_secret}".encode(), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode()


class DingTalkAdapter:
    provider = "dingtalk"

    def _base(self, settings: Settings) -> str:
        return settings.dingtalk_api_url.rstrip("/")

    async def receive(self, request: WebhookRequest, bot: BotContext) -> Inbound:
        if request.method != "POST":
            raise Rejected("dingtalk only posts messages")
        timestamp = request.header("timestamp")
        given = request.header("sign")
        try:
            skew = abs(time.time() * 1000 - float(timestamp))
        except ValueError as exc:
            raise Rejected("bad timestamp") from exc
        if skew > _SKEW * 1000:
            raise Rejected("timestamp too old")
        if not given or not hmac.compare_digest(sign(bot.secret("app_secret"), timestamp), given):
            raise Rejected("signature mismatch")
        try:
            payload = json.loads(request.body or b"{}")
        except json.JSONDecodeError as exc:
            raise Rejected("body is not json") from exc
        if not isinstance(payload, dict) or payload.get("msgtype") != "text":
            return Inbound()
        text = str((payload.get("text") or {}).get("content") or "").strip()
        if not text:
            return Inbound()
        is_group = str(payload.get("conversationType")) == "2"
        reply_to = {}
        if payload.get("sessionWebhook"):
            reply_to["session_webhook"] = str(payload["sessionWebhook"])
        return Inbound(
            messages=[
                InboundMessage(
                    external_message_id=str(payload.get("msgId") or ""),
                    chat_type="group" if is_group else "private",
                    chat_id=str(payload.get("conversationId") or ""),
                    chat_name=str(payload.get("conversationTitle") or ""),
                    sender_id=str(payload.get("senderStaffId") or payload.get("senderId") or ""),
                    sender_name=str(payload.get("senderNick") or ""),
                    text=text,
                    sent_at=utc_from_epoch(payload.get("createAt"), millis=True),
                    mentioned=is_group,
                    reply_to=reply_to,
                )
            ]
        )

    async def _token(self, http: httpx.AsyncClient, settings: Settings, bot: BotContext) -> str:
        app_key = bot.setting("app_key")
        cached = _tokens.get(app_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            response = await http.post(
                f"{self._base(settings)}/v1.0/oauth2/accessToken",
                json={"appKey": app_key, "appSecret": bot.secret("app_secret")},
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SendError(f"钉钉请求失败：{exc}") from exc
        if not isinstance(data, dict) or not data.get("accessToken"):
            raise SendError(f"钉钉拒绝：{data.get('message') if isinstance(data, dict) else data}")
        token = str(data["accessToken"])
        _tokens[app_key] = (time.monotonic() + _TOKEN_TTL, token)
        return token

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
        webhook = (reply_to or {}).get("session_webhook")
        if webhook:
            try:
                response = await http.post(
                    webhook, json={"msgtype": "text", "text": {"content": text}}
                )
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise SendError(f"钉钉请求失败：{exc}") from exc
            if not isinstance(data, dict) or data.get("errcode", 0) != 0:
                raise SendError(
                    f"钉钉拒绝：{data.get('errmsg') if isinstance(data, dict) else data}"
                )
            return
        token = await self._token(http, settings, bot)
        robot_code = bot.setting("robot_code") or bot.setting("app_key")
        body: dict[str, Any] = {
            "robotCode": robot_code,
            "msgKey": "sampleText",
            "msgParam": json.dumps({"content": text}, ensure_ascii=False),
        }
        if kind == "chat":
            path, body["openConversationId"] = "/v1.0/robot/groupMessages/send", target
        else:
            path, body["userIds"] = "/v1.0/robot/oToMessages/batchSend", [target]
        try:
            response = await http.post(
                f"{self._base(settings)}{path}",
                headers={"x-acs-dingtalk-access-token": token},
                json=body,
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SendError(f"钉钉请求失败：{exc}") from exc
        if response.status_code >= 400 or not isinstance(data, dict) or data.get("code"):
            _tokens.pop(bot.setting("app_key"), None)
            raise SendError(f"钉钉拒绝：{data.get('message') if isinstance(data, dict) else data}")

    async def setup(
        self, http: httpx.AsyncClient, settings: Settings, bot: BotContext
    ) -> dict[str, Any]:
        check_fields(bot, SPEC)
        await self._token(http, settings, bot)
        return {}
