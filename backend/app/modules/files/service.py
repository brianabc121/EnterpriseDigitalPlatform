"""文件上传与下载。

- 上传：平台校验类型和大小后签发预签名上传 URL，浏览器直接上传到对象存储。
- 下载：消息里保存平台签发的文件链接（/api/v1/files/{key}?sig=...）。链接本身不过期，
  只有拿到消息的人才知道；访问时平台校验签名，再重定向到 5 分钟有效的预签名下载 URL，
  不直接暴露对象存储地址（设计文档 §13.3）。
"""

import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from app.core.config import Settings
from app.core.errors import Unprocessable
from app.integrations.storage import StorageConfig, presign

UPLOAD_URL_TTL = 10 * 60
DOWNLOAD_URL_TTL = 5 * 60

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
FILE_TYPES = {
    "application/pdf",
    "text/plain",
    "application/zip",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
_UNSAFE = re.compile(r"[^\w.\-一-鿿]+")


@dataclass(frozen=True)
class UploadTicket:
    key: str
    upload_url: str
    file_url: str
    kind: str  # image 或 file


def storage_config(settings: Settings) -> StorageConfig:
    return StorageConfig(
        endpoint=settings.storage_endpoint,
        public_endpoint=settings.storage_public_endpoint,
        access_key=settings.storage_access_key,
        secret_key=settings.storage_secret_key.get_secret_value(),
        bucket=settings.storage_bucket,
        region=settings.storage_region,
    )


def safe_filename(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name.strip()).strip("._") or "file"
    return cleaned[-100:]


def _signature(settings: Settings, key: str) -> str:
    secret = settings.file_url_secret.get_secret_value().encode()
    return hmac.new(secret, key.encode(), hashlib.sha256).hexdigest()[:32]


def file_url(settings: Settings, key: str) -> str:
    path = "/".join(quote(part) for part in key.split("/"))
    return (
        f"{settings.public_api_url.rstrip('/')}/api/v1/files/{path}?sig={_signature(settings, key)}"
    )


def verify(settings: Settings, key: str, signature: str) -> bool:
    return hmac.compare_digest(_signature(settings, key), signature)


def new_upload(
    settings: Settings,
    *,
    tenant_code: str,
    filename: str,
    content_type: str,
    size: int,
) -> UploadTicket:
    if content_type in IMAGE_TYPES:
        kind, limit = "image", MAX_IMAGE_BYTES
    elif content_type in FILE_TYPES:
        kind, limit = "file", MAX_FILE_BYTES
    else:
        raise Unprocessable("不支持的文件类型")
    if not 0 < size <= limit:
        raise Unprocessable(f"文件大小不能超过 {limit // 1024 // 1024} MB")
    now = datetime.now(UTC)
    key = f"{tenant_code}/{now:%Y/%m}/{uuid.uuid4().hex}/{safe_filename(filename)}"
    config = storage_config(settings)
    return UploadTicket(
        key=key,
        upload_url=presign(config, "PUT", key, expires=UPLOAD_URL_TTL),
        file_url=file_url(settings, key),
        kind=kind,
    )


def download_url(settings: Settings, key: str) -> str:
    """5 分钟有效的预签名下载 URL；非图片以附件方式下载。"""
    name = key.rsplit("/", 1)[-1]
    query = {}
    if not name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        query["response-content-disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    return presign(storage_config(settings), "GET", key, expires=DOWNLOAD_URL_TTL, query=query)
