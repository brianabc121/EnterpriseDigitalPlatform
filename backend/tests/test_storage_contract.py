"""对象存储契约测试：用真实的 S3 兼容服务验证预签名上传、下载和建桶。

只在设置了 EDP_TEST_STORAGE_URL 时运行（例如 make dev-up 之后
EDP_TEST_STORAGE_URL=http://localhost:9000）。换用其他对象存储（云厂商 OSS/COS/OBS、RustFS、
SeaweedFS 等）前，用它确认签名与接口兼容。
"""

import dataclasses
import os
import uuid

import httpx
import pytest

from app.integrations.storage import StorageConfig, ensure_bucket, presign

URL = os.environ.get("EDP_TEST_STORAGE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="需要 EDP_TEST_STORAGE_URL（真实对象存储）")


def config(bucket: str) -> StorageConfig:
    assert URL is not None
    return StorageConfig(
        endpoint=URL,
        public_endpoint=URL,
        access_key=os.environ.get("EDP_TEST_STORAGE_ACCESS_KEY", "edp"),
        secret_key=os.environ.get("EDP_TEST_STORAGE_SECRET_KEY", "edp-dev-storage-secret"),
        bucket=bucket,
        region=os.environ.get("EDP_TEST_STORAGE_REGION", "us-east-1"),
    )


async def test_bucket_upload_and_download_round_trip() -> None:
    storage = config(f"edp-contract-{uuid.uuid4().hex[:12]}")
    assert await ensure_bucket(storage) is True
    assert await ensure_bucket(storage) is False
    key = f"acme/2026/09/{uuid.uuid4().hex}/报价 单(1).pdf"
    body = "合同内容 %PDF-1.4".encode()

    async with httpx.AsyncClient(timeout=10) as client:
        put = await client.put(
            presign(storage, "PUT", key, expires=60),
            content=body,
            headers={"content-type": "application/pdf"},
        )
        got = await client.get(presign(storage, "GET", key, expires=60))
        # 改动签过名的参数、用错误的密钥签名，都会被拒绝。
        signed = presign(storage, "GET", key, expires=60)
        tampered = await client.get(signed.replace("X-Amz-Expires=60", "X-Amz-Expires=61"))
        forged = dataclasses.replace(storage, secret_key="wrong-secret")
        wrong_key = await client.get(presign(forged, "GET", key, expires=60))

    assert put.status_code == 200, put.text
    assert (got.status_code, got.content) == (200, body)
    assert got.headers["content-type"] == "application/pdf"
    assert tampered.status_code == 403
    assert wrong_key.status_code == 403


async def test_upload_url_only_accepts_the_declared_type_and_size() -> None:
    """上传凭证签了 content-type 和 content-length：换类型、换成更大的文件都被拒绝；
    下载时可以指定返回的类型（图片按扩展名的类型显示）。"""
    storage = config(f"edp-contract-{uuid.uuid4().hex[:12]}")
    await ensure_bucket(storage)
    key = f"acme/2026/09/{uuid.uuid4().hex}/a.png"
    body = b"\x89PNG\r\n\x1a\n" + bytes(24)
    url = presign(
        storage,
        "PUT",
        key,
        expires=60,
        headers={"content-type": "image/png", "content-length": str(len(body))},
    )

    async with httpx.AsyncClient(timeout=10) as client:
        wrong_type = await client.put(url, content=body, headers={"content-type": "text/html"})
        bigger = await client.put(
            url, content=body + b"<script>", headers={"content-type": "image/png"}
        )
        put = await client.put(url, content=body, headers={"content-type": "image/png"})
        got = await client.get(
            presign(storage, "GET", key, expires=60, query={"response-content-type": "image/png"})
        )

    assert wrong_type.status_code == 403
    assert bigger.status_code == 403
    assert put.status_code == 200, put.text
    assert (got.status_code, got.content) == (200, body)
    assert got.headers["content-type"] == "image/png"
