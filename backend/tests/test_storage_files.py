"""对象存储签名、上传凭证与文件链接。"""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.integrations.storage import StorageConfig, presign
from app.modules.files import service as files
from tests.factories import bearer, create_staff, login, provision


def test_presign_matches_the_aws_example() -> None:
    """AWS 文档"使用查询参数签名"中的预签名 GET 示例。"""
    config = StorageConfig(
        endpoint="https://examplebucket.s3.amazonaws.com",
        public_endpoint="https://examplebucket.s3.amazonaws.com",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        bucket="",
    )

    url = presign(config, "GET", "test.txt", expires=86400, now=datetime(2013, 5, 24, tzinfo=UTC))

    assert url == (
        "https://examplebucket.s3.amazonaws.com/test.txt"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
        "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
    )


def test_presign_uses_path_style_and_encodes_keys() -> None:
    config = StorageConfig(
        endpoint="http://minio:9000",
        public_endpoint="http://localhost:9000",
        access_key="k",
        secret_key="s",
        bucket="edp-files",
    )

    url = presign(config, "PUT", "acme/2026/09/abc/报价 单.pdf", expires=600)

    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc) == ("http", "localhost:9000")
    assert parts.path == "/edp-files/acme/2026/09/abc/%E6%8A%A5%E4%BB%B7%20%E5%8D%95.pdf"
    query = parse_qs(parts.query)
    assert query["X-Amz-Expires"] == ["600"]
    with pytest.raises(ValueError):
        presign(config, "GET", "x", expires=8 * 24 * 3600)


def test_upload_rules_and_file_links(settings: Settings) -> None:
    ticket = files.new_upload(
        settings,
        tenant_code="acme",
        filename="../合同 v2.pdf",
        content_type="application/pdf",
        size=1024,
    )
    assert ticket.kind == "file"
    assert ticket.key.startswith("acme/") and ticket.key.endswith("/合同_v2.pdf")
    assert ticket.upload_url.startswith(settings.storage_public_endpoint)
    sig = parse_qs(urlsplit(ticket.file_url).query)["sig"][0]
    assert files.verify(settings, ticket.key, sig)
    assert not files.verify(settings, ticket.key + "x", sig)

    image = files.new_upload(
        settings, tenant_code="acme", filename="a.png", content_type="image/png", size=10
    )
    assert image.kind == "image"
    from app.core.errors import Unprocessable

    with pytest.raises(Unprocessable):
        files.new_upload(
            settings,
            tenant_code="acme",
            filename="a.exe",
            content_type="application/x-msdownload",
            size=10,
        )
    with pytest.raises(Unprocessable):
        files.new_upload(
            settings,
            tenant_code="acme",
            filename="big.png",
            content_type="image/png",
            size=11 * 1024 * 1024,
        )


async def test_staff_upload_and_download_redirect(
    app: FastAPI, client: httpx.AsyncClient, settings: Settings
) -> None:
    await provision(app, "acme")
    admin = await login(client, "acme")
    await create_staff(client, admin, "alice")
    token = await login(client, "acme", "alice", "staff-pass-123")

    response = await client.post(
        "/api/v1/uploads",
        headers=bearer(token),
        json={"filename": "截图.png", "content_type": "image/png", "size": 2048},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "image"
    path = urlsplit(body["file_url"])
    redirect = await client.get(f"{path.path}?{path.query}")
    assert redirect.status_code == 302
    assert redirect.headers["location"].startswith(settings.storage_public_endpoint)
    assert "X-Amz-Signature=" in redirect.headers["location"]
    forged = await client.get(f"{path.path}?sig=deadbeef")
    assert forged.status_code == 404
