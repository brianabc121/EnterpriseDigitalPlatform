"""解析收到的邮件（设计文档 §10.8）。

- 头部按原文自己解码（编码词、RFC 2231），GBK、GB2312 声明的内容按 GB18030 解码：国内邮件
  常把 GBK 字符标成 GB2312，标准库按声明解码会出乱码。
- 正文优先用纯文本，只有 HTML 时转成文字；回复里引用的历史内容和新写的部分分开。
- 附件：带文件名或声明为附件的部分；正文 HTML 里引用的内嵌图片（cid:）不算附件。
- 自动回复、退信、群发、本邮箱发出的邮件、忽略的发件人不导入（skip_reason）。
"""

import email
import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header
from email.message import Message
from email.policy import compat32
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

TEXT_LIMIT = 20_000
QUOTED_LIMIT = 20_000

_CHARSETS = {"gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030", "cp936": "gb18030"}


def _charset(name: str | None) -> str:
    name = (name or "").strip().strip('"').lower()
    return _CHARSETS.get(name, name or "utf-8")


def decode_bytes(data: bytes, charset: str | None) -> str:
    """按声明的编码解码；编码不认识或解码失败时依次试 UTF-8、GB18030。"""
    for candidate in (_charset(charset), "utf-8", "gb18030"):
        try:
            return data.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _unescape(value: str) -> str:
    """直接写了 8 位字节的头部（解析时以 surrogateescape 保留）：按 UTF-8、GB18030 解码。"""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return decode_bytes(value.encode("utf-8", "surrogateescape"), None)
    return value


def clean(value: str) -> str:
    """去掉数据库存不了的空字符。"""
    return value.replace("\x00", "")


def decode_words(value: object) -> str:
    """头部里的编码词（=?GBK?B?...?=）。"""
    if not value:
        return ""
    value = _unescape(str(value))
    parts: list[str] = []
    try:
        chunks = decode_header(value)
    except Exception:  # 格式不对的头部原样返回
        return " ".join(value.split())
    for chunk, charset in chunks:
        if isinstance(chunk, bytes):
            parts.append(decode_bytes(chunk, charset))
        else:
            parts.append(chunk)
    return clean(" ".join("".join(parts).split()))


@dataclass(frozen=True)
class Address:
    name: str
    address: str  # 小写

    def show(self) -> str:
        return f"{self.name} <{self.address}>" if self.name else self.address

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "address": self.address}


def addresses(value: object) -> list[Address]:
    if not value:
        return []
    found: list[Address] = []
    for name, address in getaddresses([_unescape(str(value))]):
        address = address.strip().lower()
        if "@" not in address or len(address) > 254:
            continue
        found.append(Address(decode_words(name).strip('"').strip()[:128], address))
    return found


@dataclass
class Part:
    name: str
    mime: str
    data: bytes
    content_id: str | None
    inline: bool


@dataclass
class Parsed:
    subject: str
    sender: Address | None
    reply_to: Address | None
    to: list[Address]
    cc: list[Address]
    date: datetime | None
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    # 新写的部分、折叠的引用；只有 HTML 正文时 html 为 True（"查看原邮件"里看原样）。
    text: str
    quoted: str
    html: bool
    attachments: list[Part] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)


_IDS = re.compile(r"<[^<>\s]+>")


def _message_id(value: str | None) -> str | None:
    found = _IDS.search(value or "")
    return found.group(0)[:250] if found else None


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _filename(part: Message) -> str | None:
    name = part.get_filename()
    if name is None:
        name = part.get_param("name")  # type: ignore[assignment]
        if isinstance(name, tuple):
            name = email.utils.collapse_rfc2231_value(name)
    if not name:
        return None
    return decode_words(str(name)).strip()[:200] or None


def _payload(part: Message) -> bytes:
    data = part.get_payload(decode=True)
    return data if isinstance(data, bytes) else b""


def _content_id(part: Message) -> str | None:
    value = str(part.get("Content-ID") or "").strip().strip("<>").strip()
    return value[:250] or None


def _raw(message: Message, name: str) -> str | None:
    """头部原文（不经过标准库的清理：直接写了 8 位字节的头部，清理后只剩替换字符）。"""
    name = name.lower()
    for key, value in message.raw_items():
        if key.lower() == name:
            return str(value)
    return None


def parse(raw: bytes) -> Parsed:
    message = email.message_from_bytes(raw, policy=compat32)
    headers = {
        key.lower(): decode_words(str(value))
        for key, value in message.raw_items()
        if key.lower()
        in (
            "auto-submitted",
            "x-autoreply",
            "x-autorespond",
            "precedence",
            "list-id",
            "list-unsubscribe",
            "content-type",
        )
    }
    plain: str | None = None
    rich: str | None = None
    attachments: list[Part] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        kind = part.get_content_type()
        disposition = part.get_content_disposition()
        name = _filename(part)
        if kind == "message/rfc822" or (part is not message and kind.startswith("message/")):
            inner = part.get_payload()
            data = (
                inner[0].as_bytes()
                if isinstance(inner, list) and inner and isinstance(inner[0], Message)
                else _payload(part)
            )
            attachments.append(Part(name or "附件邮件.eml", "message/rfc822", data, None, False))
            continue
        is_attachment = disposition == "attachment" or (
            name is not None and disposition != "inline"
        )
        if kind == "text/plain" and not is_attachment and plain is None:
            plain = decode_bytes(_payload(part), part.get_content_charset())
            continue
        if kind == "text/html" and not is_attachment and rich is None:
            rich = decode_bytes(_payload(part), part.get_content_charset())
            continue
        if kind.startswith("multipart/"):
            continue
        data = _payload(part)
        if not data:
            continue
        cid = _content_id(part)
        extension = kind.rsplit("/", 1)[-1] if "/" in kind else "bin"
        attachments.append(
            Part(
                name or f"附件{len(attachments) + 1}.{extension}",
                kind,
                data,
                cid,
                disposition == "inline" or (name is None and cid is not None),
            )
        )
    body = plain if plain is not None and plain.strip() else html_to_text(rich or "")
    new, quoted = split_quoted(body)
    if rich:
        # 正文 HTML 里引用的内嵌图片在"查看原邮件"里显示，不单独列为附件。
        attachments = [
            a for a in attachments if not (a.content_id and f"cid:{a.content_id}" in rich)
        ]
    senders = addresses(_raw(message, "From"))
    reply_to = addresses(_raw(message, "Reply-To"))
    references = _IDS.findall(_raw(message, "References") or "")[-20:]
    return Parsed(
        subject=decode_words(_raw(message, "Subject"))[:500],
        sender=senders[0] if senders else None,
        reply_to=reply_to[0] if reply_to else None,
        to=addresses(_raw(message, "To"))[:50],
        cc=addresses(_raw(message, "Cc"))[:50],
        date=_date(_raw(message, "Date")),
        message_id=_message_id(_raw(message, "Message-ID")),
        in_reply_to=_message_id(_raw(message, "In-Reply-To")),
        references=references,
        text=clean(new[:TEXT_LIMIT]),
        quoted=clean(quoted[:QUOTED_LIMIT]),
        html=bool(rich) and not (plain and plain.strip()),
        attachments=attachments,
        headers=headers,
    )


# ---- HTML 转文字 ----


class _Text(HTMLParser):
    BLOCKS = frozenset(
        {"p", "div", "tr", "table", "section", "article", "header", "footer", "ul", "ol",
         "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr"}
    )  # fmt: skip
    SKIP = frozenset({"script", "style", "head", "title", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skipping += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in ("ul", "ol"):
            # 列表项自己换行，列表开头不再空一行。
            pass
        elif tag in ("td", "th"):
            self.parts.append(" ")
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(re.sub(r"[ \t\r\n\f\v]+", " ", data))


def html_to_text(value: str) -> str:
    if not value:
        return ""
    parser = _Text()
    try:
        parser.feed(value)
        parser.close()
    except Exception:  # 解析不了的 HTML 去掉标签
        return html.unescape(re.sub(r"<[^>]+>", " ", value)).strip()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [line.strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ---- 引用的历史内容 ----

_QUOTE_STARTS = (
    re.compile(
        r"^-{2,}\s*(原始邮件|原邮件|Original Message|Forwarded message|转发邮件)\s*-{2,}$", re.I
    ),
    re.compile(r"^在.{0,160}写道\s*[:：]$"),
    re.compile(r"^On .{0,300}wrote:$", re.I),
    re.compile(r"^_{10,}$"),
)
_HEADER_START = re.compile(r"^(发件人|From)\s*[:：]", re.I)
_HEADER_NEXT = re.compile(
    r"^(发送时间|发送日期|日期|时间|Sent|Date|收件人|To|主题|Subject)\s*[:：]", re.I
)


def _quote_start(lines: list[str]) -> int | None:
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        joined = f"{line} {lines[i + 1].strip()}" if i + 1 < len(lines) else line
        if any(p.match(line) for p in _QUOTE_STARTS) or (
            _QUOTE_STARTS[2].match(joined) and line.lower().startswith("on ")
        ):
            return i
        if _HEADER_START.match(line) and any(
            _HEADER_NEXT.match(n.strip()) for n in lines[i + 1 : i + 4]
        ):
            return i
    # 末尾连续以 ">" 开头的行。
    tail = len(lines)
    while tail > 0 and (not lines[tail - 1].strip() or lines[tail - 1].lstrip().startswith(">")):
        tail -= 1
    if tail < len(lines) and any(line.lstrip().startswith(">") for line in lines[tail:]):
        return tail
    return None


def split_quoted(text: str) -> tuple[str, str]:
    """（新写的部分，引用的历史内容）。整封都是引用时不拆。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    start = _quote_start(lines)
    if start is None:
        return text.strip(), ""
    new = "\n".join(lines[:start]).strip()
    if not new:
        return text.strip(), ""
    return new, "\n".join(lines[start:]).strip()


# ---- 要不要导入 ----

_SYSTEM_SENDERS = ("mailer-daemon", "postmaster")
_NO_REPLY = ("noreply", "no-reply", "donotreply", "do-not-reply", "do_not_reply")


def skip_reason(parsed: Parsed, mailbox: str, ignore: list[str]) -> str | None:
    """不导入的原因；要导入时返回空。"""
    sender = parsed.sender
    if sender is None:
        return "没有发件人"
    if sender.address == mailbox.lower():
        return "本邮箱发出的邮件"
    local, _, domain = sender.address.partition("@")
    content_type = parsed.headers.get("content-type", "").lower()
    if local in _SYSTEM_SENDERS or "multipart/report" in content_type:
        return "退信"
    auto = parsed.headers.get("auto-submitted", "").strip().lower()
    if (
        (auto and auto != "no")
        or "x-autoreply" in parsed.headers
        or "x-autorespond" in parsed.headers
    ):
        return "自动回复"
    precedence = parsed.headers.get("precedence", "").strip().lower()
    if precedence == "auto_reply":
        return "自动回复"
    if precedence in ("bulk", "junk", "list") or "list-id" in parsed.headers:
        return "群发邮件"
    if "list-unsubscribe" in parsed.headers or local in _NO_REPLY:
        return "群发邮件"
    for rule in ignore:
        rule = rule.strip().lower()
        if rule and (sender.address == rule or (rule.startswith("@") and f"@{domain}" == rule)):
            return "忽略的发件人"
    return None


# ---- 主题 ----

_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|sv)\s*[:：]|(回复|答复|转发)\s*[:：])\s*", re.I)


def base_subject(subject: str) -> str:
    """去掉 "Re:"、"回复："、"Fwd:" 等前缀。"""
    previous = None
    while previous != subject:
        previous = subject
        subject = _PREFIX.sub("", subject)
    return subject.strip()


def reply_subject(subject: str) -> str:
    base = base_subject(subject)
    return f"Re: {base}" if base else "Re: 您的来信"
