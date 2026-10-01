"""接口传输加密的中间件（设计文档 §25.15）：解开加密的请求（查询参数、请求头和请求体），交给业务
路由处理，再把响应加密。业务代码不用改。

- 只处理 /api/ 和 /platform/ 下的接口。握手本身、带签名的文件链接和企业微信授权的跳转（浏览器
  直接打开，带不了请求头）不加密；企业系统接口（/open/v1）和回调（/hooks）不在这两个前缀下。
- required：没有加密的请求返回 426；optional：照常处理没有加密的请求。
- 加密的请求：会话不存在或已过期返回 428（浏览器重新握手后重试），时间不对、重放、密文不对
  返回 400。这些错误的响应是明文（没有可以加密的密钥）。
- 响应的状态码不变；响应体连同 Content-Type、Content-Disposition 一起加密，附加认证数据绑定
  这次请求的随机数。
"""

import json
import re
import time
from typing import Any

from redis.exceptions import RedisError
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings
from app.core.errors import error_body
from app.modules.transport import crypto
from app.modules.transport.sessions import SessionStore

PROTECTED = ("/api/", "/platform/")
PLAIN = ("/api/v1/transport/", "/api/v1/files/", "/api/v1/wecom/install/callback")
MAX_BODY = 64 * 1024 * 1024
CLOCK_SKEW_SECONDS = 300
NO_BODY_STATUS = frozenset({204, 205, 304})
# 信封里可以带的请求头：认证、内容类型和自定义的 X- 头（转发相关的不行，不能用来伪造来源）。
_ALLOWED_HEADERS = frozenset(
    {"authorization", "content-type", "accept", "accept-language", "idempotency-key"}
)
_BLOCKED_PREFIXES = ("x-forwarded", "x-real-ip", "x-edp-")
# 解开后从原来的请求头里去掉的：加密用的头，以及以信封里的为准的头。
_REPLACED_HEADERS = frozenset(
    {crypto.SESSION_HEADER, crypto.SEALED_HEADER, "content-type", "content-length", "authorization"}
)
# 响应里跟着内容一起加密的头。
_SEALED_RESPONSE_HEADERS = ("content-type", "content-disposition")
# 会话号和随机数（base64url）会用在 Redis 的键里，只接受这些字符。
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,64}")
# 直接发文件的扩展：响应体必须经过这里加密，不让下面的路由绕过。
_BYPASS_EXTENSIONS = ("http.response.pathsend", "http.response.zerocopysend")


def protected(path: str) -> bool:
    return path.startswith(PROTECTED) and not path.startswith(PLAIN)


class TransportError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _invalid() -> TransportError:
    return TransportError(400, "transport_invalid", "加密的请求无效，请刷新页面后重试")


class TransportMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Settings, store: SessionStore) -> None:
        self.app = app
        self.settings = settings
        self.store = store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        mode = self.settings.transport_mode
        if scope["type"] != "http" or mode == "off" or not protected(scope["path"]):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        session = headers.get(crypto.SESSION_HEADER)
        if session is None:
            if mode == "required" and scope["method"] != "OPTIONS":
                await _reply(send, 426, "transport_required", "需要加密传输，请刷新页面后重试")
                return
            await self.app(scope, receive, send)
            return
        try:
            opened = await self._open(scope, receive, headers, session)
        except TransportError as exc:
            await _reply(send, exc.status, exc.code, exc.message)
            return
        if opened is None:  # 客户端已经断开
            return
        inner, inner_receive, keys, nonce = opened
        sealing = _SealingSend(send, keys, scope["method"], _path(scope), session, nonce)
        await self.app(inner, inner_receive, sealing)

    async def _open(
        self, scope: Scope, receive: Receive, headers: Headers, session: str
    ) -> tuple[Scope, Receive, crypto.SessionKeys, str] | None:
        parts = headers.get(crypto.SEALED_HEADER, "").split(".")
        if len(parts) != 3:
            raise _invalid()
        ts_text, nonce, blob = parts
        if not ts_text.isdigit() or not _TOKEN.fullmatch(nonce) or not _TOKEN.fullmatch(session):
            raise _invalid()
        ts = int(ts_text)
        if abs(time.time() - ts) > CLOCK_SKEW_SECONDS:
            raise TransportError(400, "transport_clock", "请求的时间不对，请检查电脑的时间设置")
        try:
            keys, fresh = await self.store.use(session, nonce)
        except RedisError as exc:
            raise TransportError(
                503, "transport_unavailable", "加密服务暂时不可用，请稍后重试"
            ) from exc
        if keys is None:
            raise TransportError(428, "transport_session", "加密会话已过期，请重试")
        if not fresh:
            raise TransportError(400, "transport_replay", "重复的请求")
        method, path = scope["method"], _path(scope)
        try:
            meta = json.loads(
                crypto.open_sealed(
                    keys.request,
                    crypto.unb64url(blob),
                    crypto.request_aad(method, path, session, ts, nonce, "meta"),
                )
            )
        except (crypto.SealError, ValueError) as exc:
            raise _invalid() from exc
        query, extra = _parse_meta(meta)

        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            body += message.get("body", b"")
            if len(body) > MAX_BODY:
                raise TransportError(413, "payload_too_large", "请求的内容太大")
            more = message.get("more_body", False)
        if body:
            try:
                body = crypto.open_sealed(
                    keys.request, body, crypto.request_aad(method, path, session, ts, nonce, "body")
                )
            except crypto.SealError as exc:
                raise _invalid() from exc

        dropped = _REPLACED_HEADERS | set(extra)
        raw_headers = [
            (name, value)
            for name, value in scope["headers"]
            if name.decode("latin-1").lower() not in dropped
        ]
        raw_headers += [(name.encode(), value.encode("latin-1")) for name, value in extra.items()]
        if body:
            raw_headers.append((b"content-length", str(len(body)).encode()))
        inner = dict(scope)
        inner["headers"] = raw_headers
        inner["query_string"] = query.encode("ascii")
        extensions = dict(scope.get("extensions") or {})
        for name in _BYPASS_EXTENSIONS:
            extensions.pop(name, None)
        inner["extensions"] = extensions

        delivered = False

        async def inner_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            # 请求体已经读完：之后只会等到客户端断开（流式响应靠它判断客户端是否还在）。
            return await receive()

        return inner, inner_receive, keys, nonce


def _path(scope: Scope) -> str:
    """浏览器发出的路径（保留百分号编码，与前端 URL.pathname 一致）。"""
    raw = scope.get("raw_path")
    return raw.decode("latin-1") if raw else str(scope["path"])


def _parse_meta(meta: Any) -> tuple[str, dict[str, str]]:
    if not isinstance(meta, dict):
        raise _invalid()
    query = meta.get("q", "")
    headers = meta.get("h", {})
    if not isinstance(query, str) or not query.isascii() or not isinstance(headers, dict):
        raise _invalid()
    extra: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str) or not name.isascii():
            raise _invalid()
        lower = name.lower()
        allowed = lower in _ALLOWED_HEADERS or (
            lower.startswith("x-") and not lower.startswith(_BLOCKED_PREFIXES)
        )
        if not allowed:
            continue
        try:
            value.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise _invalid() from exc
        extra[lower] = value
    return query, extra


class _SealingSend:
    """收齐响应后整体加密再发出（包括流式的导出文件）。"""

    def __init__(
        self,
        send: Send,
        keys: crypto.SessionKeys,
        method: str,
        path: str,
        session: str,
        nonce: str,
    ) -> None:
        self.send = send
        self.keys = keys
        self.method = method
        self.path = path
        self.session = session
        self.nonce = nonce
        self.start: Message | None = None
        self.chunks: list[bytes] = []

    async def __call__(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.start = message
            return
        if message["type"] != "http.response.body" or self.start is None:
            await self.send(message)
            return
        self.chunks.append(message.get("body", b""))
        if message.get("more_body", False):
            return
        await self._finish(self.start, b"".join(self.chunks))

    async def _finish(self, start: Message, body: bytes) -> None:
        status = start["status"]
        headers = MutableHeaders(raw=list(start["headers"]))
        headers["cache-control"] = "no-store"
        if status in NO_BODY_STATUS or self.method == "HEAD":
            await self.send(
                {"type": "http.response.start", "status": status, "headers": headers.raw}
            )
            await self.send({"type": "http.response.body", "body": b""})
            return
        sealed_headers = {
            name: headers[name] for name in _SEALED_RESPONSE_HEADERS if name in headers
        }
        for name in sealed_headers:
            del headers[name]
        plaintext = json.dumps({"h": sealed_headers}, ensure_ascii=False).encode() + b"\n" + body
        sealed = crypto.seal(
            self.keys.response,
            plaintext,
            crypto.response_aad(self.method, self.path, self.session, self.nonce),
        )
        headers["content-type"] = crypto.SEALED_TYPE
        headers["content-length"] = str(len(sealed))
        await self.send({"type": "http.response.start", "status": status, "headers": headers.raw})
        await self.send({"type": "http.response.body", "body": sealed})


async def _reply(send: Send, status: int, code: str, message: str) -> None:
    body = json.dumps(error_body(code, message), ensure_ascii=False).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
