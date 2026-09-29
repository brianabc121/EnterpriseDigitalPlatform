"""模拟外部网站（抓取帮助中心的测试）：按完整地址返回预先设置的响应，没有设置的地址返回 404。"""

from dataclasses import dataclass, field

import httpx


@dataclass
class _Response:
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class FakeWeb:
    def __init__(self) -> None:
        self.responses: dict[str, _Response] = {}
        self.requests: list[str] = []

    def page(
        self,
        url: str,
        html: str,
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.responses[url] = _Response(status, content_type, html.encode())

    def text(self, url: str, body: str) -> None:
        self.responses[url] = _Response(200, "text/plain; charset=utf-8", body.encode())

    def redirect(self, url: str, location: str, status: int = 302) -> None:
        self.responses[url] = _Response(status, "text/html", b"", {"location": location})

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            self.requests.append(url)
            found = self.responses.get(url)
            if found is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(
                found.status,
                headers={"content-type": found.content_type, **found.headers},
                content=found.body,
            )

        return httpx.MockTransport(handler)
