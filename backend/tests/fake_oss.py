"""模拟的阿里云 OSS（企业资料，设计文档 §36）：单元测试里作为 httpx 传输层，
浏览器验收时作为独立服务运行。

- 路径风格的地址：/{bucket}/{key}。按 OSS 的 V4 签名校验 Authorization 头和
  URL 签名：规范请求在这里按官方文档独立实现，不复用平台的代码，平台算错签名时
  返回 403（SignatureDoesNotMatch）；地址过期、AccessKey 不对也返回 403。
- PutObject、GetObject（Range、response-content-disposition、
  response-content-type；x-oss-process 的视频截帧和图片缩放返回占位图片）、
  HeadObject、DeleteObject、InitiateMultipartUpload、UploadPart、
  CompleteMultipartUpload、AbortMultipartUpload、ListObjectsV2。
- 跨域：OPTIONS 预检和所有响应都带 Access-Control-Allow-Origin，暴露 ETag
  （和配置好跨域的 Bucket 一样）。
- 控制接口（独立运行时）：GET /_fake/objects 列出保存的对象。

独立运行：uv run python -m tests.fake_oss --port 8905
"""

import argparse
import base64
import hashlib
import hmac
import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote

import httpx

ACCESS_KEY_ID = "LTAIfakeOssAccessKey"
ACCESS_KEY_SECRET = "fake-oss-access-key-secret"
BUCKET = "edp-materials"
REGION = "cn-hangzhou"
# 1×1 的 PNG：视频截帧和图片缩放的占位结果。
PLACEHOLDER = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
CORS = {
    "access-control-allow-origin": "*",
    "access-control-expose-headers": "ETag, x-oss-request-id, Content-Range",
}


@dataclass
class StoredObject:
    data: bytes
    content_type: str
    etag: str
    modified: float


def _encode(value: str, *, keep_slash: bool = False) -> str:
    return quote(value, safe="-_.~" + ("/" if keep_slash else ""))


def _query(raw: str) -> list[tuple[str, str]]:
    """查询串（不把 + 当空格）。"""
    pairs = []
    for part in raw.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        pairs.append((unquote(name), unquote(value)))
    return pairs


def _signing_key(date: str) -> bytes:
    key = hmac.new(f"aliyun_v4{ACCESS_KEY_SECRET}".encode(), date.encode(), hashlib.sha256)
    for part in (REGION, "oss", "aliyun_v4_request"):
        key = hmac.new(key.digest(), part.encode(), hashlib.sha256)
    return key.digest()


def _error(status: int, code: str, message: str = "") -> httpx.Response:
    body = f"<Error><Code>{code}</Code><Message>{message or code}</Message></Error>"
    return httpx.Response(
        status, content=body.encode(), headers={**CORS, "content-type": "application/xml"}
    )


@dataclass
class FakeOSS:
    objects: dict[str, StoredObject] = field(default_factory=dict)
    uploads: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    # 接下来的几个请求返回 503（模拟 OSS 不可用）。
    fail_count: int = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def reset(self) -> None:
        self.objects.clear()
        self.uploads.clear()
        self.requests.clear()
        self.fail_count = 0

    # ---- 签名 ----

    def _verify(
        self, request: httpx.Request, bucket: str, key: str, params: list[tuple[str, str]]
    ) -> httpx.Response | None:
        query = dict(params)
        headers = {name.lower(): value for name, value in request.headers.items()}
        if "x-oss-signature" in query:
            if query.get("x-oss-signature-version") != "OSS4-HMAC-SHA256":
                return _error(403, "SignatureDoesNotMatch", "bad signature version")
            credential = query.get("x-oss-credential", "")
            timestamp = query.get("x-oss-date", "")
            signature = query["x-oss-signature"]
            try:
                expires = int(query.get("x-oss-expires", ""))
                signed_at = datetime.strptime(timestamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            except ValueError:
                return _error(403, "AccessDenied", "bad x-oss-date or x-oss-expires")
            if not 1 <= expires <= 7 * 24 * 3600:
                return _error(403, "AccessDenied", "bad x-oss-expires")
            if signed_at.timestamp() + expires < time.time():
                return _error(403, "AccessDenied", "Request has expired.")
            signed_params = [(k, v) for k, v in params if k != "x-oss-signature"]
            additional = [h for h in query.get("x-oss-additional-headers", "").split(";") if h]
        else:
            auth = headers.get("authorization", "")
            if not auth.startswith("OSS4-HMAC-SHA256 "):
                return _error(403, "AccessDenied", "missing V4 authorization")
            fields = dict(
                part.strip().split("=", 1)
                for part in auth[len("OSS4-HMAC-SHA256 ") :].split(",")
                if "=" in part
            )
            credential = fields.get("Credential", "")
            signature = fields.get("Signature", "")
            additional = [h for h in fields.get("AdditionalHeaders", "").split(";") if h]
            timestamp = headers.get("x-oss-date", "")
            if headers.get("x-oss-content-sha256") != "UNSIGNED-PAYLOAD":
                return _error(403, "AccessDenied", "x-oss-content-sha256 must be UNSIGNED-PAYLOAD")
            signed_params = params
        parts = credential.split("/")
        if len(parts) != 5 or parts[0] != ACCESS_KEY_ID:
            return _error(403, "InvalidAccessKeyId")
        if parts[2:] != [REGION, "oss", "aliyun_v4_request"] or parts[1] != timestamp[:8]:
            return _error(403, "SignatureDoesNotMatch", "bad credential scope")
        canonical_uri = _encode(f"/{bucket}/{key}" if bucket else "/", keep_slash=True)
        pairs = sorted((_encode(k), _encode(v)) for k, v in signed_params)
        canonical_query = "&".join(f"{k}={v}" if v else k for k, v in pairs)
        picked = {
            name: " ".join(value.strip().split())
            for name, value in headers.items()
            if name.startswith("x-oss-")
            or name in ("content-type", "content-md5")
            or name in additional
        }
        canonical_headers = "".join(f"{name}:{picked[name]}\n" for name in sorted(picked))
        canonical = "\n".join(
            [
                request.method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                ";".join(sorted(additional)),
                "UNSIGNED-PAYLOAD",
            ]
        )
        scope = f"{timestamp[:8]}/{REGION}/oss/aliyun_v4_request"
        to_sign = (
            f"OSS4-HMAC-SHA256\n{timestamp}\n{scope}\n"
            + hashlib.sha256(canonical.encode()).hexdigest()
        )
        expected = hmac.new(_signing_key(timestamp[:8]), to_sign.encode(), hashlib.sha256)
        if not hmac.compare_digest(expected.hexdigest(), signature):
            return _error(403, "SignatureDoesNotMatch", canonical)
        return None

    # ---- 请求分发 ----

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "OPTIONS":
            return httpx.Response(
                200,
                headers={
                    **CORS,
                    "access-control-allow-methods": "GET, PUT, POST, HEAD, DELETE",
                    "access-control-allow-headers": request.headers.get(
                        "access-control-request-headers", "*"
                    ),
                    "access-control-max-age": "600",
                },
            )
        if self.fail_count > 0:
            self.fail_count -= 1
            return _error(503, "ServiceUnavailable")
        raw = request.url.raw_path.decode()
        path, _, query_string = raw.partition("?")
        if path.startswith("/_fake/"):
            return self._control(path)
        segments = path.split("/", 2)
        bucket = unquote(segments[1]) if len(segments) > 1 else ""
        key = unquote(segments[2]) if len(segments) > 2 else ""
        params = _query(query_string)
        denied = self._verify(request, bucket, key, params)
        if denied is not None:
            return denied
        if bucket != BUCKET:
            return _error(404, "NoSuchBucket")
        query = dict(params)
        method = request.method
        if not key:
            if method == "GET" and query.get("list-type") == "2":
                return self._list(query)
            return _error(400, "InvalidRequest")
        if method == "POST" and "uploads" in query:
            return self._initiate(key, request)
        if "uploadId" in query:
            if method == "PUT":
                return self._upload_part(key, query, request)
            if method == "POST":
                return self._complete(key, query, request)
            if method == "DELETE":
                self.uploads.pop(query["uploadId"], None)
                return httpx.Response(204, headers=CORS)
        if method == "PUT":
            return self._put(key, request.content, request.headers.get("content-type", ""))
        if method in ("GET", "HEAD"):
            return self._get(key, query, request)
        if method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204, headers=CORS)
        return _error(405, "MethodNotAllowed")

    def _put(self, key: str, data: bytes, content_type: str) -> httpx.Response:
        etag = hashlib.md5(data).hexdigest().upper()
        self.objects[key] = StoredObject(
            data, content_type or "application/octet-stream", etag, time.time()
        )
        return httpx.Response(200, headers={**CORS, "etag": f'"{etag}"'})

    def _get(self, key: str, query: dict[str, str], request: httpx.Request) -> httpx.Response:
        stored = self.objects.get(key)
        if stored is None:
            return _error(404, "NoSuchKey")
        process = query.get("x-oss-process", "")
        if process.startswith(("video/snapshot", "image/")):
            return httpx.Response(
                200, content=PLACEHOLDER, headers={**CORS, "content-type": "image/png"}
            )
        headers = {
            **CORS,
            "etag": f'"{stored.etag}"',
            "content-type": query.get("response-content-type") or stored.content_type,
            "accept-ranges": "bytes",
            "last-modified": datetime.fromtimestamp(stored.modified, UTC).strftime(
                "%a, %d %b %Y %H:%M:%S GMT"
            ),
        }
        if query.get("response-content-disposition"):
            headers["content-disposition"] = query["response-content-disposition"]
        data = stored.data
        status = 200
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("range", ""))
        if match and data:
            start = int(match.group(1) or 0)
            end = min(int(match.group(2) or len(data) - 1), len(data) - 1)
            if start <= end:
                headers["content-range"] = f"bytes {start}-{end}/{len(data)}"
                data = data[start : end + 1]
                status = 206
        if request.method == "HEAD":
            headers["content-length"] = str(len(stored.data))
            return httpx.Response(status, headers=headers)
        return httpx.Response(status, content=data, headers=headers)

    def _initiate(self, key: str, request: httpx.Request) -> httpx.Response:
        upload_id = uuid.uuid4().hex.upper()
        self.uploads[upload_id] = {
            "key": key,
            "content_type": request.headers.get("content-type", "application/octet-stream"),
            "parts": {},
        }
        body = (
            f"<InitiateMultipartUploadResult><Bucket>{BUCKET}</Bucket><Key>{key}</Key>"
            f"<UploadId>{upload_id}</UploadId></InitiateMultipartUploadResult>"
        )
        return httpx.Response(
            200, content=body.encode(), headers={**CORS, "content-type": "application/xml"}
        )

    def _upload_part(
        self, key: str, query: dict[str, str], request: httpx.Request
    ) -> httpx.Response:
        upload = self.uploads.get(query["uploadId"])
        if upload is None or upload["key"] != key:
            return _error(404, "NoSuchUpload")
        try:
            number = int(query.get("partNumber", ""))
        except ValueError:
            return _error(400, "InvalidArgument")
        etag = hashlib.md5(request.content).hexdigest().upper()
        upload["parts"][number] = (request.content, etag)
        return httpx.Response(200, headers={**CORS, "etag": f'"{etag}"'})

    def _complete(self, key: str, query: dict[str, str], request: httpx.Request) -> httpx.Response:
        upload = self.uploads.get(query["uploadId"])
        if upload is None or upload["key"] != key:
            return _error(404, "NoSuchUpload")
        try:
            root = ET.fromstring(request.content)
        except ET.ParseError:
            return _error(400, "MalformedXML")
        listed = [
            (int(part.findtext("PartNumber") or 0), (part.findtext("ETag") or "").strip('"'))
            for part in root.iter("Part")
        ]
        if not listed or [n for n, _ in listed] != sorted(n for n, _ in listed):
            return _error(400, "InvalidPartOrder")
        chunks = []
        for number, etag in listed:
            stored = upload["parts"].get(number)
            if stored is None or stored[1] != etag:
                return _error(400, "InvalidPart")
            chunks.append(stored[0])
        data = b"".join(chunks)
        joined = hashlib.md5("".join(e for _, e in listed).encode()).hexdigest().upper()
        etag = f"{joined}-{len(listed)}"
        self.objects[key] = StoredObject(data, upload["content_type"], etag, time.time())
        del self.uploads[query["uploadId"]]
        body = (
            f"<CompleteMultipartUploadResult><Bucket>{BUCKET}</Bucket><Key>{key}</Key>"
            f"<ETag>&quot;{etag}&quot;</ETag></CompleteMultipartUploadResult>"
        )
        return httpx.Response(
            200, content=body.encode(), headers={**CORS, "content-type": "application/xml"}
        )

    def _list(self, query: dict[str, str]) -> httpx.Response:
        prefix = query.get("prefix", "")
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        limit = int(query.get("max-keys") or 1000)
        start = int(query.get("continuation-token") or 0)
        page = keys[start : start + limit]
        truncated = start + limit < len(keys)
        contents = "".join(
            f"<Contents><Key>{k}</Key><Size>{len(self.objects[k].data)}</Size></Contents>"
            for k in page
        )
        token = (
            f"<NextContinuationToken>{start + limit}</NextContinuationToken>" if truncated else ""
        )
        flag = "true" if truncated else "false"
        body = (
            f"<ListBucketResult><Name>{BUCKET}</Name><Prefix>{prefix}</Prefix>{contents}"
            f"<IsTruncated>{flag}</IsTruncated>{token}</ListBucketResult>"
        )
        return httpx.Response(
            200, content=body.encode(), headers={**CORS, "content-type": "application/xml"}
        )

    def _control(self, path: str) -> httpx.Response:
        if path == "/_fake/objects":
            items = [
                {"key": k, "size": len(o.data), "content_type": o.content_type}
                for k, o in sorted(self.objects.items())
            ]
            return httpx.Response(200, json={"objects": items, "uploads": len(self.uploads)})
        return httpx.Response(404, json={"error": "unknown control endpoint"})

    # ---- 独立运行 ----

    def asgi(self) -> Any:
        fake = self

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] != "http":
                return
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            headers = [(k.decode(), v.decode()) for k, v in scope.get("headers", [])]
            raw_path = scope.get("raw_path") or scope["path"].encode()
            target = raw_path.decode()
            if scope.get("query_string"):
                target += "?" + scope["query_string"].decode()
            request = httpx.Request(
                scope["method"], f"http://fake-oss{target}", headers=headers, content=body
            )
            response = fake.handle(request)
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": [
                        (k.encode(), v.encode())
                        for k, v in response.headers.items()
                        if k.lower() != "transfer-encoding"
                    ],
                }
            )
            await send({"type": "http.response.body", "body": response.content})

        return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="模拟的阿里云 OSS")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8905)
    args = parser.parse_args()
    print(f"fake oss on http://{args.host}:{args.port}/{BUCKET} ({json.dumps(REGION)})")
    uvicorn.run(FakeOSS().asgi(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
