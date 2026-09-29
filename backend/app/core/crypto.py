"""敏感字段加密：渠道凭证（企业微信永久授权码等）加密后入库。

密钥来自 EDP_DATA_ENCRYPTION_KEY（任意长度的随机字符串，经 SHA-256 派生为 Fernet 密钥），
密文带版本前缀，将来换成租户级数据密钥（信封加密，设计文档 §7.1）时可以按前缀区分。
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings

_PREFIX = "v1:"


class DecryptError(Exception):
    """密文无法解密：密钥不对或内容被改动。"""


def _fernet(settings: Settings) -> Fernet:
    secret = settings.data_encryption_key.get_secret_value().encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))


def seal(settings: Settings, plaintext: str) -> str:
    return _PREFIX + _fernet(settings).encrypt(plaintext.encode()).decode()


def unseal(settings: Settings, sealed: str) -> str:
    if not sealed.startswith(_PREFIX):
        raise DecryptError("unknown ciphertext version")
    try:
        return _fernet(settings).decrypt(sealed[len(_PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise DecryptError("ciphertext cannot be decrypted") from exc
