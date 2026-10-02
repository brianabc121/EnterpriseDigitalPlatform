"""飞书自建应用的机器人（事件订阅 v2，im.message.receive_v1）。

- 配置回调地址时飞书发 url_verification，原样返回 challenge；之后每个事件的 header.token 必须与
  Verification Token 一致。配置了 Encrypt Key 时事件体是 AES-256-CBC 加密的（密钥为 Encrypt Key 的
  SHA-256，前 16 字节是 IV），并校验 X-Lark-Signature。
- 发消息：用 App ID / App Secret 换 tenant_access_token（进程内缓存），im/v1/messages 按 chat_id
  （群或私聊会话）或 open_id（员工）发送。
- 群里要收到全部消息需要申请"获取群组中所有消息"权限；否则只收到 @ 机器人的消息。
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

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
    WebhookResponse,
    check_fields,
    utc_from_epoch,
)

SPEC = ProviderSpec(
    provider="feishu",
    name="飞书",
    config_fields=(
        FieldSpec("app_id", "App ID", "飞书开放平台 → 自建应用 → 凭证与基础信息"),
        FieldSpec("bot_open_id", "机器人 open_id", "可选：填了之后只有 @ 这个机器人才算 @", False),
    ),
    secret_fields=(
        FieldSpec("app_secret", "App Secret"),
        FieldSpec("verification_token", "Verification Token", "事件订阅 → 加密策略"),
        FieldSpec("encrypt_key", "Encrypt Key", "可选；填了之后事件体加密传输", False),
    ),
    notes=(
        "在应用的「事件订阅」里填回调地址，订阅 im.message.receive_v1；"
        "开启「机器人」能力并发布版本。",
        "权限：im:message、im:message:send_as_bot、im:chat:readonly；群里要收到全部消息需要"
        "「获取群组中所有消息」（im:message.group_msg）。",
    ),
    notify=True,
    groups="申请「获取群组中所有消息」权限后收到全部，否则只收到 @ 机器人的消息",
)

_TOKEN_TTL = 90 * 60
_tokens: dict[str, tuple[float, str]] = {}


def decrypt(encrypt_key: str, encrypted: str) -> str:
    key = hashlib.sha256(encrypt_key.encode()).digest()
    try:
        data = base64.b64decode(encrypted, validate=True)
    except ValueError as exc:
        raise Rejected("ciphertext is not base64") from exc
    if len(data) < 32 or len(data) % 16:
        raise Rejected("ciphertext has an invalid length")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(data[:16])).decryptor()
    raw = decryptor.update(data[16:]) + decryptor.finalize()
    pad = raw[-1]
    if not 1 <= pad <= 16:
        raise Rejected("invalid padding")
    try:
        return raw[:-pad].decode()
    except UnicodeDecodeError as exc:
        raise Rejected("plaintext is not utf-8") from exc


def encrypt(encrypt_key: str, plaintext: str, iv: bytes | None = None) -> str:
    """模拟服务和测试用：按飞书的方式加密事件体。"""
    import secrets

    key = hashlib.sha256(encrypt_key.encode()).digest()
    iv = iv or secrets.token_bytes(16)
    raw = plaintext.encode()
    pad = 16 - len(raw) % 16
    raw += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + encryptor.update(raw) + encryptor.finalize()).decode()


def signature(encrypt_key: str, timestamp: str, nonce: str, body: bytes) -> str:
    return hashlib.sha256(
        timestamp.encode() + nonce.encode() + encrypt_key.encode() + body
    ).hexdigest()


class FeishuAdapter:
    provider = "feishu"

    def _base(self, settings: Settings) -> str:
        return settings.feishu_api_url.rstrip("/")

    async def receive(self, request: WebhookRequest, bot: BotContext) -> Inbound:
        if request.method != "POST":
            raise Rejected("feishu only posts events")
        try:
            outer = json.loads(request.body or b"{}")
        except json.JSONDecodeError as exc:
            raise Rejected("body is not json") from exc
        if not isinstance(outer, dict):
            raise Rejected("body is not an object")
        encrypt_key = bot.secret("encrypt_key")
        if "encrypt" in outer:
            if not encrypt_key:
                raise Rejected("encrypted event without encrypt key")
            try:
                payload = json.loads(decrypt(encrypt_key, str(outer["encrypt"])))
            except json.JSONDecodeError as exc:
                raise Rejected("decrypted body is not json") from exc
        else:
            payload = outer
        if encrypt_key and request.header("X-Lark-Signature"):
            expected = signature(
                encrypt_key,
                request.header("X-Lark-Request-Timestamp"),
                request.header("X-Lark-Request-Nonce"),
                request.body,
            )
            if not hmac.compare_digest(expected, request.header("X-Lark-Signature")):
                raise Rejected("signature mismatch")
        token = bot.secret("verification_token")
        if payload.get("type") == "url_verification":
            if not hmac.compare_digest(str(payload.get("token") or ""), token):
                raise Rejected("verification token mismatch")
            return Inbound(
                response=WebhookResponse(
                    json.dumps({"challenge": payload.get("challenge")}), "application/json"
                )
            )
        header = payload.get("header") or {}
        if not hmac.compare_digest(str(header.get("token") or ""), token):
            raise Rejected("verification token mismatch")
        if header.get("event_type") != "im.message.receive_v1":
            return Inbound()
        event = payload.get("event") or {}
        message = event.get("message") or {}
        if message.get("message_type") != "text":
            return Inbound()
        try:
            text = str(json.loads(message.get("content") or "{}").get("text") or "")
        except json.JSONDecodeError:
            text = ""
        mentions = message.get("mentions") or []
        bot_open_id = bot.setting("bot_open_id")
        mentioned = False
        for mention in mentions:
            open_id = str((mention.get("id") or {}).get("open_id") or "")
            if not bot_open_id or open_id == bot_open_id:
                mentioned = True
            text = text.replace(str(mention.get("key") or ""), f"@{mention.get('name') or ''}")
        text = text.strip()
        if not text:
            return Inbound()
        sender = (event.get("sender") or {}).get("sender_id") or {}
        chat_type: ChatType = "private" if message.get("chat_type") == "p2p" else "group"
        return Inbound(
            messages=[
                InboundMessage(
                    external_message_id=str(message.get("message_id") or ""),
                    chat_type=chat_type,
                    chat_id=str(message.get("chat_id") or ""),
                    chat_name="",
                    sender_id=str(sender.get("open_id") or ""),
                    sender_name="",
                    text=text,
                    sent_at=utc_from_epoch(message.get("create_time"), millis=True),
                    mentioned=mentioned,
                )
            ]
        )

    async def _token(self, http: httpx.AsyncClient, settings: Settings, bot: BotContext) -> str:
        app_id = bot.setting("app_id")
        cached = _tokens.get(app_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            response = await http.post(
                f"{self._base(settings)}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": bot.secret("app_secret")},
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SendError(f"飞书请求失败：{exc}") from exc
        if (
            not isinstance(data, dict)
            or data.get("code") not in (0, None)
            or not data.get("tenant_access_token")
        ):
            raise SendError(f"飞书拒绝：{data.get('msg') if isinstance(data, dict) else data}")
        token = str(data["tenant_access_token"])
        _tokens[app_id] = (time.monotonic() + _TOKEN_TTL, token)
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
        token = await self._token(http, settings, bot)
        receive_id_type = "chat_id" if kind == "chat" else "open_id"
        try:
            response = await http.post(
                f"{self._base(settings)}/open-apis/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": target,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SendError(f"飞书请求失败：{exc}") from exc
        if not isinstance(data, dict) or data.get("code") != 0:
            _tokens.pop(bot.setting("app_id"), None)
            raise SendError(f"飞书拒绝：{data.get('msg') if isinstance(data, dict) else data}")

    async def setup(
        self, http: httpx.AsyncClient, settings: Settings, bot: BotContext
    ) -> dict[str, Any]:
        check_fields(bot, SPEC)
        await self._token(http, settings, bot)
        return {}
