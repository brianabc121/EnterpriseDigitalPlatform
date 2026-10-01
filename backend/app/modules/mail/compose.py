"""组装回复的邮件（设计文档 §10.8）：Re: 主题、In-Reply-To 和 References 指向原邮件，正文后面
附上签名和引用的原邮件；同时生成纯文本和 HTML。"""

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from zoneinfo import ZoneInfo

QUOTE_LINES = 200
_LOCAL = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class Original:
    """被回复的那封邮件：引用到回复里。"""

    sender_name: str
    sender_address: str
    sent_at: datetime
    text: str


@dataclass(frozen=True)
class Attachment:
    name: str
    mime: str
    data: bytes


def message_id(local: str, address: str) -> str:
    domain = address.rpartition("@")[2] or "localhost"
    return f"<{local}@{domain}>"


def _quote_head(original: Original) -> str:
    when = original.sent_at.astimezone(_LOCAL).strftime("%Y-%m-%d %H:%M")
    who = (
        f"{original.sender_name} <{original.sender_address}>"
        if original.sender_name
        else original.sender_address
    )
    return f"在 {when}，{who} 写道："


def _plain(text: str, signature: str | None, original: Original | None) -> str:
    parts = [text.rstrip()]
    if signature and signature.strip():
        parts.append(f"--\n{signature.strip()}")
    if original is not None and original.text.strip():
        lines = original.text.strip().split("\n")[:QUOTE_LINES]
        parts.append(_quote_head(original) + "\n" + "\n".join(f"> {line}" for line in lines))
    return "\n\n".join(parts) + "\n"


def _html_block(text: str) -> str:
    return html.escape(text).replace("\n", "<br>\n")


def _html(text: str, signature: str | None, original: Original | None) -> str:
    parts = [f"<div>{_html_block(text.rstrip())}</div>"]
    if signature and signature.strip():
        parts.append(f'<div style="color:#666">--<br>\n{_html_block(signature.strip())}</div>')
    if original is not None and original.text.strip():
        lines = "\n".join(original.text.strip().split("\n")[:QUOTE_LINES])
        parts.append(
            f"<div>{html.escape(_quote_head(original))}</div>"
            '<blockquote style="margin:0 0 0 .8ex;border-left:1px solid #ccc;padding-left:1ex">'
            f"{_html_block(lines)}</blockquote>"
        )
    body = "<br>\n".join(parts)
    return (
        '<!doctype html><html><head><meta charset="utf-8"></head>'
        f"<body style=\"font-family:-apple-system,'Microsoft YaHei',sans-serif;font-size:14px\">"
        f"{body}</body></html>"
    )


def compose(
    *,
    sender_name: str,
    sender_address: str,
    to_name: str,
    to_address: str,
    subject: str,
    text: str,
    signature: str | None,
    original: Original | None,
    msg_id: str,
    in_reply_to: str | None,
    references: list[str],
    attachments: list[Attachment],
    now: datetime | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender_address)) if sender_name else sender_address
    message["To"] = formataddr((to_name, to_address)) if to_name else to_address
    message["Subject"] = subject
    message["Date"] = format_datetime(now or datetime.now(UTC))
    message["Message-ID"] = msg_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        chain = [*[r for r in references if r != in_reply_to][-19:], in_reply_to]
        message["References"] = " ".join(chain)
    # 告诉对方的邮件系统这是人工回复，不要再自动回复（避免来回循环）。
    message["Auto-Submitted"] = "no"
    message.set_content(_plain(text, signature, original))
    message.add_alternative(_html(text, signature, original), subtype="html")
    for attachment in attachments:
        maintype, _, subtype = attachment.mime.partition("/")
        message.add_attachment(
            attachment.data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.name,
        )
    return message
