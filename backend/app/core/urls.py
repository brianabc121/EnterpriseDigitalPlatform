"""租户填写的外部地址（例如自带的大模型接口、推送地址、要抓取的网站）。

生产环境只允许公网 https 地址，防止借平台访问内网。只检查一次不够：域名先解析到公网地址通过检查，
连接时再解析到内网地址（DNS 重绑定）。所以发请求前用 resolve_outbound 解析并检查一次，然后就
连这个地址（Host 和 TLS 的 SNI 仍然是原来的域名，证书照常校验）。
"""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.errors import Unprocessable

_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost")


@dataclass(frozen=True)
class OutboundTarget:
    """检查过的外部地址。request_url 是实际连接的地址（生产环境里域名换成了解析到的 IP）。"""

    url: str
    request_url: str
    headers: dict[str, str] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


async def _public_addresses(url: str) -> list[str]:
    """校验地址并解析域名；返回解析到的地址（都必须是公网地址）。"""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise Unprocessable("只支持 https 地址")
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        raise Unprocessable("不能使用内网地址")
    try:
        addresses = await _resolve(host, parts.port or 443)
    except socket.gaierror as exc:
        raise Unprocessable("无法解析这个地址的域名") from exc
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise Unprocessable("不能使用内网地址")
    return addresses


def _clean(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Unprocessable("地址格式不正确")
    return url.strip().rstrip("/")


async def check_outbound_url(url: str, *, allow_private: bool) -> str:
    """校验并返回去掉末尾斜杠的地址。allow_private 为 true 时（开发、测试环境）不限制。"""
    cleaned = _clean(url)
    if not allow_private:
        await _public_addresses(cleaned)
    return cleaned


async def resolve_outbound(url: str, *, allow_private: bool) -> OutboundTarget:
    """发请求前调用：检查地址，并把域名固定为这次解析到的地址。

    返回值里的 request_url、headers、extensions 一起交给 httpx：连接解析到的 IP，Host 头和
    SNI 仍是原来的域名，证书按域名校验。allow_private 为 true 时原样返回。
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Unprocessable("地址格式不正确")
    if allow_private:
        return OutboundTarget(url=url.strip(), request_url=url.strip())
    addresses = await _public_addresses(url.strip())
    address = ipaddress.ip_address(addresses[0])
    literal = f"[{address}]" if address.version == 6 else str(address)
    netloc = literal if parts.port is None else f"{literal}:{parts.port}"
    host_header = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    return OutboundTarget(
        url=url.strip(),
        request_url=urlunsplit((parts.scheme, netloc, parts.path, parts.query, "")),
        headers={"Host": host_header},
        extensions={"sni_hostname": parts.hostname},
    )
