"""阿里云 OSS（企业资料，设计文档 §36）：V4 签名（OSS4-HMAC-SHA256）。

- 服务端请求（初始化和合并分片、写文字资料、读文件做病毒扫描和导入知识库、
  删除）用 Authorization 头；
- 浏览器上传、预览、下载用 URL 签名（x-oss-signature-version、x-oss-credential、
  x-oss-date、x-oss-expires、x-oss-signature），平台不经手文件内容。

规范请求按官方文档和 SDK：方法、规范 URI（/{bucket}/{key}，"/" 不编码）、
规范查询串（按编码后的名称排序，没有值的参数只写名称）、规范请求头（content-type、
content-md5、x-oss-* 和附加的请求头，名称小写、值去掉首尾空白）、附加请求头的名称、
UNSIGNED-PAYLOAD。签名密钥依次用 "aliyun_v4" + AccessKeySecret、日期、地域、"oss"、
"aliyun_v4_request" 做 HMAC-SHA256。

阿里云 OSS 用虚拟主机风格（{bucket}.{endpoint}）；开发和测试的模拟 OSS 用路径风格。
"""

import contextlib
import hashlib
import hmac
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

ALGORITHM = "OSS4-HMAC-SHA256"
UNSIGNED = "UNSIGNED-PAYLOAD"
MAX_EXPIRES = 7 * 24 * 3600
# 规范请求头里默认包含的请求头（另外是全部 x-oss-* 和附加的请求头）。
DEFAULT_SIGNED = ("content-type", "content-md5")


@dataclass(frozen=True)
class OssConfig:
    endpoint: str
    region: str
    bucket: str
    access_key_id: str
    access_key_secret: str
    # 签发给浏览器的地址（自定义域名）；为空时用 endpoint。
    public_endpoint: str = ""
    prefix: str = ""
    path_style: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key_id and self.access_key_secret)


class OssError(Exception):
    """OSS 请求失败（网络、签名、权限或对象不存在）。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def uri_encode(value: str, *, keep_slash: bool = False) -> str:
    return quote(value, safe="-_.~" + ("/" if keep_slash else ""))


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str) -> bytes:
    key = _hmac(f"aliyun_v4{secret}".encode(), date)
    key = _hmac(key, region)
    key = _hmac(key, "oss")
    return _hmac(key, "aliyun_v4_request")


def canonical_uri(bucket: str, key: str) -> str:
    if not bucket:
        return "/"
    return uri_encode(f"/{bucket}/{key}", keep_slash=True)


def canonical_query(params: Mapping[str, str | None]) -> str:
    """按编码后的名称排序；没有值（None 或空串）的参数只写名称。"""
    pairs = sorted((uri_encode(k), uri_encode(v) if v else "") for k, v in params.items())
    return "&".join(f"{k}={v}" if v else k for k, v in pairs)


def canonical_headers(headers: Mapping[str, str], additional: list[str]) -> str:
    picked = {
        name.lower(): " ".join(value.strip().split())
        for name, value in headers.items()
        if name.lower().startswith("x-oss-")
        or name.lower() in DEFAULT_SIGNED
        or name.lower() in additional
    }
    return "".join(f"{name}:{picked[name]}\n" for name in sorted(picked))


def string_to_sign(canonical_request: str, timestamp: str, scope: str) -> str:
    digest = hashlib.sha256(canonical_request.encode()).hexdigest()
    return f"{ALGORITHM}\n{timestamp}\n{scope}\n{digest}"


def canonical_request(
    method: str,
    uri: str,
    query: Mapping[str, str | None],
    headers: Mapping[str, str],
    additional: list[str],
) -> str:
    return "\n".join(
        [
            method,
            uri,
            canonical_query(query),
            canonical_headers(headers, additional),
            ";".join(additional),
            UNSIGNED,
        ]
    )


def _stamp(now: datetime | None) -> tuple[str, str]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")


class OssSigner:
    def __init__(self, config: OssConfig) -> None:
        self.config = config

    def scope(self, date: str) -> str:
        return f"{date}/{self.config.region}/oss/aliyun_v4_request"

    def object_url(self, key: str, *, public: bool = False) -> str:
        """对象的地址（不带查询串）。签发给浏览器的地址用 public_endpoint：地域的外网域名
        （*.aliyuncs.com，服务端用内网域名时）加上 Bucket，或者绑定到 Bucket 的自定义域名。"""
        config = self.config
        use_public = public and bool(config.public_endpoint)
        base = config.public_endpoint if use_public else config.endpoint
        parts = urlsplit(base.rstrip("/"))
        path = uri_encode("/" + key, keep_slash=True) if key else "/"
        if config.path_style:
            return f"{parts.scheme}://{parts.netloc}{parts.path}/{uri_encode(config.bucket)}{path}"
        custom_domain = use_public and not parts.netloc.endswith(".aliyuncs.com")
        host = parts.netloc if custom_domain else f"{config.bucket}.{parts.netloc}"
        return f"{parts.scheme}://{host}{parts.path}{path}"

    def sign_headers(
        self,
        method: str,
        key: str,
        *,
        query: Mapping[str, str | None] | None = None,
        headers: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, str]:
        """服务端请求的请求头：x-oss-date、x-oss-content-sha256 和 Authorization。"""
        timestamp, date = _stamp(now)
        signed = {**(headers or {}), "x-oss-date": timestamp, "x-oss-content-sha256": UNSIGNED}
        request = canonical_request(
            method, canonical_uri(self.config.bucket, key), query or {}, signed, []
        )
        scope = self.scope(date)
        signature = hmac.new(
            signing_key(self.config.access_key_secret, date, self.config.region),
            string_to_sign(request, timestamp, scope).encode(),
            hashlib.sha256,
        ).hexdigest()
        credential = f"{self.config.access_key_id}/{scope}"
        return {
            **signed,
            # 和阿里云的 SDK 一样：逗号后面没有空格。
            "authorization": f"{ALGORITHM} Credential={credential},Signature={signature}",
        }

    def presign(
        self,
        method: str,
        key: str,
        *,
        expires: int,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        now: datetime | None = None,
        public: bool = True,
    ) -> str:
        """签发给浏览器的地址，有效 expires 秒（最长 7 天）。headers 是请求必须
        原样带上的请求头（如上传时的 Content-Type），一起签名。"""
        if not 1 <= expires <= MAX_EXPIRES:
            raise ValueError("expires must be between 1 second and 7 days")
        timestamp, date = _stamp(now)
        scope = self.scope(date)
        params: dict[str, str | None] = {
            **(query or {}),
            "x-oss-signature-version": ALGORITHM,
            "x-oss-credential": f"{self.config.access_key_id}/{scope}",
            "x-oss-date": timestamp,
            "x-oss-expires": str(expires),
        }
        request = canonical_request(
            method, canonical_uri(self.config.bucket, key), params, headers or {}, []
        )
        signature = hmac.new(
            signing_key(self.config.access_key_secret, date, self.config.region),
            string_to_sign(request, timestamp, scope).encode(),
            hashlib.sha256,
        ).hexdigest()
        params["x-oss-signature"] = signature
        return f"{self.object_url(key, public=public)}?{canonical_query(params)}"


def _text(root: ET.Element, name: str) -> str:
    """OSS 的 XML 没有命名空间；兼容带命名空间的模拟服务。"""
    found = root.find(name)
    if found is None:
        found = next((el for el in root.iter() if el.tag.endswith("}" + name)), None)
    return (found.text or "") if found is not None else ""


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    etag: str
    content_type: str


class OssClient:
    """服务端访问 OSS（V4 签名的 Authorization 头）。"""

    def __init__(
        self, config: OssConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self.signer = OssSigner(config)
        self._http = httpx.AsyncClient(timeout=60, transport=transport)

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def aclose(self) -> None:
        await self._http.aclose()

    def key(self, name: str) -> str:
        """加上配置的前缀。"""
        return f"{self.config.prefix}{name}"

    async def _request(
        self,
        method: str,
        key: str,
        *,
        query: Mapping[str, str | None] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        ok: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        if not self.enabled:
            raise OssError("还没有配置企业资料存储（阿里云 OSS）")
        signed = self.signer.sign_headers(method, key, query=query, headers=headers)
        url = self.signer.object_url(key)
        if query:
            url += "?" + canonical_query(query)
        try:
            response = await self._http.request(method, url, headers=signed, content=content)
        except httpx.HTTPError as exc:
            raise OssError(f"{method} {key}: {exc}") from exc
        if response.status_code not in ok:
            code = ""
            with contextlib.suppress(ET.ParseError):
                code = _text(ET.fromstring(response.content), "Code")
            raise OssError(
                f"{method} {key}: HTTP {response.status_code} {code}".strip(), response.status_code
            )
        return response

    # ---- 对象 ----

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        response = await self._request(
            "PUT", key, headers={"content-type": content_type}, content=data
        )
        return str(response.headers.get("etag", "")).strip('"')

    async def get(self, key: str) -> bytes:
        return (await self._request("GET", key)).content

    async def head(self, key: str) -> ObjectInfo | None:
        try:
            response = await self._request("HEAD", key, ok=(200,))
        except OssError as exc:
            if exc.status == 404:
                return None
            raise
        return ObjectInfo(
            size=int(response.headers.get("content-length") or 0),
            etag=str(response.headers.get("etag", "")).strip('"'),
            content_type=str(response.headers.get("content-type", "")),
        )

    async def delete(self, key: str) -> None:
        """删除对象（不存在时也算成功）。"""
        await self._request("DELETE", key, ok=(200, 204, 404))

    # ---- 分片上传 ----

    async def initiate_multipart(self, key: str, content_type: str) -> str:
        response = await self._request(
            "POST", key, query={"uploads": None}, headers={"content-type": content_type}
        )
        upload_id = _text(ET.fromstring(response.content), "UploadId")
        if not upload_id:
            raise OssError(f"initiate {key}: no UploadId")
        return upload_id

    async def complete_multipart(
        self, key: str, upload_id: str, parts: list[tuple[int, str]]
    ) -> str:
        """合并分片（分片号，ETag），返回对象的 ETag。"""
        body = "".join(
            f"<Part><PartNumber>{number}</PartNumber><ETag>&quot;{etag.strip(chr(34))}&quot;"
            "</ETag></Part>"
            for number, etag in sorted(parts)
        )
        payload = f"<CompleteMultipartUpload>{body}</CompleteMultipartUpload>".encode()
        response = await self._request(
            "POST",
            key,
            query={"uploadId": upload_id},
            headers={"content-type": "application/xml"},
            content=payload,
        )
        return _text(ET.fromstring(response.content), "ETag").strip('"')

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        await self._request("DELETE", key, query={"uploadId": upload_id}, ok=(200, 204, 404))

    # ---- 列举和删除 ----

    async def list_keys(self, prefix: str) -> list[tuple[str, int]]:
        """列出前缀下的全部对象（key，字节数），ListObjectsV2 分页。"""
        keys: list[tuple[str, int]] = []
        token: str | None = None
        while True:
            query: dict[str, str | None] = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                query["continuation-token"] = token
            response = await self._request("GET", "", query=query)
            root = ET.fromstring(response.content)
            for item in root.iter():
                if item.tag.split("}")[-1] != "Contents":
                    continue
                name = _text(item, "Key")
                if name:
                    keys.append((name, int(_text(item, "Size") or 0)))
            token = _text(root, "NextContinuationToken") or None
            if _text(root, "IsTruncated") != "true" or not token:
                return keys

    async def delete_prefix(self, prefix: str) -> tuple[int, int]:
        """删除前缀下的全部对象，返回（对象数，字节数）。"""
        if not prefix.strip("/"):
            raise ValueError("refusing to delete the whole bucket")
        objects = await self.list_keys(prefix)
        for name, _ in objects:
            await self.delete(name)
        return len(objects), sum(size for _, size in objects)

    async def ping(self) -> None:
        """健康检查：能列举存储空间。"""
        await self._request("GET", "", query={"list-type": "2", "max-keys": "1"})

    # ---- 签发给浏览器的地址 ----

    def presign(
        self,
        method: str,
        key: str,
        *,
        expires: int,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> str:
        """签发给浏览器的地址。now 可以取整（例如到整点），同一时段内地址不变，浏览器能缓存。"""
        return self.signer.presign(
            method, key, expires=expires, query=query, headers=headers, now=now
        )
