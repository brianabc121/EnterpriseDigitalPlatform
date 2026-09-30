"""接口传输加密（设计文档 §25.15）：服务器签名密钥、握手、加密和解密。

- 服务器签名密钥：ECDSA P-256（EDP_TRANSPORT_SIGNING_KEY：PEM，或者 PKCS#8 DER 的 base64；
  生产环境以外没有配置时由 EDP_DATA_ENCRYPTION_KEY 派生）。浏览器用它的公钥验证握手的签名。
- 握手：浏览器和服务器各生成一次性的 ECDH P-256 密钥对，共享密钥经 HKDF-SHA256（盐是会话号）
  派生出请求和响应各一把 AES-256-GCM 密钥。
- 密文：12 字节随机 IV + AES-GCM 密文（含 16 字节认证标签）。附加认证数据绑定方法、路径、
  会话号、时间戳和随机数，密文不能挪到别的请求用。

前端的实现在 frontend/packages/api-client/src/transport.ts，两边的格式必须一致
（tests/test_transport.py 和 transport.test.ts 用同一组固定的测试向量）。
"""

import base64
import binascii
import hashlib
import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import Settings

PROTOCOL = "edp1"
SEALED_TYPE = "application/x-edp-sealed"
SESSION_HEADER = "x-edp-transport"
SEALED_HEADER = "x-edp-sealed"
IV_BYTES = 12
TAG_BYTES = 16
POINT_BYTES = 65  # 未压缩的 P-256 公钥：0x04 + X + Y
_KEYS_INFO = f"{PROTOCOL} transport keys".encode()
_DERIVE_INFO = f"{PROTOCOL} transport signing key".encode()


class SealError(Exception):
    """密文不对：格式错误、被改动、密钥或附加认证数据不匹配。"""


@dataclass(frozen=True)
class SessionKeys:
    request: bytes  # 浏览器 → 服务器
    response: bytes  # 服务器 → 浏览器


@dataclass(frozen=True)
class Handshake:
    session: str
    server_key: bytes  # 服务器的一次性公钥（未压缩的点）
    expires_at: int
    signature: bytes  # ECDSA P-256 / SHA-256，r || s 各 32 字节（WebCrypto 的格式）


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64url(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise SealError("base64 无效") from exc


_signing_cache: dict[tuple[str, str], ec.EllipticCurvePrivateKey] = {}


def signing_key(settings: Settings) -> ec.EllipticCurvePrivateKey:
    configured = settings.transport_signing_key.get_secret_value().strip()
    secret = settings.data_encryption_key.get_secret_value()
    cache_key = (configured, "" if configured else secret)
    key = _signing_cache.get(cache_key)
    if key is None:
        key = _load(configured) if configured else _derive(secret)
        _signing_cache[cache_key] = key
    return key


def _load(configured: str) -> ec.EllipticCurvePrivateKey:
    if configured.startswith("-----BEGIN"):
        key = serialization.load_pem_private_key(configured.encode(), password=None)
    else:
        der = base64.b64decode(configured)
        key = serialization.load_der_private_key(der, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("EDP_TRANSPORT_SIGNING_KEY must be an ECDSA P-256 private key")
    return key


def _derive(secret: str) -> ec.EllipticCurvePrivateKey:
    """开发和测试环境：由数据加密密钥派生固定的一对，重启后不变（前端不用重新获取公钥）。"""
    material = HKDF(hashes.SHA256(), length=48, salt=None, info=_DERIVE_INFO).derive(
        secret.encode()
    )
    order = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
    return ec.derive_private_key(int.from_bytes(material, "big") % (order - 1) + 1, ec.SECP256R1())


def private_key_b64(key: ec.EllipticCurvePrivateKey) -> str:
    """EDP_TRANSPORT_SIGNING_KEY 的写法：PKCS#8 DER 的 base64（一行，方便放进环境变量）。"""
    der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return base64.b64encode(der).decode()


def public_key_b64(settings: Settings) -> str:
    """服务器公钥（SubjectPublicKeyInfo DER 的 base64）：VITE_TRANSPORT_PUBLIC_KEY 的值。"""
    return base64.b64encode(_public_der(settings)).decode()


def key_id(settings: Settings) -> str:
    return hashlib.sha256(_public_der(settings)).hexdigest()[:16]


def _public_der(settings: Settings) -> bytes:
    return (
        signing_key(settings)
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )


def transcript(client_key: bytes, server_key: bytes, session: str, expires_at: int) -> bytes:
    """握手签名的内容：双方的一次性公钥（定长）、会话号和过期时间。"""
    return (
        f"{PROTOCOL}|handshake|".encode()
        + client_key
        + server_key
        + f"{session}|{expires_at}".encode()
    )


def derive_keys(shared: bytes, session: str) -> SessionKeys:
    okm = HKDF(hashes.SHA256(), length=64, salt=session.encode(), info=_KEYS_INFO).derive(shared)
    return SessionKeys(request=okm[:32], response=okm[32:])


def handshake(settings: Settings, client_key: bytes, *, now: int) -> tuple[Handshake, SessionKeys]:
    if len(client_key) != POINT_BYTES:
        raise SealError("公钥长度不对")
    try:
        client = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), client_key)
    except ValueError as exc:
        raise SealError("公钥无效") from exc
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    server_key = ephemeral.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = ephemeral.exchange(ec.ECDH(), client)
    session = secrets.token_urlsafe(18)
    expires_at = now + settings.transport_session_ttl_seconds
    der = signing_key(settings).sign(
        transcript(client_key, server_key, session, expires_at), ec.ECDSA(hashes.SHA256())
    )
    r, s = decode_dss_signature(der)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return Handshake(session, server_key, expires_at, signature), derive_keys(shared, session)


def request_aad(method: str, path: str, session: str, ts: int, nonce: str, part: str) -> bytes:
    return f"{PROTOCOL}|req|{method.upper()}|{path}|{session}|{ts}|{nonce}|{part}".encode()


def response_aad(method: str, path: str, session: str, nonce: str) -> bytes:
    return f"{PROTOCOL}|res|{method.upper()}|{path}|{session}|{nonce}".encode()


def seal(key: bytes, plaintext: bytes, aad: bytes, *, iv: bytes | None = None) -> bytes:
    iv = iv or os.urandom(IV_BYTES)
    return iv + AESGCM(key).encrypt(iv, plaintext, aad)


def open_sealed(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < IV_BYTES + TAG_BYTES:
        raise SealError("密文太短")
    try:
        return AESGCM(key).decrypt(blob[:IV_BYTES], blob[IV_BYTES:], aad)
    except InvalidTag as exc:
        raise SealError("密文无效") from exc
