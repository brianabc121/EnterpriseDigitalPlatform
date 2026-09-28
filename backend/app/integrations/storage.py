"""S3 兼容对象存储：生成 AWS Signature V4 预签名 URL。

平台不经手文件内容：浏览器用预签名 URL 直接上传到对象存储；下载时由平台接口校验下载凭证后，
重定向到短时有效的预签名 URL（不直接暴露对象存储地址）。

开发环境用 OpenIM 依赖里的 MinIO；生产环境可以换成任何支持 S3 协议和 SigV4 的对象存储
（实施计划 §12 对象存储选型）。
"""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

_ALGORITHM = "AWS4-HMAC-SHA256"
_SERVICE = "s3"
_UNSIGNED = "UNSIGNED-PAYLOAD"


@dataclass(frozen=True)
class StorageConfig:
    # 后端访问对象存储的地址（创建存储桶等）；浏览器访问的公开地址用于签发上传、下载 URL。
    endpoint: str
    public_endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    region: str = "us-east-1"


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _signing_key(secret_key: str, date: str, region: str) -> bytes:
    key = _sign(f"AWS4{secret_key}".encode(), date)
    key = _sign(key, region)
    key = _sign(key, _SERVICE)
    return _sign(key, "aws4_request")


def _encode(value: str) -> str:
    return quote(value, safe="-_.~")


def presign(
    config: StorageConfig,
    method: str,
    key: str,
    *,
    expires: int,
    endpoint: str | None = None,
    now: datetime | None = None,
    query: dict[str, str] | None = None,
) -> str:
    """路径风格（{endpoint}/{bucket}/{key}）的预签名 URL，有效期 expires 秒（最长 7 天）。"""
    if not 1 <= expires <= 7 * 24 * 3600:
        raise ValueError("expires must be between 1 second and 7 days")
    base = (endpoint or config.public_endpoint).rstrip("/")
    parts = urlsplit(base)
    host = parts.netloc
    # bucket 为空表示虚拟主机风格（存储桶已经在域名里）。
    path = parts.path + ("/" + _encode(config.bucket) if config.bucket else "")
    if key:
        path += "/" + "/".join(_encode(segment) for segment in key.split("/"))
    now = now or datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    scope = f"{date}/{config.region}/{_SERVICE}/aws4_request"
    params = {
        **(query or {}),
        "X-Amz-Algorithm": _ALGORITHM,
        "X-Amz-Credential": f"{config.access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(f"{_encode(k)}={_encode(v)}" for k, v in sorted(params.items()))
    canonical_request = "\n".join(
        [method, path, canonical_query, f"host:{host}\n", "host", _UNSIGNED]
    )
    string_to_sign = "\n".join(
        [_ALGORITHM, amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    signature = hmac.new(
        _signing_key(config.secret_key, date, config.region),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{parts.scheme}://{host}{path}?{canonical_query}&X-Amz-Signature={signature}"


async def ensure_bucket(
    config: StorageConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> bool:
    """存储桶不存在时创建。返回是否新建。"""
    async with httpx.AsyncClient(timeout=10, transport=transport) as client:
        head = await client.head(presign(config, "HEAD", "", expires=60, endpoint=config.endpoint))
        if head.status_code == 200:
            return False
        created = await client.put(presign(config, "PUT", "", expires=60, endpoint=config.endpoint))
        if created.status_code not in (200, 409):
            raise RuntimeError(
                f"create bucket failed: HTTP {created.status_code} {created.text[:200]}"
            )
        return created.status_code == 200
