"""阿里云 OSS 的 V4 签名和客户端（设计文档 §36.6）。

签名的期望值由阿里云官方的 Python SDK（alibabacloud-oss-v2 1.4.0 的 SignerV4）按同样的输入算出，
防止改动后和真实的 OSS 对不上；读写、分片上传、列举和删除接模拟 OSS（tests/fake_oss.py）。
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from app.integrations.oss import OssClient, OssConfig, OssError, OssSigner
from tests import fake_oss
from tests.fake_oss import FakeOSS

NOW = datetime(2026, 10, 2, 8, 30, 15, tzinfo=UTC)
SDK_CONFIG = OssConfig(
    endpoint="https://oss-cn-hangzhou.aliyuncs.com",
    region="cn-hangzhou",
    bucket="edp-materials",
    access_key_id="LTAI5tAccessKeyIdExample",
    access_key_secret="secret+/=Example",
)
UPLOAD_ID = "0004B9894A22E5B1888A1E29F823****"


def fake_config(**changes: object) -> OssConfig:
    values: dict[str, object] = {
        "endpoint": "http://fake-oss",
        "region": fake_oss.REGION,
        "bucket": fake_oss.BUCKET,
        "access_key_id": fake_oss.ACCESS_KEY_ID,
        "access_key_secret": fake_oss.ACCESS_KEY_SECRET,
        "path_style": True,
        "prefix": "test/",
    }
    values.update(changes)
    return OssConfig(**values)  # type: ignore[arg-type]


def signature(url: str) -> str:
    return dict(parse_qsl(urlsplit(url).query))["x-oss-signature"]


def test_signatures_match_the_official_sdk() -> None:
    signer = OssSigner(SDK_CONFIG)
    put = signer.sign_headers(
        "PUT", "t1/a.md", headers={"content-type": "text/markdown; charset=utf-8"}, now=NOW
    )
    assert put["x-oss-date"] == "20261002T083015Z"
    assert put["x-oss-content-sha256"] == "UNSIGNED-PAYLOAD"
    assert put["authorization"] == (
        "OSS4-HMAC-SHA256 Credential=LTAI5tAccessKeyIdExample/20261002/cn-hangzhou/oss/"
        "aliyun_v4_request,Signature="
        "57073e828a88e94bd9554815b477fb0d4639e53e36b878006024b4f5a292f1b2"
    )
    initiate = signer.sign_headers(
        "POST", "t1/v.mp4", query={"uploads": None}, headers={"content-type": "video/mp4"}, now=NOW
    )
    assert initiate["authorization"].endswith(
        "Signature=7a27177a35bdee4d265748cf82142c513a029ac8bb6e06ba45b7aa59dbf65edb"
    )
    listing = signer.sign_headers(
        "GET", "", query={"list-type": "2", "prefix": "env/t1/", "max-keys": "1000"}, now=NOW
    )
    assert listing["authorization"].endswith(
        "Signature=67454ae81b7534eec322b489e1d92cf359fa51a9c1e315de52b84a8e859cb190"
    )

    snapshot = signer.presign(
        "GET",
        "t1/v.mp4",
        expires=7200,
        query={"x-oss-process": "video/snapshot,t_1000,f_jpg,w_480,m_fast"},
        now=NOW,
    )
    assert signature(snapshot) == (
        "dee2bc1ffb48ec67e14cc40ea82fa53c2ad2123adc38646409db16a79ce0b004"
    )
    upload = signer.presign(
        "PUT", "t1/a.png", expires=900, headers={"content-type": "image/png"}, now=NOW
    )
    assert signature(upload) == "91d8ba89fd28481baadfa0e64b3edc0000765514929d0f5d87fd363d00956efd"
    part = signer.presign(
        "PUT",
        "t1/v.mp4",
        expires=900,
        query={"partNumber": "3", "uploadId": UPLOAD_ID},
        now=NOW,
    )
    assert signature(part) == "73d4839fe425f869b14e6221b696e1e3b6e0d06c9555e8dcbf25b663212b566c"
    params = dict(parse_qsl(urlsplit(part).query))
    assert params["x-oss-signature-version"] == "OSS4-HMAC-SHA256"
    assert params["x-oss-credential"] == (
        "LTAI5tAccessKeyIdExample/20261002/cn-hangzhou/oss/aliyun_v4_request"
    )
    assert (params["x-oss-date"], params["x-oss-expires"]) == ("20261002T083015Z", "900")
    with pytest.raises(ValueError):
        signer.presign("GET", "t1/a.md", expires=8 * 24 * 3600)


def test_object_urls_for_endpoints_and_custom_domains() -> None:
    assert OssSigner(SDK_CONFIG).object_url("t1/a b.mp4") == (
        "https://edp-materials.oss-cn-hangzhou.aliyuncs.com/t1/a%20b.mp4"
    )
    # 服务端用内网域名，签发给浏览器的地址用外网域名。
    internal = OssConfig(
        endpoint="https://oss-cn-hangzhou-internal.aliyuncs.com",
        public_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        region="cn-hangzhou",
        bucket="edp-materials",
        access_key_id="a",
        access_key_secret="b",
    )
    signer = OssSigner(internal)
    assert signer.object_url("k.pdf") == (
        "https://edp-materials.oss-cn-hangzhou-internal.aliyuncs.com/k.pdf"
    )
    assert signer.object_url("k.pdf", public=True) == (
        "https://edp-materials.oss-cn-hangzhou.aliyuncs.com/k.pdf"
    )
    # 绑定到 Bucket 的自定义域名。
    cname = OssSigner(
        OssConfig(**{**internal.__dict__, "public_endpoint": "https://files.example.com"})
    )
    assert cname.object_url("k.pdf", public=True) == "https://files.example.com/k.pdf"
    # 模拟 OSS：路径风格。
    assert OssSigner(fake_config()).object_url("test/k.pdf") == (
        "http://fake-oss/edp-materials/test/k.pdf"
    )


async def test_client_round_trip_against_fake_oss() -> None:
    fake = FakeOSS()
    client = OssClient(fake_config(), transport=fake.transport())
    browser = httpx.AsyncClient(transport=fake.transport())
    try:
        key = client.key("t1/a.md")
        assert key == "test/t1/a.md"
        etag = await client.put(key, "# 资料\n".encode(), "text/markdown; charset=utf-8")
        assert etag and await client.get(key) == "# 资料\n".encode()
        info = await client.head(key)
        assert info is not None and info.size == len("# 资料\n".encode())
        assert await client.head(client.key("t1/missing.md")) is None
        await client.ping()

        # 浏览器拿签名地址上传和下载；Content-Type 一起签名，换了就不对。
        url = client.presign(
            "PUT", client.key("t1/p.png"), expires=900, headers={"content-type": "image/png"}
        )
        assert (
            await browser.put(url, content=b"png", headers={"content-type": "image/png"})
        ).status_code == 200
        wrong = await browser.put(url, content=b"x", headers={"content-type": "text/html"})
        assert wrong.status_code == 403 and b"SignatureDoesNotMatch" in wrong.content
        download = client.presign(
            "GET",
            client.key("t1/p.png"),
            expires=300,
            query={"response-content-disposition": "attachment; filename*=UTF-8''%E5%9B%BE.png"},
        )
        got = await browser.get(download)
        assert got.content == b"png"
        assert got.headers["content-disposition"] == "attachment; filename*=UTF-8''%E5%9B%BE.png"
        tampered = download.replace("x-oss-expires=300", "x-oss-expires=600")
        assert (await browser.get(tampered)).status_code == 403
        expired = client.presign(
            "GET", client.key("t1/p.png"), expires=60, now=datetime.now(UTC) - timedelta(hours=1)
        )
        assert (await browser.get(expired)).status_code == 403
        snapshot = client.presign(
            "GET",
            client.key("t1/p.png"),
            expires=300,
            query={"x-oss-process": "image/resize,m_lfit,w_480,h_480"},
        )
        assert (await browser.get(snapshot)).headers["content-type"] == "image/png"

        # 分片上传：初始化、浏览器按签名地址传分片、合并。
        video = client.key("t1/v.mp4")
        upload_id = await client.initiate_multipart(video, "video/mp4")
        chunks = [b"a" * 10, b"b" * 10, b"c" * 3]
        parts = []
        for number, chunk in enumerate(chunks, start=1):
            part_url = client.presign(
                "PUT",
                video,
                expires=900,
                query={"partNumber": str(number), "uploadId": upload_id},
            )
            response = await browser.put(part_url, content=chunk)
            assert response.status_code == 200, response.text
            parts.append((number, response.headers["etag"]))
        merged = await client.complete_multipart(video, upload_id, parts)
        assert merged.endswith("-3")
        assert await client.get(video) == b"".join(chunks)
        assert fake.uploads == {}

        # 取消分片上传；列举（分页）和按前缀删除。
        aborted = await client.initiate_multipart(client.key("t1/x.mp4"), "video/mp4")
        await client.abort_multipart(client.key("t1/x.mp4"), aborted)
        assert fake.uploads == {}
        for n in range(3):
            await client.put(client.key(f"t2/{n}.txt"), b"x" * n, "text/plain")
        assert len(await client.list_keys(client.key("t1/"))) == 3
        assert await client.delete_prefix(client.key("t2/")) == (3, 3)
        assert await client.list_keys(client.key("t2/")) == []
        await client.delete(client.key("t1/missing.md"))
        with pytest.raises(ValueError):
            await client.delete_prefix("")
    finally:
        await client.aclose()
        await browser.aclose()


async def test_errors_and_unconfigured_client() -> None:
    fake = FakeOSS()
    wrong_secret = OssClient(fake_config(access_key_secret="nope"), transport=fake.transport())
    with pytest.raises(OssError) as denied:
        await wrong_secret.put("test/a.txt", b"x", "text/plain")
    assert denied.value.status == 403 and "SignatureDoesNotMatch" in str(denied.value)
    await wrong_secret.aclose()

    client = OssClient(fake_config(), transport=fake.transport())
    fake.fail_count = 1
    with pytest.raises(OssError) as unavailable:
        await client.get("test/a.txt")
    assert unavailable.value.status == 503
    await client.aclose()

    disabled = OssClient(fake_config(access_key_id=""))
    assert not disabled.enabled
    with pytest.raises(OssError):
        await disabled.ping()
    await disabled.aclose()
