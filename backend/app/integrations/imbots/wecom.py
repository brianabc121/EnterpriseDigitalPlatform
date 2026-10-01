"""企业微信智能机器人（企业在管理后台创建，填回调 URL、Token、EncodingAESKey）。

- 验签、加解密与企业微信其他回调一致（integrations/wecom/crypto.py）：GET 验证地址时解密 echostr；
  POST 的回调体是 JSON {"encrypt": ...}（也兼容 XML），明文是 JSON：msgtype、text.content、
  from.userid、chatid、chattype（single / group）、msgid、response_url。
- 回复：向回调里的 response_url POST 文字（有效期有限）。智能机器人没有主动发消息的接口，
  主动通知经已授权的代开发应用的应用消息发送（wecom/notify.py）。
- 群里机器人只收到 @ 它的消息。真实联调以官方文档为准（设计文档附录 A）。
"""

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
from app.integrations.wecom.crypto import CallbackCrypto, CallbackError, parse_xml

SPEC = ProviderSpec(
    provider="wecom",
    name="企业微信",
    config_fields=(FieldSpec("bot_id", "机器人 ID", "可选：企业微信分配的 aibotid", False),),
    secret_fields=(
        FieldSpec("token", "Token", "智能机器人 → 接收消息 → Token"),
        FieldSpec("encoding_aes_key", "EncodingAESKey", "43 个字符"),
    ),
    notes=(
        "企业微信管理后台 → 应用管理 → 智能机器人 → 创建，接收消息的 URL 填回调地址。",
        "成员已经在「企业微信 → 成员绑定」里对应到员工的，不需要绑定码。",
        "主动通知经代开发应用的应用消息发送；机器人在群里只收到 @ 它的消息。",
    ),
    notify=False,
    groups="只收到 @ 机器人的消息",
)


def _crypto(bot: BotContext) -> CallbackCrypto:
    try:
        return CallbackCrypto(bot.secret("token"), bot.secret("encoding_aes_key"))
    except ValueError as exc:
        raise Rejected(str(exc)) from exc


def _plain(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("<"):
        try:
            return parse_xml(stripped)
        except CallbackError as exc:
            raise Rejected(str(exc)) from exc
    try:
        data = json.loads(stripped or "{}")
    except json.JSONDecodeError as exc:
        raise Rejected("plaintext is not json") from exc
    return data if isinstance(data, dict) else {}


class WecomBotAdapter:
    provider = "wecom"

    async def receive(self, request: WebhookRequest, bot: BotContext) -> Inbound:
        crypto = _crypto(bot)
        query = request.query
        try:
            if request.method == "GET":
                plaintext, _ = crypto.verify_url(
                    msg_signature=query.get("msg_signature", ""),
                    timestamp=query.get("timestamp", ""),
                    nonce=query.get("nonce", ""),
                    echostr=query.get("echostr", ""),
                )
                return Inbound(response=WebhookResponse(plaintext))
            body = request.body.decode("utf-8", errors="replace")
            outer = _plain(body)
            encrypted = outer.get("encrypt") or outer.get("Encrypt")
            if not isinstance(encrypted, str) or not encrypted:
                raise Rejected("missing encrypt")
            crypto.verify(
                query.get("msg_signature", ""),
                query.get("timestamp", ""),
                query.get("nonce", ""),
                encrypted,
            )
            plaintext, _ = crypto.decrypt(encrypted)
        except CallbackError as exc:
            raise Rejected(str(exc)) from exc
        payload = _plain(plaintext)
        msgtype = payload.get("msgtype") or payload.get("MsgType")
        if msgtype != "text":
            return Inbound()
        text_field = payload.get("text")
        text = (
            str(text_field.get("content") or "")
            if isinstance(text_field, dict)
            else str(payload.get("Content") or "")
        ).strip()
        if not text:
            return Inbound()
        sender = payload.get("from") or {}
        userid = str(
            sender.get("userid") if isinstance(sender, dict) else payload.get("FromUserName") or ""
        )
        chattype = str(payload.get("chattype") or "single")
        chat_id = str(payload.get("chatid") or "")
        is_group = chattype == "group"
        reply_to = {}
        if payload.get("response_url"):
            reply_to["response_url"] = str(payload["response_url"])
        return Inbound(
            messages=[
                InboundMessage(
                    external_message_id=str(payload.get("msgid") or payload.get("MsgId") or ""),
                    chat_type="group" if is_group else "private",
                    chat_id=chat_id if is_group else userid,
                    chat_name="",
                    sender_id=userid,
                    sender_name="",
                    text=text,
                    sent_at=utc_from_epoch(payload.get("create_time") or payload.get("CreateTime")),
                    mentioned=is_group,
                    reply_to=reply_to,
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
        url = (reply_to or {}).get("response_url")
        if not url:
            raise SendError("企业微信智能机器人不能主动发消息；通知经代开发应用的应用消息发送")
        try:
            response = await http.post(url, json={"msgtype": "text", "text": {"content": text}})
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SendError(f"企业微信请求失败：{exc}") from exc
        if not isinstance(data, dict) or data.get("errcode", 0) != 0:
            raise SendError(
                f"企业微信拒绝：{data.get('errmsg') if isinstance(data, dict) else data}"
            )

    async def setup(
        self, http: httpx.AsyncClient, settings: Settings, bot: BotContext
    ) -> dict[str, Any]:
        check_fields(bot, SPEC)
        _crypto(bot)
        return {}
