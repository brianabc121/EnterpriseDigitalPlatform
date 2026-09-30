"""模拟外部网站：按完整地址返回预先设置的响应，没有设置的地址返回 404。

- 抓取帮助中心的测试：page、text、redirect。
- 事件推送的测试：receiver 模拟企业系统接收推送的地址，记录收到的请求；可以让前几次返回失败。
"""

from dataclasses import dataclass, field

import httpx


@dataclass
class _Response:
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Received:
    url: str
    headers: dict[str, str]
    body: bytes


class FakeWeb:
    def __init__(self) -> None:
        self.responses: dict[str, _Response] = {}
        self.requests: list[str] = []
        # 推送接收地址：还要返回失败的次数（之后返回 200）。
        self.receivers: dict[str, int] = {}
        self.received: list[Received] = []

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

    def receiver(self, url: str, *, fail: int = 0) -> None:
        """接收推送的地址：前 fail 次返回 500，之后返回 200。"""
        self.receivers[url] = fail

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            self.requests.append(url)
            if url in self.receivers and request.method == "POST":
                self.received.append(Received(url, dict(request.headers), request.content))
                if self.receivers[url] > 0:
                    self.receivers[url] -= 1
                    return httpx.Response(500, text="try later")
                return httpx.Response(200, json={"ok": True})
            found = self.responses.get(url)
            if found is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(
                found.status,
                headers={"content-type": found.content_type, **found.headers},
                content=found.body,
            )

        return httpx.MockTransport(handler)
