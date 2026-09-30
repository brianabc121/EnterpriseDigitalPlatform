"""测试用的传输加密客户端（设计文档 §25.15）：和前端 transport.ts 相同的格式。"""

import base64
import json as jsonlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from app.modules.transport import crypto


class SealedClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.session = ""
        self.keys = crypto.SessionKeys(b"", b"")

    async def handshake(self) -> dict[str, Any]:
        key = (await self.client.get("/api/v1/transport/key")).json()
        public = serialization.load_der_public_key(base64.b64decode(key["public_key"]))
        assert isinstance(public, ec.EllipticCurvePublicKey)
        private = ec.generate_private_key(ec.SECP256R1())
        raw = private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        response = await self.client.post(
            "/api/v1/transport/handshake", json={"client_key": crypto.b64url(raw)}
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        server = crypto.unb64url(body["server_key"])
        signature = crypto.unb64url(body["signature"])
        der = encode_dss_signature(
            int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
        )
        # 签名不对时抛出 InvalidSignature。
        public.verify(
            der,
            crypto.transcript(raw, server, body["session"], body["expires_at"]),
            ec.ECDSA(hashes.SHA256()),
        )
        shared = private.exchange(
            ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), server)
        )
        self.session = body["session"]
        self.keys = crypto.derive_keys(shared, self.session)
        return body

    def seal(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        ts: int | None = None,
        nonce: str | None = None,
    ) -> tuple[str, dict[str, str], bytes | None, str]:
        """加密一个请求：(不带查询参数的地址, 请求头, 请求体, 随机数)。"""
        parts = urlsplit(url)
        ts = int(time.time()) if ts is None else ts
        nonce = nonce or crypto.b64url(secrets.token_bytes(16))
        meta = {"q": parts.query, "h": dict(headers or {})}
        sealed_meta = crypto.seal(
            self.keys.request,
            jsonlib.dumps(meta).encode(),
            crypto.request_aad(method, parts.path, self.session, ts, nonce, "meta"),
        )
        out = {
            crypto.SESSION_HEADER: self.session,
            crypto.SEALED_HEADER: f"{ts}.{nonce}.{crypto.b64url(sealed_meta)}",
        }
        sealed_body = None
        if body:
            sealed_body = crypto.seal(
                self.keys.request,
                body,
                crypto.request_aad(method, parts.path, self.session, ts, nonce, "body"),
            )
            out["content-type"] = crypto.SEALED_TYPE
        return parts.path, out, sealed_body, nonce

    def open(self, method: str, path: str, nonce: str, response: httpx.Response) -> httpx.Response:
        """解开加密的响应，返回和明文请求一样的 httpx.Response。"""
        if not response.headers.get("content-type", "").startswith(crypto.SEALED_TYPE):
            return response
        plain = crypto.open_sealed(
            self.keys.response,
            response.content,
            crypto.response_aad(method, path, self.session, nonce),
        )
        meta_line, _, body = plain.partition(b"\n")
        headers = {
            k: v for k, v in response.headers.items() if k not in ("content-type", "content-length")
        }
        headers.update(jsonlib.loads(meta_line)["h"])
        return httpx.Response(response.status_code, headers=headers, content=body)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> httpx.Response:
        if params:
            url = f"{url}?{urlencode(params)}"
        inner = dict(headers or {})
        body = content
        if json is not None:
            body = jsonlib.dumps(json).encode()
            inner["content-type"] = "application/json"
        elif content_type:
            inner["content-type"] = content_type
        path, sealed_headers, sealed_body, nonce = self.seal(method, url, headers=inner, body=body)
        response = await self.client.request(
            method, path, headers=sealed_headers, content=sealed_body
        )
        return self.open(method, path, nonce, response)
