"""抓取官网帮助中心（设计文档 §12.1 冷启动）：从起始网页出发，按广度优先抓取同一站点、同一目录
下的网页，提取正文作为文档知识。

- 每一跳（包括跳转后的地址）都经过 check_outbound_url 检查，生产环境不能访问内网地址；
- 遵守 robots.txt；只抓取 HTML 页面，单页大小、页数有上限；
- 内容相同的页面只保留一个。
"""

import hashlib
import logging
from collections import deque
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.context import CRAWLER_AGENT, AppContext
from app.core.errors import Unprocessable
from app.core.urls import check_outbound_url
from app.modules.kb.parsers import html_to_document

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 5
MIN_TEXT = 30
_SKIP_EXT = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".css",
    ".js",
    ".json",
    ".xml",
    ".zip",
    ".rar",
    ".7z",
    ".gz",
    ".mp3",
    ".mp4",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".exe",
    ".dmg",
    ".apk",
)


class FetchError(Exception):
    pass


@dataclass
class Page:
    url: str
    title: str
    text: str


@dataclass
class CrawlResult:
    pages: list[Page]
    errors: list[str]
    fetched: int


def _canonical(url: str) -> str:
    return urldefrag(url)[0]


class _Scope:
    def __init__(self, start: str) -> None:
        parts = urlsplit(start)
        self.origin = (parts.scheme, parts.netloc.lower())
        path = parts.path or "/"
        self.prefix = path if path.endswith("/") else path.rsplit("/", 1)[0] + "/"

    def contains(self, url: str) -> bool:
        parts = urlsplit(url)
        if (parts.scheme, parts.netloc.lower()) != self.origin:
            return False
        path = parts.path or "/"
        return path.startswith(self.prefix) and not path.lower().endswith(_SKIP_EXT)


async def _get(
    ctx: AppContext, url: str, *, allow_private: bool, accept: tuple[str, ...]
) -> tuple[str, str]:
    """GET 一个地址（手动处理跳转，每一跳都检查地址），返回（最终地址, 文本）。"""
    limit = ctx.settings.kb_crawl_max_page_bytes
    for _ in range(MAX_REDIRECTS + 1):
        try:
            await check_outbound_url(url, allow_private=allow_private)
        except Unprocessable as exc:
            raise FetchError(exc.message) from exc
        try:
            async with ctx.web.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("跳转地址缺失")
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise FetchError(f"HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").lower()
                if not any(kind in content_type for kind in accept):
                    raise FetchError("不是网页")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > limit:
                        raise FetchError("网页太大")
                encoding = response.charset_encoding or "utf-8"
                try:
                    return url, bytes(body).decode(encoding, errors="replace")
                except LookupError:
                    return url, bytes(body).decode("utf-8", errors="replace")
        except httpx.HTTPError as exc:
            raise FetchError("无法访问") from exc
    raise FetchError("跳转次数过多")


async def _robots(ctx: AppContext, start: str, *, allow_private: bool) -> RobotFileParser | None:
    parts = urlsplit(start)
    robots = RobotFileParser()
    try:
        _, text = await _get(
            ctx,
            f"{parts.scheme}://{parts.netloc}/robots.txt",
            allow_private=allow_private,
            accept=("text/plain",),
        )
    except FetchError:
        return None
    robots.parse(text.splitlines())
    return robots


async def crawl(ctx: AppContext, start: str, *, max_pages: int, allow_private: bool) -> CrawlResult:
    try:
        await check_outbound_url(start, allow_private=allow_private)
    except Unprocessable as exc:
        return CrawlResult(pages=[], errors=[f"{start}：{exc.message}"], fetched=0)
    start = _canonical(start.strip())
    scope = _Scope(start)
    robots = await _robots(ctx, start, allow_private=allow_private)
    queue: deque[str] = deque([start])
    seen = {start}
    digests: set[str] = set()
    pages: list[Page] = []
    errors: list[str] = []
    fetched = 0
    # 没有正文的目录页也要抓（从中发现链接），但总请求数有上限。
    while queue and len(pages) < max_pages and fetched < max_pages * 3:
        url = queue.popleft()
        if robots is not None and not robots.can_fetch(CRAWLER_AGENT, url):
            errors.append(f"{url}：robots.txt 不允许抓取")
            continue
        fetched += 1
        try:
            final, html = await _get(
                ctx, url, allow_private=allow_private, accept=("text/html", "xhtml")
            )
        except FetchError as exc:
            errors.append(f"{url}：{exc}")
            continue
        final = _canonical(final)
        if final != url and not scope.contains(final):
            errors.append(f"{url}：跳转到了范围之外（{final}）")
            continue
        document = html_to_document(html)
        for link in document.links:
            target = _canonical(urljoin(final, link.strip()))
            if target not in seen and scope.contains(target):
                seen.add(target)
                queue.append(target)
        if len(document.text) < MIN_TEXT:
            continue
        digest = hashlib.sha256(document.text.encode()).hexdigest()
        if digest in digests:
            continue
        digests.add(digest)
        pages.append(Page(url=final, title=document.title or final, text=document.text))
    return CrawlResult(pages=pages, errors=errors, fetched=fetched)
