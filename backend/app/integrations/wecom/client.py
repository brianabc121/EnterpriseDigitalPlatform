"""企业微信服务端 API 客户端（服务商代开发应用，设计文档 §3.1、§7.4）。

- 模板凭证：企业微信每 10 分钟向指令回调推送 suite_ticket，平台保存在 Redis；
  suite_access_token 用模板 ID、Secret 和 suite_ticket 换取。
- 企业凭证：代开发应用的 Secret 就是授权企业的永久授权码，access_token 用 gettoken 换取。
- 凭证和 JS-SDK ticket 按有效期（提前 5 分钟）缓存在 Redis，多个进程共用；接口返回凭证失效类
  错误时换一次新凭证再重试。
- errcode 为 0 以外的返回抛出 WeComError；网络错误、HTTP 错误和"系统繁忙"（-1）抛出
  WeComUnavailable，调用方可以稍后重试。
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.message import Message
from typing import Any

import httpx
from redis.asyncio import Redis

from app.observability import metrics

logger = logging.getLogger(__name__)

_KEY_PREFIX = "edp:wecom"
# 凭证失效类错误：access_token 无效或过期、suite_access_token 无效或过期、凭证不合法。
_TOKEN_ERRORS = frozenset({40001, 40014, 42001, 40082, 42009})
_SYSTEM_BUSY = -1
_EARLY_REFRESH_SECONDS = 300


class WeComError(Exception):
    """企业微信返回了业务错误（errcode 非 0）。"""

    def __init__(self, errcode: int, errmsg: str, path: str = "") -> None:
        super().__init__(f"{path}: {errcode} {errmsg}".strip(": "))
        self.errcode = errcode
        self.errmsg = errmsg
        self.path = path


class WeComUnavailable(WeComError):
    """网络或服务端故障，可以稍后重试。"""

    def __init__(self, message: str, path: str = "") -> None:
        super().__init__(_SYSTEM_BUSY, message, path)


class SuiteTicketMissing(WeComError):
    """还没有收到 suite_ticket（企业微信每 10 分钟推送一次）。"""

    def __init__(self) -> None:
        super().__init__(0, "suite_ticket not received yet", "get_suite_token")


@dataclass(frozen=True)
class Media:
    data: bytes
    content_type: str
    filename: str | None


CorpSecretLoader = Callable[[str], Awaitable[str]]


class WeComClient:
    def __init__(
        self,
        *,
        base_url: str,
        suite_id: str,
        suite_secret: str,
        redis: Redis,
        corp_secret: CorpSecretLoader,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.suite_id = suite_id
        self._suite_secret = suite_secret
        self._redis = redis
        self._corp_secret = corp_secret
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- 凭证 ----

    def _key(self, *parts: str) -> str:
        return ":".join((_KEY_PREFIX, *parts))

    async def save_suite_ticket(self, ticket: str) -> None:
        await self._redis.set(self._key("suite_ticket", self.suite_id), ticket)

    async def suite_ticket(self) -> str | None:
        value = await self._redis.get(self._key("suite_ticket", self.suite_id))
        return value.decode() if isinstance(value, bytes) else value

    async def _cached(
        self, key: str, fetch: Callable[[], Awaitable[tuple[str, int]]], *, refresh: bool
    ) -> str:
        if not refresh:
            value = await self._redis.get(key)
            if value:
                return value.decode() if isinstance(value, bytes) else str(value)
        value, expires_in = await fetch()
        ttl = max(60, int(expires_in) - _EARLY_REFRESH_SECONDS)
        await self._redis.set(key, value, ex=ttl)
        return value

    async def suite_token(self, *, refresh: bool = False) -> str:
        async def fetch() -> tuple[str, int]:
            ticket = await self.suite_ticket()
            if not ticket:
                raise SuiteTicketMissing()
            data = await self._send(
                "POST",
                "/cgi-bin/service/get_suite_token",
                json={
                    "suite_id": self.suite_id,
                    "suite_secret": self._suite_secret,
                    "suite_ticket": ticket,
                },
            )
            return data["suite_access_token"], int(data.get("expires_in", 7200))

        return await self._cached(self._key("suite_token", self.suite_id), fetch, refresh=refresh)

    async def corp_token(self, corp_id: str, *, refresh: bool = False) -> str:
        async def fetch() -> tuple[str, int]:
            secret = await self._corp_secret(corp_id)
            data = await self._send(
                "GET", "/cgi-bin/gettoken", params={"corpid": corp_id, "corpsecret": secret}
            )
            return data["access_token"], int(data.get("expires_in", 7200))

        return await self._cached(self._key("corp_token", corp_id), fetch, refresh=refresh)

    async def corp_ticket(self, corp_id: str, kind: str) -> str:
        """JS-SDK 的 ticket：kind 为 jsapi（企业，wx.config）或 agent（应用，wx.agentConfig）。"""

        async def fetch() -> tuple[str, int]:
            if kind == "jsapi":
                data = await self.corp_call(corp_id, "GET", "/cgi-bin/get_jsapi_ticket")
            else:
                data = await self.corp_call(
                    corp_id, "GET", "/cgi-bin/ticket/get", params={"type": "agent_config"}
                )
            return data["ticket"], int(data.get("expires_in", 7200))

        return await self._cached(self._key("ticket", kind, corp_id), fetch, refresh=False)

    async def forget_corp(self, corp_id: str) -> None:
        """授权取消或永久授权码重置后，丢弃缓存的企业凭证。"""
        await self._redis.delete(
            self._key("corp_token", corp_id),
            self._key("ticket", "jsapi", corp_id),
            self._key("ticket", "agent", corp_id),
        )

    # ---- 调用 ----

    async def suite_call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """以模板身份（suite_access_token）调用服务商接口。"""
        for attempt in (0, 1):
            token = await self.suite_token(refresh=attempt == 1)
            try:
                return await self._send(
                    method, path, params={**(params or {}), "suite_access_token": token}, json=json
                )
            except WeComError as exc:
                if attempt == 0 and exc.errcode in _TOKEN_ERRORS:
                    continue
                raise
        raise AssertionError("unreachable")

    async def corp_call(
        self,
        corp_id: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """以授权企业的代开发应用身份（access_token）调用。"""
        for attempt in (0, 1):
            token = await self.corp_token(corp_id, refresh=attempt == 1)
            try:
                return await self._send(
                    method, path, params={**(params or {}), "access_token": token}, json=json
                )
            except WeComError as exc:
                if attempt == 0 and exc.errcode in _TOKEN_ERRORS:
                    continue
                raise
        raise AssertionError("unreachable")

    async def download_media(self, corp_id: str, media_id: str) -> Media:
        """获取临时素材（3 天内有效）。成功时返回文件内容，失败时返回 JSON 错误。"""
        for attempt in (0, 1):
            token = await self.corp_token(corp_id, refresh=attempt == 1)
            response = await self._request(
                "GET", "/cgi-bin/media/get", params={"access_token": token, "media_id": media_id}
            )
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            if content_type in ("application/json", "text/plain"):
                try:
                    self._check("/cgi-bin/media/get", response.json())
                except WeComError as exc:
                    if attempt == 0 and exc.errcode in _TOKEN_ERRORS:
                        continue
                    raise
                except ValueError:
                    pass
            return Media(
                data=response.content,
                content_type=content_type or "application/octet-stream",
                filename=_filename(response.headers.get("content-disposition", "")),
            )
        raise AssertionError("unreachable")

    async def upload_media(
        self, corp_id: str, kind: str, filename: str, data: bytes, content_type: str
    ) -> str:
        """上传临时素材，返回 media_id。kind：image、voice、video、file。"""
        for attempt in (0, 1):
            token = await self.corp_token(corp_id, refresh=attempt == 1)
            response = await self._request(
                "POST",
                "/cgi-bin/media/upload",
                params={"access_token": token, "type": kind},
                files={"media": (filename, data, content_type)},
            )
            try:
                return str(self._check("/cgi-bin/media/upload", _json(response))["media_id"])
            except WeComError as exc:
                if attempt == 0 and exc.errcode in _TOKEN_ERRORS:
                    continue
                raise
        raise AssertionError("unreachable")

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._request(method, path, params=params, json=json)
        return self._check(path, _json(response))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, path, params=params, json=json, files=files)
        except httpx.HTTPError as exc:
            metrics.WECOM_API_ERRORS.labels(_api_name(path), str(_SYSTEM_BUSY)).inc()
            raise WeComUnavailable(f"{type(exc).__name__}: {exc}", path) from exc
        if response.status_code >= 400:
            metrics.WECOM_API_ERRORS.labels(_api_name(path), str(_SYSTEM_BUSY)).inc()
            raise WeComUnavailable(f"HTTP {response.status_code}", path)
        return response

    @staticmethod
    def _check(path: str, data: dict[str, Any]) -> dict[str, Any]:
        errcode = int(data.get("errcode") or 0)
        if errcode:
            metrics.WECOM_API_ERRORS.labels(_api_name(path), str(errcode)).inc()
        if errcode == _SYSTEM_BUSY:
            raise WeComUnavailable(str(data.get("errmsg") or "system busy"), path)
        if errcode:
            raise WeComError(errcode, str(data.get("errmsg") or ""), path)
        return data


def _api_name(path: str) -> str:
    """指标里的接口名：去掉公共前缀，如 /cgi-bin/kf/send_msg → kf/send_msg。"""
    return path.removeprefix("/cgi-bin/").strip("/")[:64] or "unknown"


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise WeComUnavailable("response is not json", str(response.request.url.path)) from exc
    if not isinstance(data, dict):
        raise WeComUnavailable("response is not an object", str(response.request.url.path))
    return data


def _filename(disposition: str) -> str | None:
    if not disposition:
        return None
    message = Message()
    message["content-disposition"] = disposition
    name = message.get_filename()
    return name.strip('"') if name else None
