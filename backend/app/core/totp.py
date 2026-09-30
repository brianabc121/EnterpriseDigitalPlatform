"""基于时间的一次性验证码（TOTP，RFC 6238）：平台运营账号的二次验证。

与常见的验证器应用（企业微信、微信小程序"腾讯身份验证器"、Google Authenticator 等）兼容：
SHA-1、6 位数字、30 秒一个时间步。验证时允许前后各一个时间步的时钟误差。
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
PERIOD = 30
WINDOW = 1


def new_secret() -> str:
    """160 位随机密钥的 Base32 编码（去掉填充）。"""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def counter_at(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // PERIOD)


def code_at(secret: str, counter: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def match(secret: str, code: str, *, now: float | None = None) -> int | None:
    """验证码对应的时间步；不匹配时为空。"""
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = counter_at(now)
    for counter in range(current - WINDOW, current + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, counter), code):
            return counter
    return None


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    """验证器应用扫码添加账号用的 otpauth:// 地址。"""
    label = quote(f"{issuer}:{account}")
    query = urlencode({"secret": secret, "issuer": issuer, "digits": DIGITS, "period": PERIOD})
    return f"otpauth://totp/{label}?{query}"
