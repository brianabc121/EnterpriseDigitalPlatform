"""接口传输加密（设计文档 §25.15）：握手、加密的请求和响应、防篡改和防重放、强制加密。"""

import base64
import hashlib
import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.modules.transport import crypto
from tests.desk import Desk
from tests.factories import ADMIN_PASSWORD
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.transport_client import SealedClient


@pytest.fixture
async def desk(
    app: FastAPI,
    client: httpx.AsyncClient,
    fake_im: FakeOpenIM,
    settings: Settings,
    database_urls: DatabaseUrls,
) -> Desk:
    return await Desk(app, client, fake_im, settings, database_urls).open()


@pytest.fixture
async def sealed(desk: Desk) -> SealedClient:
    client = SealedClient(desk.client)
    await client.handshake()
    return client


def code(response: httpx.Response) -> str:
    body: dict[str, Any] = response.json()
    return str(body["error"]["code"])


def test_vectors_shared_with_the_frontend() -> None:
    # 与 frontend/packages/api-client/src/transport.test.ts 的测试向量相同。
    aad = crypto.request_aad(
        "GET", "/api/v1/me", "session-123", 1700000000, "nonce-abcdefghijklmn", "meta"
    )
    assert aad == b"edp1|req|GET|/api/v1/me|session-123|1700000000|nonce-abcdefghijklmn|meta"
    plain = b'{"q":"a=1","h":{"authorization":"Bearer t"}}'
    sealed = crypto.seal(bytes(range(32)), plain, aad, iv=bytes(range(12)))
    assert crypto.b64url(sealed) == (
        "AAECAwQFBgcICQoLPCCnOf_Hoya8Y7up2ctCFqG38kCYFC0VQgaR7HIHIogjUsud3aRguACGApBtMz3RoEUA"
        "-kwsEoxWH59C"
    )
    assert crypto.open_sealed(bytes(range(32)), sealed, aad) == plain
    keys = crypto.derive_keys(bytes([7] * 32), "session-123")
    assert keys.request.hex() == "b60ae179503083f959bce425948f4d51dd1a66cf2d122b165c9cfbcb44103071"
    assert keys.response.hex() == "351dda0c995386a2536da38d7a687696c9dd6bc90a04d7cb18ba793f7d788150"
    point = bytes([4]) + bytes(64)
    transcript = crypto.transcript(point, point, "session-123", 1700000000)
    assert hashlib.sha256(transcript).hexdigest() == (
        "9cc22b92948df5e5c477265b4435df2ad9fefa3548c7f9c9225902858a456492"
    )
    with pytest.raises(crypto.SealError):
        crypto.open_sealed(bytes(range(32)), sealed, aad + b"x")


async def test_sealed_requests_and_responses(desk: Desk, sealed: SealedClient) -> None:
    me = await sealed.request("GET", "/api/v1/me", headers=desk.admin)
    assert me.status_code == 200 and me.json()["username"] == "admin"

    created = await sealed.request(
        "POST", "/api/v1/customers", headers=desk.admin, json={"display_name": "李女士"}
    )
    assert created.status_code == 201, created.text
    # 查询参数在密文里。
    found = await sealed.request(
        "GET", "/api/v1/customers", headers=desk.admin, params={"q": "李女士"}
    )
    assert [c["display_name"] for c in found.json()["items"]] == ["李女士"]
    # 业务错误照常返回状态码，内容也是加密的。
    missing = await sealed.request(
        "GET", f"/api/v1/customers/{'0' * 8}-0000-0000-0000-{'0' * 12}", headers=desk.admin
    )
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    # 下载的文件：Content-Type、Content-Disposition 跟着内容一起加密。
    path, headers, _, nonce = sealed.seal("GET", "/api/v1/products/template", headers=desk.admin)
    raw = await desk.client.get(path, headers=headers)
    assert raw.headers["content-type"] == crypto.SEALED_TYPE
    assert "content-disposition" not in raw.headers and raw.headers["cache-control"] == "no-store"
    template = sealed.open("GET", path, nonce, raw)
    assert template.headers["content-disposition"].endswith("product-template.xlsx")
    assert template.content[:2] == b"PK"  # xlsx 是 zip
    # 大一些的请求体（商品表格按 base64 上传）。
    upload = await sealed.request(
        "POST",
        "/api/v1/products/imports",
        headers=desk.admin,
        json={
            "filename": "products.xlsx",
            "content_base64": base64.b64encode(template.content).decode(),
        },
    )
    assert upload.status_code == 201, upload.text


async def test_tampered_moved_replayed_or_stale_requests_are_rejected(
    desk: Desk, sealed: SealedClient
) -> None:
    path, headers, _, _ = sealed.seal("GET", "/api/v1/me", headers=desk.admin)
    assert (await desk.client.get(path, headers=headers)).status_code == 200
    replay = await desk.client.get(path, headers=headers)
    assert (replay.status_code, code(replay)) == (400, "transport_replay")

    # 挪到别的接口：附加认证数据不对。
    path, headers, _, _ = sealed.seal("GET", "/api/v1/me", headers=desk.admin)
    moved = await desk.client.get("/api/v1/customers", headers=headers)
    assert (moved.status_code, code(moved)) == (400, "transport_invalid")

    # 改动密文的一个字节。
    path, headers, _, _ = sealed.seal("GET", "/api/v1/me", headers=desk.admin)
    ts, nonce, blob = headers[crypto.SEALED_HEADER].split(".")
    data = bytearray(crypto.unb64url(blob))
    data[-1] ^= 1
    headers[crypto.SEALED_HEADER] = f"{ts}.{nonce}.{crypto.b64url(bytes(data))}"
    tampered = await desk.client.get(path, headers=headers)
    assert (tampered.status_code, code(tampered)) == (400, "transport_invalid")

    # 请求体被改动。
    path, headers, body, _ = sealed.seal(
        "POST",
        "/api/v1/customers",
        headers={**desk.admin, "content-type": "application/json"},
        body=b'{"display_name": "x"}',
    )
    assert body is not None
    changed = await desk.client.post(
        path, headers=headers, content=body[:-1] + bytes([body[-1] ^ 1])
    )
    assert (changed.status_code, code(changed)) == (400, "transport_invalid")

    # 时间差太多。
    path, headers, _, _ = sealed.seal(
        "GET", "/api/v1/me", headers=desk.admin, ts=int(time.time()) - 3600
    )
    stale = await desk.client.get(path, headers=headers)
    assert (stale.status_code, code(stale)) == (400, "transport_clock")

    # 会话不存在或已过期：浏览器重新握手后重试。
    other = SealedClient(desk.client)
    other.keys = sealed.keys
    other.session = crypto.b64url(b"\x00" * 18)
    path, headers, _, _ = other.seal("GET", "/api/v1/me", headers=desk.admin)
    expired = await desk.client.get(path, headers=headers)
    assert (expired.status_code, code(expired)) == (428, "transport_session")


async def test_required_mode_rejects_plain_requests_except_excluded_paths(
    app: FastAPI, desk: Desk
) -> None:
    app.state.settings.transport_encryption = "required"
    plain = await desk.client.get("/api/v1/me", headers=desk.admin)
    assert (plain.status_code, code(plain)) == (426, "transport_required")

    key = (await desk.client.get("/api/v1/transport/key")).json()
    assert key["mode"] == "required"
    sealed = SealedClient(desk.client)
    await sealed.handshake()
    # 登录（密码在密文里），再用新的令牌访问。
    login = await sealed.request(
        "POST",
        "/api/v1/auth/login",
        json={"tenant_code": desk.code, "username": "admin", "password": ADMIN_PASSWORD},
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    me = await sealed.request("GET", "/api/v1/me", headers={"authorization": f"Bearer {token}"})
    assert me.status_code == 200

    # 不加密的：健康检查、企业系统接口（有自己的密钥）、回调。
    assert (await desk.client.get("/healthz")).status_code == 200
    assert (await desk.client.get("/open/v1/orders")).status_code == 401
    assert (await desk.client.post("/hooks/openim/callback", content=b"{}")).status_code != 426


async def test_handshake_rejects_bad_keys(desk: Desk) -> None:
    bad = await desk.client.post(
        "/api/v1/transport/handshake", json={"client_key": crypto.b64url(bytes([4]) + bytes(64))}
    )
    assert bad.status_code == 422
