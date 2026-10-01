"""常见邮箱的收发信服务器（设计文档 §10.8）。选择邮箱类型后自动带出，可以在高级设置里改。"""

from dataclasses import dataclass

from app.modules.mail.models import Security


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    security: Security


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    # 按地址的域名自动选择这个邮箱类型。
    domains: tuple[str, ...]
    imap: Endpoint
    smtp: Endpoint
    # 密码输入框的名称（授权码、应用专用密码……）和怎么获取。
    secret_label: str
    help: str


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        "netease163",
        "163 邮箱",
        ("163.com",),
        Endpoint("imap.163.com", 993, Security.SSL),
        Endpoint("smtp.163.com", 465, Security.SSL),
        "授权码",
        "网页版登录 163 邮箱，在「设置 → POP3/SMTP/IMAP」里开启 IMAP/SMTP 服务，按提示生成授权码"
        "（不是登录密码）。",
    ),
    Provider(
        "netease126",
        "126 邮箱",
        ("126.com",),
        Endpoint("imap.126.com", 993, Security.SSL),
        Endpoint("smtp.126.com", 465, Security.SSL),
        "授权码",
        "网页版登录 126 邮箱，在「设置 → POP3/SMTP/IMAP」里开启 IMAP/SMTP 服务，按提示生成授权码。",
    ),
    Provider(
        "yeah",
        "yeah.net 邮箱",
        ("yeah.net",),
        Endpoint("imap.yeah.net", 993, Security.SSL),
        Endpoint("smtp.yeah.net", 465, Security.SSL),
        "授权码",
        "网页版登录 yeah.net 邮箱，在「设置 → POP3/SMTP/IMAP」里开启 IMAP/SMTP 服务，按提示生成"
        "授权码。",
    ),
    Provider(
        "qq",
        "QQ 邮箱",
        ("qq.com", "foxmail.com", "vip.qq.com"),
        Endpoint("imap.qq.com", 993, Security.SSL),
        Endpoint("smtp.qq.com", 465, Security.SSL),
        "授权码",
        "网页版登录 QQ 邮箱，在「设置 → 账号」里开启 IMAP/SMTP 服务，按提示生成授权码"
        "（不是 QQ 密码）。",
    ),
    Provider(
        "exmail",
        "腾讯企业邮",
        (),
        Endpoint("imap.exmail.qq.com", 993, Security.SSL),
        Endpoint("smtp.exmail.qq.com", 465, Security.SSL),
        "邮箱密码或客户端专用密码",
        "开启了「安全登录」（微信动态密码）时，在网页版「设置 → 客户端设置」里生成客户端专用密码；"
        "同时确认开启了 IMAP/SMTP 服务。",
    ),
    Provider(
        "qiye163",
        "网易企业邮",
        (),
        Endpoint("imap.qiye.163.com", 993, Security.SSL),
        Endpoint("smtp.qiye.163.com", 994, Security.SSL),
        "邮箱密码或客户端授权码",
        "管理员开启了客户端授权码时，在网页版「设置 → 客户端设置」里生成；同时确认开启了 IMAP/SMTP "
        "服务。",
    ),
    Provider(
        "aliyun",
        "阿里企业邮",
        (),
        Endpoint("imap.qiye.aliyun.com", 993, Security.SSL),
        Endpoint("smtp.qiye.aliyun.com", 465, Security.SSL),
        "邮箱密码",
        "在网页版「设置 → 账户与安全 → 客户端设置」里确认开启了 IMAP/SMTP 服务。",
    ),
    Provider(
        "gmail",
        "Gmail",
        ("gmail.com", "googlemail.com"),
        Endpoint("imap.gmail.com", 993, Security.SSL),
        Endpoint("smtp.gmail.com", 465, Security.SSL),
        "应用专用密码",
        "在 Google 账号的「安全性」里开启两步验证，再生成应用专用密码（16 位）。服务器需要能访问 "
        "Google 的邮件服务器。",
    ),
    Provider(
        "custom",
        "其他邮箱",
        (),
        Endpoint("", 993, Security.SSL),
        Endpoint("", 465, Security.SSL),
        "密码",
        "填写邮箱服务商提供的 IMAP、SMTP 服务器地址和端口（在邮箱的帮助中心或「客户端设置」里可以"
        "找到）。",
    ),
)

BY_KEY = {p.key: p for p in PROVIDERS}
CUSTOM = BY_KEY["custom"]


def provider_of(key: str) -> Provider | None:
    return BY_KEY.get(key)


def guess(address: str) -> Provider:
    """按地址的域名猜邮箱类型；猜不出时是"其他邮箱"。"""
    domain = address.rpartition("@")[2].strip().lower()
    for provider in PROVIDERS:
        if domain in provider.domains:
            return provider
    return CUSTOM
