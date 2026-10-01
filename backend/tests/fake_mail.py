"""模拟的邮箱服务器：IMAP（收信）、SMTP（发信）和控制接口（/_fake/*）。

uv run python -m tests.fake_mail --imap-port 1143 --smtp-port 1025 --http-port 8903

- 邮箱（地址、授权码）由控制接口或 add_mailbox 创建；IMAP、SMTP 用地址和授权码登录。
- 只实现平台用到的命令：CAPABILITY、LOGIN、ID、SELECT、EXAMINE、STATUS、UID SEARCH、UID FETCH、
  NOOP、LOGOUT；EHLO、HELO、AUTH PLAIN/LOGIN、MAIL、RCPT、DATA、RSET、NOOP、QUIT。不加密。
- require_id 的邮箱模拟网易邮箱：登录后没有发 ID 就打开收件箱，返回 Unsafe Login。
- 用 BODY[]（不是 BODY.PEEK[]）取信、用 SELECT 打开会把邮件标记为已读（seen），
  用来检查平台不改变邮件的已读状态。
"""

import argparse
import asyncio
import base64
import contextlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from email.utils import format_datetime, formataddr, make_msgid
from typing import Any
from urllib.parse import unquote

HOST = "fake-mail"


@dataclass
class Stored:
    uid: int
    raw: bytes
    received_at: datetime


@dataclass
class Mailbox:
    address: str
    secret: str
    uidvalidity: int
    require_id: bool = False
    # 有的服务器打开收件箱时不给出 UIDNEXT（平台改为取最后一封的 UID）。
    advertise_uidnext: bool = True
    next_uid: int = 1
    messages: list[Stored] = field(default_factory=list)
    seen: set[int] = field(default_factory=set)
    logins: int = 0


@dataclass
class Sent:
    login: str
    mail_from: str
    rcpt_to: list[str]
    raw: bytes

    @property
    def message(self) -> EmailMessage:
        parsed = message_from_bytes(self.raw, policy=default)
        assert isinstance(parsed, EmailMessage)
        return parsed

    def text(self) -> str:
        body = self.message.get_body(("plain",))
        return str(body.get_content()) if body is not None else ""

    def attachments(self) -> list[tuple[str, bytes]]:
        return [
            (part.get_filename() or "", part.get_content())
            for part in self.message.iter_attachments()
        ]

    def summary(self) -> dict[str, Any]:
        message = self.message
        return {
            "login": self.login,
            "mail_from": self.mail_from,
            "rcpt_to": self.rcpt_to,
            "from": str(message["From"] or ""),
            "to": str(message["To"] or ""),
            "subject": str(message["Subject"] or ""),
            "message_id": str(message["Message-ID"] or ""),
            "in_reply_to": str(message["In-Reply-To"] or ""),
            "references": str(message["References"] or ""),
            "text": self.text(),
            "attachments": [name for name, _ in self.attachments()],
        }


def compose(
    *,
    sender: str,
    to: str,
    subject: str,
    text: str | None = None,
    html: str | None = None,
    sender_name: str = "",
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    headers: dict[str, str] | None = None,
    attachments: list[tuple[str, str, bytes]] | None = None,
    inline_images: list[tuple[str, str, bytes]] | None = None,
    date: datetime | None = None,
) -> bytes:
    """客户写的一封邮件。attachments、inline_images：(文件名或 Content-ID, 类型, 内容)。"""
    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = format_datetime(date or datetime.now(UTC))
    message["Message-ID"] = message_id or make_msgid(domain=sender.rpartition("@")[2])
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    for key, value in (headers or {}).items():
        message[key] = value
    if text is not None:
        message.set_content(text)
    if html is not None:
        if text is None:
            message.set_content(html, subtype="html")
        else:
            message.add_alternative(html, subtype="html")
        if inline_images:
            body = message.get_body(("html",))
            assert body is not None
            for cid, mime, data in inline_images:
                maintype, _, subtype = mime.partition("/")
                body.add_related(data, maintype=maintype, subtype=subtype, cid=f"<{cid}>")
    for name, mime, data in attachments or []:
        maintype, _, subtype = mime.partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return message.as_bytes(policy=default.clone(linesep="\r\n"))


# ---- IMAP ----


def _tokens(text: str) -> list[str]:
    """命令参数：原子、带引号的字符串、括号里的列表（原样）。"""
    tokens: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == " ":
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < len(text) and text[j] != '"':
                if text[j] == "\\" and j + 1 < len(text):
                    j += 1
                buf.append(text[j])
                j += 1
            tokens.append("".join(buf))
            i = j + 1
        elif c == "(":
            depth, j = 0, i
            while j < len(text):
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            tokens.append(text[i : j + 1])
            i = j + 1
        else:
            j = i
            while j < len(text) and text[j] != " ":
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def _in_set(spec: str, uid: int, largest: int) -> bool:
    """UID 集合："4"、"4:*"、"1,3:5"。按 RFC 3501，"n:*" 在 n 大于最大 UID 时也包含最后一封。"""
    for part in spec.split(","):
        lo_text, _, hi_text = part.partition(":")
        lo = largest if lo_text == "*" else int(lo_text)
        hi = lo if not hi_text else (largest if hi_text == "*" else int(hi_text))
        if min(lo, hi) <= uid <= max(lo, hi):
            return True
    return False


def _internaldate(value: datetime) -> str:
    return value.strftime("%d-%b-%Y %H:%M:%S %z")


def _header_block(raw: bytes) -> bytes:
    end = raw.find(b"\r\n\r\n")
    if end >= 0:
        return raw[: end + 4]
    end = raw.find(b"\n\n")
    return raw[: end + 2] if end >= 0 else raw


class _ImapConnection:
    def __init__(
        self, fake: "FakeMail", reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.fake = fake
        self.reader = reader
        self.writer = writer
        self.mailbox: Mailbox | None = None
        self.identified = False
        self.selected = False
        self.readonly = True

    def send(self, line: str | bytes) -> None:
        self.writer.write((line.encode() if isinstance(line, str) else line) + b"\r\n")

    async def run(self) -> None:
        self.send(f"* OK [CAPABILITY IMAP4rev1 ID] {HOST} IMAP ready")
        await self.writer.drain()
        while True:
            line = await self.reader.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\r\n")
            tag, _, rest = text.partition(" ")
            command, _, args = rest.partition(" ")
            if not await self.handle(tag, command.upper(), args):
                await self.writer.drain()
                return
            await self.writer.drain()

    async def handle(self, tag: str, command: str, args: str) -> bool:
        if command == "CAPABILITY":
            self.send("* CAPABILITY IMAP4rev1 ID")
            self.send(f"{tag} OK CAPABILITY completed")
        elif command == "NOOP":
            self.send(f"{tag} OK NOOP completed")
        elif command == "LOGOUT":
            self.send(f"* BYE {HOST} logging out")
            self.send(f"{tag} OK LOGOUT completed")
            return False
        elif command == "LOGIN":
            self.login(tag, _tokens(args))
        elif command == "ID":
            self.identified = True
            self.send(f'* ID ("name" "{HOST}")')
            self.send(f"{tag} OK ID completed")
        elif self.mailbox is None:
            self.send(f"{tag} BAD Please login first")
        elif command in ("SELECT", "EXAMINE"):
            self.select(tag, command, _tokens(args))
        elif command == "STATUS":
            box = self.mailbox
            self.send(
                f"* STATUS INBOX (MESSAGES {len(box.messages)} UIDNEXT {box.next_uid} "
                f"UIDVALIDITY {box.uidvalidity})"
            )
            self.send(f"{tag} OK STATUS completed")
        elif command == "UID" and self.selected:
            sub, _, rest = args.partition(" ")
            if sub.upper() == "SEARCH":
                self.search(tag, _tokens(rest))
            elif sub.upper() == "FETCH":
                self.fetch(tag, _tokens(rest))
            else:
                self.send(f"{tag} BAD Unsupported UID command")
        else:
            self.send(f"{tag} BAD Unknown or unexpected command {command}")
        return True

    def login(self, tag: str, args: list[str]) -> None:
        if len(args) != 2:
            self.send(f"{tag} BAD LOGIN needs user and password")
            return
        box = self.fake.mailboxes.get(args[0].lower())
        if box is None or box.secret != args[1] or self.fake.reject_logins:
            self.send(f"{tag} NO [AUTHENTICATIONFAILED] LOGIN Login error or password error")
            return
        box.logins += 1
        self.mailbox = box
        self.send(f"{tag} OK LOGIN completed")

    def select(self, tag: str, command: str, args: list[str]) -> None:
        box = self.mailbox
        assert box is not None
        if not args or args[0].upper() != "INBOX":
            self.send(f"{tag} NO Mailbox does not exist")
            return
        if box.require_id and not self.identified:
            self.send(f"{tag} NO {command} Unsafe Login. Please contact kefu@188.com for help")
            return
        self.selected = True
        self.readonly = command == "EXAMINE"
        if not self.readonly:
            box.seen.update(m.uid for m in box.messages)
        self.send(r"* FLAGS (\Answered \Flagged \Deleted \Seen \Draft)")
        self.send(f"* {len(box.messages)} EXISTS")
        self.send("* 0 RECENT")
        self.send(f"* OK [UIDVALIDITY {box.uidvalidity}] UIDs valid")
        if box.advertise_uidnext:
            self.send(f"* OK [UIDNEXT {box.next_uid}] Predicted next UID")
        mode = "READ-ONLY" if self.readonly else "READ-WRITE"
        self.send(f"{tag} OK [{mode}] {command} completed")

    def _largest(self) -> int:
        assert self.mailbox is not None
        return max((m.uid for m in self.mailbox.messages), default=0)

    def search(self, tag: str, args: list[str]) -> None:
        assert self.mailbox is not None
        largest = self._largest()
        if args and args[0].upper() == "UID" and len(args) > 1:
            found = [m.uid for m in self.mailbox.messages if _in_set(args[1], m.uid, largest)]
        elif args and args[0].upper() == "ALL":
            found = [m.uid for m in self.mailbox.messages]
        else:
            self.send(f"{tag} BAD Unsupported search")
            return
        self.send(("* SEARCH " + " ".join(str(u) for u in found)).rstrip())
        self.send(f"{tag} OK SEARCH completed")

    def fetch(self, tag: str, args: list[str]) -> None:
        box = self.mailbox
        assert box is not None
        if len(args) < 2:
            self.send(f"{tag} BAD FETCH needs a set and items")
            return
        items = args[1].strip("()").upper().split()
        largest = self._largest()
        for seq, stored in enumerate(box.messages, start=1):
            if not _in_set(args[0], stored.uid, largest):
                continue
            parts = [f"UID {stored.uid}"]
            literal: tuple[str, bytes] | None = None
            for item in items:
                if item == "RFC822.SIZE":
                    parts.append(f"RFC822.SIZE {len(stored.raw)}")
                elif item == "INTERNALDATE":
                    parts.append(f'INTERNALDATE "{_internaldate(stored.received_at)}"')
                elif item == "FLAGS":
                    parts.append("FLAGS (\\Seen)" if stored.uid in box.seen else "FLAGS ()")
                elif item in ("BODY.PEEK[]", "BODY[]", "RFC822"):
                    literal = ("RFC822" if item == "RFC822" else "BODY[]", stored.raw)
                    if item != "BODY.PEEK[]" and not self.readonly:
                        box.seen.add(stored.uid)
                elif item in ("BODY.PEEK[HEADER]", "BODY[HEADER]"):
                    literal = ("BODY[HEADER]", _header_block(stored.raw))
            head = f"* {seq} FETCH (" + " ".join(parts)
            if literal is None:
                self.send(head + ")")
            else:
                name, data = literal
                self.writer.write(f"{head} {name} {{{len(data)}}}\r\n".encode() + data)
                self.send(")")
        self.send(f"{tag} OK FETCH completed")


# ---- SMTP ----


class _SmtpConnection:
    def __init__(
        self, fake: "FakeMail", reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.fake = fake
        self.reader = reader
        self.writer = writer
        self.login: str | None = None
        self.mail_from: str | None = None
        self.rcpt_to: list[str] = []

    def send(self, line: str) -> None:
        self.writer.write(line.encode() + b"\r\n")

    async def line(self) -> str:
        data = await self.reader.readline()
        if not data:
            raise ConnectionError("closed")
        return data.decode(errors="replace").rstrip("\r\n")

    async def run(self) -> None:
        self.send(f"220 {HOST} ESMTP ready")
        await self.writer.drain()
        while True:
            text = await self.line()
            verb, _, rest = text.partition(" ")
            if not await self.handle(verb.upper(), rest):
                await self.writer.drain()
                return
            await self.writer.drain()

    def _check(self, user: str, secret: str) -> bool:
        box = self.fake.mailboxes.get(user.lower())
        if box is None or box.secret != secret or self.fake.reject_logins:
            self.send("535 Error: authentication failed")
            return False
        self.login = box.address
        self.send("235 2.7.0 Authentication successful")
        return True

    async def auth(self, rest: str) -> None:
        mechanism, _, initial = rest.partition(" ")
        mechanism = mechanism.upper()
        if mechanism == "PLAIN":
            if not initial:
                self.send("334 ")
                await self.writer.drain()
                initial = await self.line()
            _, user, secret = base64.b64decode(initial).decode().split("\0")
            self._check(user, secret)
        elif mechanism == "LOGIN":
            if not initial:
                self.send("334 VXNlcm5hbWU6")
                await self.writer.drain()
                initial = await self.line()
            user = base64.b64decode(initial).decode()
            self.send("334 UGFzc3dvcmQ6")
            await self.writer.drain()
            secret = base64.b64decode(await self.line()).decode()
            self._check(user, secret)
        else:
            self.send("504 5.5.4 Unrecognized authentication type")

    async def handle(self, verb: str, rest: str) -> bool:
        if verb == "EHLO":
            for line in (f"250-{HOST}", "250-AUTH PLAIN LOGIN", "250-8BITMIME", "250 SMTPUTF8"):
                self.send(line)
        elif verb == "HELO":
            self.send(f"250 {HOST}")
        elif verb == "AUTH":
            await self.auth(rest)
        elif verb == "NOOP":
            self.send("250 2.0.0 Ok")
        elif verb == "RSET":
            self.mail_from, self.rcpt_to = None, []
            self.send("250 2.0.0 Ok")
        elif verb == "QUIT":
            self.send("221 2.0.0 Bye")
            return False
        elif verb == "MAIL":
            if self.login is None:
                self.send("530 5.7.0 Authentication required")
                return True
            address = _angle(rest)
            if address.lower() != self.login.lower():
                self.send("553 Mail from must equal authorized user")
                return True
            self.mail_from, self.rcpt_to = address, []
            self.send("250 2.1.0 Ok")
        elif verb == "RCPT":
            address = _angle(rest)
            if self.mail_from is None:
                self.send("503 5.5.1 Error: need MAIL command")
            elif address.lower() in self.fake.reject_recipients:
                self.send("550 5.1.1 Mailbox not found")
            else:
                self.rcpt_to.append(address)
                self.send("250 2.1.5 Ok")
        elif verb == "DATA":
            await self.data()
        else:
            self.send("502 5.5.2 Error: command not recognized")
        return True

    async def data(self) -> None:
        if self.mail_from is None or not self.rcpt_to:
            self.send("503 5.5.1 Error: need RCPT command")
            return
        self.send("354 End data with <CR><LF>.<CR><LF>")
        await self.writer.drain()
        lines: list[bytes] = []
        while True:
            data = await self.reader.readline()
            if not data:
                raise ConnectionError("closed")
            if data in (b".\r\n", b".\n"):
                break
            lines.append(data[1:] if data.startswith(b"..") else data)
        if self.fake.temporary_failures > 0:
            self.fake.temporary_failures -= 1
            self.send("451 4.3.0 Try again later")
        else:
            assert self.login is not None
            self.fake.sent.append(Sent(self.login, self.mail_from, self.rcpt_to, b"".join(lines)))
            self.send(f"250 2.0.0 Ok: queued as {len(self.fake.sent)}")
        self.mail_from, self.rcpt_to = None, []


def _angle(rest: str) -> str:
    found = re.search(r"<([^>]*)>", rest)
    return found.group(1).strip() if found else rest.partition(":")[2].strip().split(" ")[0]


# ---- 服务 ----


class FakeMail:
    def __init__(self) -> None:
        self.mailboxes: dict[str, Mailbox] = {}
        self.sent: list[Sent] = []
        self.reject_recipients: set[str] = set()
        self.reject_logins = False
        # 接下来几次 DATA 返回 451（暂时失败）。
        self.temporary_failures = 0
        self.servers: list[asyncio.Server] = []
        self.imap_port = 0
        self.smtp_port = 0
        self._validity = int(datetime.now(UTC).timestamp())

    def add_mailbox(
        self,
        address: str,
        secret: str,
        *,
        require_id: bool = False,
        advertise_uidnext: bool = True,
    ) -> Mailbox:
        self._validity += 1
        box = Mailbox(
            address=address.lower(),
            secret=secret,
            uidvalidity=self._validity,
            require_id=require_id,
            advertise_uidnext=advertise_uidnext,
        )
        self.mailboxes[box.address] = box
        return box

    def deliver(self, address: str, raw: bytes, *, received_at: datetime | None = None) -> int:
        box = self.mailboxes[address.lower()]
        uid = box.next_uid
        box.next_uid += 1
        box.messages.append(Stored(uid, raw, received_at or datetime.now(UTC)))
        return uid

    def reset_uidvalidity(self, address: str) -> None:
        """邮箱重建（UIDVALIDITY 变了）：UID 重新从 1 开始。"""
        box = self.mailboxes[address.lower()]
        self._validity += 1
        box.uidvalidity = self._validity
        box.next_uid = 1
        for stored in box.messages:
            stored.uid = box.next_uid
            box.next_uid += 1

    async def start(self, host: str = "127.0.0.1", imap_port: int = 0, smtp_port: int = 0) -> None:
        imap = await asyncio.start_server(self._imap, host, imap_port)
        smtp = await asyncio.start_server(self._smtp, host, smtp_port)
        self.servers = [imap, smtp]
        self.imap_port = int(imap.sockets[0].getsockname()[1])
        self.smtp_port = int(smtp.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        for server in self.servers:
            server.close()
            await server.wait_closed()
        self.servers = []

    async def _imap(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self._serve(_ImapConnection(self, reader, writer).run(), writer)

    async def _smtp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await self._serve(_SmtpConnection(self, reader, writer).run(), writer)

    @staticmethod
    async def _serve(run: Any, writer: asyncio.StreamWriter) -> None:
        try:
            await run
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # ---- 控制接口（浏览器验收用） ----

    def control(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        if method == "POST" and path == "/_fake/reset":
            self.mailboxes.clear()
            self.sent.clear()
            self.reject_recipients.clear()
            self.reject_logins = False
            self.temporary_failures = 0
            return 200, {"ok": True}
        if method == "POST" and path == "/_fake/mailboxes":
            box = self.add_mailbox(
                str(body["address"]), str(body["secret"]), require_id=bool(body.get("require_id"))
            )
            return 200, {"address": box.address, "uidvalidity": box.uidvalidity}
        if method == "POST" and path == "/_fake/deliver":
            raw = (
                base64.b64decode(body["raw"])
                if body.get("raw")
                else compose(
                    sender=str(body["from"]),
                    sender_name=str(body.get("from_name") or ""),
                    to=str(body["to"]),
                    subject=str(body.get("subject") or ""),
                    text=body.get("text"),
                    html=body.get("html"),
                    message_id=body.get("message_id"),
                    in_reply_to=body.get("in_reply_to"),
                    references=body.get("references"),
                    headers=body.get("headers"),
                    attachments=[
                        (str(a["name"]), str(a["mime"]), base64.b64decode(a["data"]))
                        for a in body.get("attachments") or []
                    ],
                )
            )
            return 200, {"uid": self.deliver(str(body["to"]), raw)}
        if method == "GET" and path == "/_fake/sent":
            return 200, [sent.summary() for sent in self.sent]
        if method == "GET" and path.startswith("/_fake/mailboxes/"):
            box = self.mailboxes.get(unquote(path.rsplit("/", 1)[1]).lower())
            if box is None:
                return 404, {"error": "no such mailbox"}
            return 200, {
                "messages": len(box.messages),
                "seen": sorted(box.seen),
                "logins": box.logins,
            }
        return 404, {"error": "not found"}

    def asgi(self) -> Any:
        fake = self

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope["type"] != "http":
                return
            raw = b""
            while True:
                message = await receive()
                raw += message.get("body", b"")
                if not message.get("more_body"):
                    break
            status, payload = fake.control(
                scope["method"], scope["path"], json.loads(raw) if raw else {}
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": json.dumps(payload).encode()})

        return app


async def _serve(host: str, imap_port: int, smtp_port: int, http_port: int) -> None:
    import uvicorn

    fake = FakeMail()
    await fake.start(host, imap_port, smtp_port)
    print(
        f"fake mail: imap {host}:{fake.imap_port} smtp {host}:{fake.smtp_port} "
        f"control http://{host}:{http_port}",
        flush=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(fake.asgi(), host=host, port=http_port, log_level="warning")
    )
    await server.serve()


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟的邮箱服务器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--imap-port", type=int, default=1143)
    parser.add_argument("--smtp-port", type=int, default=1025)
    parser.add_argument("--http-port", type=int, default=8903)
    args = parser.parse_args()
    asyncio.run(_serve(args.host, args.imap_port, args.smtp_port, args.http_port))


if __name__ == "__main__":
    main()
