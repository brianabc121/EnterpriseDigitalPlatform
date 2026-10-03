"""口令哈希与 JWT。

员工令牌、平台令牌和访客令牌使用不同的密钥和 audience，互相不能冒用。
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

ISSUER = "edp"
TENANT_AUDIENCE = "edp:tenant"
PLATFORM_AUDIENCE = "edp:platform"
VISITOR_AUDIENCE = "edp:visitor"
_ALGORITHM = "HS256"

_hasher = PasswordHasher()
# 账号不存在时也做一次完整的哈希校验，使响应耗时与"密码错误"一致，避免借耗时枚举账号。
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        matched = _hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return matched and password_hash is not None


# 自动生成的密码（设计文档 §38）：去掉容易看错的 0/O、1/l/I，大写、小写字母和数字都有。
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"


def generate_password(length: int = 12) -> str:
    while True:
        password = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
        if (
            any(c.isupper() for c in password)
            and any(c.islower() for c in password)
            and any(c.isdigit() for c in password)
        ):
            return password


class TokenError(Exception):
    """令牌无效、过期或类型不符。"""


@dataclass(frozen=True)
class AccessClaims:
    staff_id: UUID
    tenant_id: UUID
    # 签发时员工密码最近修改的时间（见 password_stamp）：和员工现在的不一样时令牌失效（§38.6）。
    password_stamp: int | None = None


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def password_stamp(changed_at: datetime | None) -> int | None:
    """密码最近修改的时间（微秒时间戳），写进访问令牌：重置或修改密码之后，之前签发的令牌就对不上了。
    没有修改过时为空。"""
    if changed_at is None:
        return None
    return (changed_at - _EPOCH) // timedelta(microseconds=1)


@dataclass(frozen=True)
class RefreshClaims:
    staff_id: UUID
    tenant_id: UUID
    token_id: UUID
    family_id: UUID


@dataclass(frozen=True)
class PlatformClaims:
    user_id: UUID


@dataclass(frozen=True)
class VisitorClaims:
    identity_id: UUID
    tenant_id: UUID
    channel_id: UUID


def _encode(payload: dict[str, Any], *, secret: str, audience: str, ttl_seconds: int) -> str:
    now = datetime.now(UTC)
    claims = {
        **payload,
        "iss": ISSUER,
        "aud": audience,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def _decode(token: str, *, secret: str, audience: str, token_type: str) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            audience=audience,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if claims.get("typ") != token_type:
        raise TokenError("unexpected token type")
    return claims


def _uuid(claims: dict[str, Any], key: str) -> UUID:
    try:
        return UUID(str(claims[key]))
    except (KeyError, ValueError) as exc:
        raise TokenError(f"invalid claim: {key}") from exc


def encode_access_token(
    *,
    staff_id: UUID,
    tenant_id: UUID,
    secret: str,
    ttl_seconds: int,
    password_stamp: int | None = None,
) -> str:
    payload: dict[str, Any] = {"sub": str(staff_id), "tid": str(tenant_id), "typ": "access"}
    if password_stamp is not None:
        payload["pwd"] = password_stamp
    return _encode(payload, secret=secret, audience=TENANT_AUDIENCE, ttl_seconds=ttl_seconds)


def decode_access_token(token: str, *, secret: str) -> AccessClaims:
    claims = _decode(token, secret=secret, audience=TENANT_AUDIENCE, token_type="access")
    stamp = claims.get("pwd")
    if stamp is not None and (not isinstance(stamp, int) or isinstance(stamp, bool)):
        raise TokenError("invalid claim: pwd")
    return AccessClaims(
        staff_id=_uuid(claims, "sub"), tenant_id=_uuid(claims, "tid"), password_stamp=stamp
    )


def encode_refresh_token(
    *,
    staff_id: UUID,
    tenant_id: UUID,
    token_id: UUID,
    family_id: UUID,
    secret: str,
    ttl_seconds: int,
) -> str:
    return _encode(
        {
            "sub": str(staff_id),
            "tid": str(tenant_id),
            "jti": str(token_id),
            "fam": str(family_id),
            "typ": "refresh",
        },
        secret=secret,
        audience=TENANT_AUDIENCE,
        ttl_seconds=ttl_seconds,
    )


def decode_refresh_token(token: str, *, secret: str) -> RefreshClaims:
    claims = _decode(token, secret=secret, audience=TENANT_AUDIENCE, token_type="refresh")
    return RefreshClaims(
        staff_id=_uuid(claims, "sub"),
        tenant_id=_uuid(claims, "tid"),
        token_id=_uuid(claims, "jti"),
        family_id=_uuid(claims, "fam"),
    )


def encode_platform_token(*, user_id: UUID, secret: str, ttl_seconds: int) -> str:
    return _encode(
        {"sub": str(user_id), "typ": "access"},
        secret=secret,
        audience=PLATFORM_AUDIENCE,
        ttl_seconds=ttl_seconds,
    )


def decode_platform_token(token: str, *, secret: str) -> PlatformClaims:
    claims = _decode(token, secret=secret, audience=PLATFORM_AUDIENCE, token_type="access")
    return PlatformClaims(user_id=_uuid(claims, "sub"))


@dataclass(frozen=True)
class PlatformRefreshClaims:
    user_id: UUID
    expires_at: datetime


def encode_platform_refresh_token(*, user_id: UUID, secret: str, ttl_seconds: int) -> str:
    """运营后台的刷新令牌（放在 httpOnly Cookie 里）：到期时间不随刷新延长。"""
    return _encode(
        {"sub": str(user_id), "typ": "refresh"},
        secret=secret,
        audience=PLATFORM_AUDIENCE,
        ttl_seconds=ttl_seconds,
    )


def decode_platform_refresh_token(token: str, *, secret: str) -> PlatformRefreshClaims:
    claims = _decode(token, secret=secret, audience=PLATFORM_AUDIENCE, token_type="refresh")
    return PlatformRefreshClaims(
        user_id=_uuid(claims, "sub"), expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC)
    )


def encode_visitor_token(
    *, identity_id: UUID, tenant_id: UUID, channel_id: UUID, secret: str, ttl_seconds: int
) -> str:
    """访客令牌：代表浏览器里的一个访客身份，由 Widget 保存，下次初始化时带回。"""
    return _encode(
        {"sub": str(identity_id), "tid": str(tenant_id), "chn": str(channel_id), "typ": "visitor"},
        secret=secret,
        audience=VISITOR_AUDIENCE,
        ttl_seconds=ttl_seconds,
    )


def decode_visitor_token(token: str, *, secret: str) -> VisitorClaims:
    claims = _decode(token, secret=secret, audience=VISITOR_AUDIENCE, token_type="visitor")
    return VisitorClaims(
        identity_id=_uuid(claims, "sub"),
        tenant_id=_uuid(claims, "tid"),
        channel_id=_uuid(claims, "chn"),
    )
