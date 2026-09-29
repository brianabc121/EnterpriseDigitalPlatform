"""内存对象存储：按路径（/{bucket}/{key}）保存 PUT 的内容，支持 GET、DELETE 和 ListObjectsV2。"""

from urllib.parse import unquote
from xml.sax.saxutils import escape

import httpx


class FakeStorage:
    def __init__(self, bucket: str = "edp-files") -> None:
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, str]] = {}

    def object_keys(self) -> list[str]:
        """对象 key（不含存储桶）。"""
        prefix = f"/{self.bucket}/"
        return sorted(p[len(prefix) :] for p in self.objects if p.startswith(prefix))

    def get(self, key: str) -> bytes:
        return self.objects[f"/{self.bucket}/{key}"][0]

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = unquote(request.url.path)
            if request.method == "HEAD":
                return httpx.Response(200)
            if request.method == "PUT":
                self.objects[path] = (request.content, request.headers.get("content-type", ""))
                return httpx.Response(200)
            if request.method == "DELETE":
                self.objects.pop(path, None)
                return httpx.Response(204)
            if request.method == "GET" and request.url.params.get("list-type") == "2":
                return self._list(path.strip("/"), request.url.params.get("prefix", ""))
            if request.method == "GET" and path in self.objects:
                data, content_type = self.objects[path]
                return httpx.Response(200, content=data, headers={"content-type": content_type})
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    def _list(self, bucket: str, prefix: str) -> httpx.Response:
        root = f"/{bucket}/"
        contents = "".join(
            f"<Contents><Key>{escape(p[len(root) :])}</Key><Size>{len(data)}</Size></Contents>"
            for p, (data, _) in sorted(self.objects.items())
            if p.startswith(root + prefix)
        )
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Name>{bucket}</Name><IsTruncated>false</IsTruncated>{contents}</ListBucketResult>"
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/xml"}
        )
