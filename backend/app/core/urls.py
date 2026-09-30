"""租户填写的外部地址（例如自带的大模型接口）。

生产环境只允许公网 https 地址，防止借平台访问内网。
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from app.core.errors import Unprocessable

_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost")


async def check_outbound_url(url: str, *, allow_private: bool) -> str:
    """校验并返回去掉末尾斜杠的地址。allow_private 为 true 时（开发、测试环境）不限制。"""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Unprocessable("地址格式不正确")
    if allow_private:
        return url.strip().rstrip("/")
    if parts.scheme != "https":
        raise Unprocessable("只支持 https 地址")
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        raise Unprocessable("不能使用内网地址")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parts.port or 443, proto=socket.IPPROTO_TCP
        )
    except socket.gaierror as exc:
        raise Unprocessable("无法解析这个地址的域名") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise Unprocessable("不能使用内网地址")
    return url.strip().rstrip("/")
