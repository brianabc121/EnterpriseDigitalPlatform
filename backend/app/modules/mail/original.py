"""查看原邮件（设计文档 §10.8）：把保存的原文转成在沙箱 iframe 里显示的 HTML。

真正的保护是工作台的沙箱 iframe（不执行脚本、不同源）和这里加上的内容安全策略（不加载任何
外部资源，外部图片、跟踪像素都不会请求）；这里另外去掉脚本、表单、内嵌页面等元素和事件属性。
正文 HTML 里引用的内嵌图片（cid:）换成 data: 地址显示。
"""

import base64
import email
import html
import re
from email.message import Message
from email.policy import compat32
from urllib.parse import unquote

from app.modules.mail.parse import decode_bytes

HTML_LIMIT = 2_000_000
INLINE_IMAGE_LIMIT = 2 * 1024 * 1024
INLINE_TOTAL_LIMIT = 8 * 1024 * 1024

_POLICY = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; "
    "form-action 'none'"
)
_HEAD = (
    '<!doctype html><html><head><meta charset="utf-8">'
    f'<meta http-equiv="Content-Security-Policy" content="{_POLICY}">'
    '<base target="_blank">'
    "<style>body{margin:12px;font-family:-apple-system,'Microsoft YaHei',sans-serif;"
    "font-size:14px;color:#1f2329;word-break:break-word}img{max-width:100%}</style>"
    "</head><body>"
)
_TAIL = "</body></html>"

# 整个元素连同内容去掉。
_BLOCKS = re.compile(
    r"<(script|iframe|frame|frameset|object|embed|applet|noscript|template|form|svg|math)\b"
    r"[^>]*>.*?</\1\s*>",
    re.I | re.S,
)
# 只去掉标签（没有结束标签或者内容不用去掉的）。
_TAGS = re.compile(
    r"</?(script|iframe|frame|frameset|object|embed|applet|form|input|button|select|textarea|"
    r"link|meta|base|svg|math|template|noscript)\b[^>]*>",
    re.I,
)
_EVENTS = re.compile(r"""\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_SCRIPT_URLS = re.compile(
    r"""(href|src|action|formaction|xlink:href|background)\s*=\s*(["']?)\s*"""
    r"""(javascript|vbscript|data\s*:\s*text/html)[^"'\s>]*\2""",
    re.I,
)
_CID = re.compile(r"""cid:([^"'\s)>]+)""", re.I)


def wrap_text(text: str) -> str:
    """纯文字的邮件。"""
    body = html.escape(text[:HTML_LIMIT])
    return (
        f'{_HEAD}<pre style="white-space:pre-wrap;font-family:inherit;margin:0">{body}</pre>{_TAIL}'
    )


def sanitize(value: str) -> str:
    value = _BLOCKS.sub("", value)
    value = _TAGS.sub("", value)
    value = _EVENTS.sub("", value)
    return _SCRIPT_URLS.sub(r'\1=""', value)


def _payload(part: Message) -> bytes:
    data = part.get_payload(decode=True)
    return data if isinstance(data, bytes) else b""


def _inline_images(message: Message) -> dict[str, str]:
    """Content-ID → data: 地址（只要图片，单张和总量都有上限）。"""
    found: dict[str, str] = {}
    total = 0
    for part in message.walk():
        if part.is_multipart() or not part.get_content_type().startswith("image/"):
            continue
        cid = str(part.get("Content-ID") or "").strip().strip("<>").strip()
        if not cid:
            continue
        data = _payload(part)
        if not data or len(data) > INLINE_IMAGE_LIMIT or total + len(data) > INLINE_TOTAL_LIMIT:
            continue
        total += len(data)
        encoded = base64.b64encode(data).decode("ascii")
        found[cid] = f"data:{part.get_content_type()};base64,{encoded}"
    return found


def _bodies(message: Message) -> tuple[str | None, str | None]:
    """（HTML 正文，纯文本正文）：不是附件的第一个 text/html、text/plain 部分。"""
    rich: str | None = None
    plain: str | None = None
    for part in message.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment" or part.get_filename():
            continue
        kind = part.get_content_type()
        if kind == "text/html" and rich is None:
            rich = decode_bytes(_payload(part), part.get_content_charset())
        elif kind == "text/plain" and plain is None:
            plain = decode_bytes(_payload(part), part.get_content_charset())
    return rich, plain


def render(raw: bytes) -> str:
    message = email.message_from_bytes(raw, policy=compat32)
    rich, plain = _bodies(message)
    if not rich or not rich.strip():
        return wrap_text(plain or "")
    images = _inline_images(message)

    def inline(match: re.Match[str]) -> str:
        cid = unquote(match.group(1))
        return images.get(cid) or images.get(cid.strip("<>")) or "about:blank"

    body = _CID.sub(inline, sanitize(rich.replace("\x00", "")[:HTML_LIMIT]))
    return f"{_HEAD}{body}{_TAIL}"
