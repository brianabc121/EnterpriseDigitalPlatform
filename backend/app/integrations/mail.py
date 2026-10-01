"""IMAP 收信、SMTP 发信（设计文档 §10.8）。

用标准库 imaplib、smtplib，阻塞调用由调用方放到线程里执行（asyncio.to_thread）。

- 连接前解析服务器地址：不允许内网、本机等地址（防止借邮箱设置探测内网），端口只允许常用端口；
  然后连接解析出的 IP，证书按服务器域名校验（防止 DNS 重新绑定）。开发和测试环境可以放开
  （EDP_MAIL_ALLOW_PRIVATE_HOSTS），这时也允许不加密的连接。
- 错误分两类：MailError 是邮箱拒绝（登录失败、收件人被拒、配置不对），重试也没有用；
  MailUnavailable 是网络、超时、服务器暂时不可用，稍后重试。
"""

import contextlib
import imaplib
import ipaddress
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any, Literal

# IMAP ID（RFC 2971）：网易邮箱要求登录后先发 ID，否则拒绝打开收件箱（Unsafe Login）。
imaplib.Commands.setdefault("ID", ("NONAUTH", "AUTH", "SELECTED"))

IMAP_PORTS = frozenset({993, 143})
SMTP_PORTS = frozenset({465, 587, 25, 994})
CLIENT_ID = '("name" "EDP" "version" "1.0" "vendor" "EDP")'

Kind = Literal["auth", "rejected", "config"]


class MailError(Exception):
    """邮箱拒绝：重试也没有用（登录失败、收件人被拒、服务器或端口不对）。"""

    def __init__(self, message: str, *, kind: Kind = "rejected") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind


class MailUnavailable(Exception):
    """网络、超时或服务器暂时不可用：稍后重试。"""


@dataclass(frozen=True)
class Server:
    host: str
    port: int
    security: str  # ssl、starttls、none
    username: str
    password: str


@dataclass(frozen=True)
class Inbox:
    uidvalidity: int
    # 下一封新邮件的 UID（服务器没有给出时为空）。
    uidnext: int | None
    exists: int


@dataclass(frozen=True)
class Fetched:
    uid: int
    size: int
    # 邮件原文；超过大小上限时只有头部（truncated）。
    raw: bytes | None
    received_at: datetime | None
    truncated: bool = False


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, (list, tuple)):
        return " ".join(_text(v) for v in value if v)
    return str(value or "")


def _context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _resolve(
    host: str, port: int, allowed: frozenset[int], *, allow_private: bool
) -> tuple[str, int]:
    """检查端口和地址，返回要连接的 (IP, 端口)。"""
    host = host.strip()
    if not host or len(host) > 255 or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host):
        raise MailError(f"服务器地址不正确：{host or '（空）'}", kind="config")
    if not 0 < port < 65536:
        raise MailError(f"端口不正确：{port}", kind="config")
    if allow_private:
        return host, port
    if port not in allowed:
        ports = "、".join(str(p) for p in sorted(allowed))
        raise MailError(f"端口 {port} 不在允许的范围内（{ports}）", kind="config")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise MailError(f"找不到服务器 {host}（{exc.strerror or exc}）", kind="config") from exc
    for *_, sockaddr in infos:
        address = ipaddress.ip_address(sockaddr[0])
        if not address.is_global:
            raise MailError(f"不允许连接内网或本机地址：{host}", kind="config")
    return str(infos[0][4][0]), port


def _describe_os_error(exc: BaseException) -> str:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"证书校验失败（{exc.verify_message}）"
    if isinstance(exc, ssl.SSLError):
        return f"加密连接失败（{exc.reason or exc}），检查端口和加密方式"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "连接超时"
    if isinstance(exc, ConnectionRefusedError):
        return "连接被拒绝"
    return str(exc) or exc.__class__.__name__


# ---- IMAP ----


class _Imap(imaplib.IMAP4):
    def __init__(self, host: str, port: int, timeout: float, address: tuple[str, int]) -> None:
        self._address = address
        super().__init__(host, port, timeout=timeout)

    def _create_socket(self, timeout: float | None) -> socket.socket:
        return socket.create_connection(self._address, timeout)


class _ImapSsl(imaplib.IMAP4_SSL):
    def __init__(self, host: str, port: int, timeout: float, address: tuple[str, int]) -> None:
        self._address = address
        super().__init__(host, port, ssl_context=_context(), timeout=timeout)

    def _create_socket(self, timeout: float | None) -> socket.socket:
        sock = socket.create_connection(self._address, timeout)
        wrapped: socket.socket = self.ssl_context.wrap_socket(sock, server_hostname=self.host)
        return wrapped


_UID = re.compile(rb"UID (\d+)")
_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")
_INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"')


class ImapSession:
    """一次 IMAP 连接：登录（需要时发 ID）、只读打开收件箱、按 UID 收取。"""

    def __init__(self, server: Server, *, timeout: float, allow_private: bool) -> None:
        self.server = server
        self.timeout = timeout
        self.allow_private = allow_private
        self.client: imaplib.IMAP4 | None = None

    def __enter__(self) -> "ImapSession":
        server = self.server
        if server.security == "none" and not self.allow_private:
            raise MailError("收信必须使用加密连接（SSL 或 STARTTLS）", kind="config")
        address = _resolve(server.host, server.port, IMAP_PORTS, allow_private=self.allow_private)
        try:
            if server.security == "ssl":
                client: imaplib.IMAP4 = _ImapSsl(server.host, server.port, self.timeout, address)
            else:
                client = _Imap(server.host, server.port, self.timeout, address)
                if server.security == "starttls":
                    client.starttls(ssl_context=_context())
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailUnavailable(
                f"连接收信服务器 {server.host}:{server.port} 失败：{_describe_os_error(exc)}"
            ) from exc
        self.client = client
        try:
            client.login(server.username, server.password)
        except imaplib.IMAP4.error as exc:
            self._close()
            reason = _text(exc.args)
            raise MailError(
                f"邮箱拒绝登录：{reason}（检查邮箱地址、授权码，以及是否开启了 IMAP 服务）",
                kind="auth",
            ) from exc
        except OSError as exc:
            self._close()
            raise MailUnavailable(f"登录收信服务器失败：{_describe_os_error(exc)}") from exc
        if "ID" in client.capabilities:
            with contextlib.suppress(imaplib.IMAP4.error, OSError):
                client._simple_command("ID", CLIENT_ID)
        return self

    def __exit__(self, *_: object) -> None:
        self._close()

    def _close(self) -> None:
        client, self.client = self.client, None
        if client is None:
            return
        # 退出时的错误不影响结果。
        with contextlib.suppress(Exception):
            client.logout()

    def _call(self, what: str, fn: Any, *args: Any) -> tuple[str, list[Any]]:
        try:
            typ, data = fn(*args)
        except imaplib.IMAP4.abort as exc:
            raise MailUnavailable(f"{what}时连接断开：{_text(exc.args)}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailError(f"{what}失败：{_text(exc.args)}") from exc
        except OSError as exc:
            raise MailUnavailable(f"{what}失败：{_describe_os_error(exc)}") from exc
        if typ != "OK":
            raise MailError(f"{what}失败：{_text(data)}")
        return typ, data

    def open_inbox(self) -> Inbox:
        """只读打开收件箱（EXAMINE：不改变邮件的已读状态）。"""
        assert self.client is not None
        _, data = self._call("打开收件箱", self.client.select, "INBOX", True)
        exists = int(_text(data[0]) or 0) if data and data[0] else 0
        validity = self.client.response("UIDVALIDITY")[1]
        uidnext = self.client.response("UIDNEXT")[1]
        if not validity or validity[0] is None:
            _, status = self._call(
                "读取收件箱状态", self.client.status, "INBOX", "(UIDVALIDITY UIDNEXT)"
            )
            line = _text(status[0] if status else b"")
            found = re.search(r"UIDVALIDITY (\d+)", line)
            validity = [found.group(1).encode()] if found else [b"0"]
            nxt = re.search(r"UIDNEXT (\d+)", line)
            uidnext = [nxt.group(1).encode()] if nxt else [None]
        next_uid = uidnext[0] if uidnext else None
        return Inbox(
            uidvalidity=int(_text(validity[0]) or 0),
            uidnext=int(_text(next_uid)) if next_uid else None,
            exists=exists,
        )

    def last_uid(self, inbox: Inbox) -> int:
        """收件箱里最后一封邮件的 UID（空收件箱为 0）。"""
        if inbox.uidnext:
            return inbox.uidnext - 1
        if not inbox.exists:
            return 0
        assert self.client is not None
        _, data = self._call("读取最后一封邮件", self.client.uid, "FETCH", "*", "(UID)")
        uids = [int(m.group(1)) for item in data for m in [_UID.search(_bytes(item))] if m]
        return max(uids, default=0)

    def uids_after(self, uid: int) -> list[int]:
        """UID 大于 uid 的邮件（按 UID 从小到大）。"""
        assert self.client is not None
        _, data = self._call("查找新邮件", self.client.uid, "SEARCH", None, f"UID {uid + 1}:*")
        found = sorted({int(x) for x in _text(data[0] if data else b"").split() if x.isdigit()})
        # "n:*" 在没有更大的 UID 时也会返回最后一封，这里去掉。
        return [u for u in found if u > uid]

    def fetch(self, uids: list[int], *, max_bytes: int) -> list[Fetched]:
        """先取大小，再逐封取原文（BODY.PEEK：不标记已读）；超过 max_bytes 的只取头部。"""
        if not uids:
            return []
        assert self.client is not None
        _, data = self._call(
            "读取邮件大小",
            self.client.uid,
            "FETCH",
            ",".join(str(u) for u in uids),
            "(UID RFC822.SIZE INTERNALDATE)",
        )
        meta: dict[int, tuple[int, datetime | None]] = {}
        for item in data:
            line = _bytes(item)
            found_uid, found_size = _UID.search(line), _SIZE.search(line)
            if found_uid is None:
                continue
            meta[int(found_uid.group(1))] = (
                int(found_size.group(1)) if found_size else 0,
                _internaldate(line),
            )
        result: list[Fetched] = []
        for uid in uids:
            if uid not in meta:
                continue
            size, received = meta[uid]
            truncated = size > max_bytes
            section = "(BODY.PEEK[HEADER])" if truncated else "(BODY.PEEK[])"
            _, body = self._call("读取邮件", self.client.uid, "FETCH", str(uid), section)
            raw = next((bytes(part[1]) for part in body if isinstance(part, tuple)), None)
            result.append(
                Fetched(uid=uid, size=size, raw=raw, received_at=received, truncated=truncated)
            )
        return result


def _internaldate(line: bytes) -> datetime | None:
    """INTERNALDATE "01-Oct-2026 09:30:00 +0800"（服务器收到邮件的时间）。"""
    found = _INTERNALDATE.search(line)
    if found is None:
        return None
    try:
        stamp = datetime.strptime(found.group(1).decode().strip(), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None
    return stamp.astimezone(UTC)


def _bytes(item: Any) -> bytes:
    if isinstance(item, tuple):
        return bytes(item[0])
    return bytes(item) if isinstance(item, (bytes, bytearray)) else b""


# ---- SMTP ----


class _Smtp(smtplib.SMTP):
    def __init__(self, host: str, address: tuple[str, int], timeout: float) -> None:
        self._address = address
        super().__init__(timeout=timeout)
        # STARTTLS 时按服务器域名校验证书。
        self._host = host

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        return socket.create_connection(self._address, timeout)


class _SmtpSsl(smtplib.SMTP_SSL):
    def __init__(self, host: str, address: tuple[str, int], timeout: float) -> None:
        self._address = address
        super().__init__(timeout=timeout, context=_context())
        self._host = host

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        sock = socket.create_connection(self._address, timeout)
        return self.context.wrap_socket(sock, server_hostname=self._host)


def _smtp_connect(server: Server, *, timeout: float, allow_private: bool) -> smtplib.SMTP:
    if server.security == "none" and not allow_private:
        raise MailError("发信必须使用加密连接（SSL 或 STARTTLS）", kind="config")
    address = _resolve(server.host, server.port, SMTP_PORTS, allow_private=allow_private)
    client: smtplib.SMTP
    try:
        kind = _SmtpSsl if server.security == "ssl" else _Smtp
        client = kind(server.host, address, timeout)
        client.connect(server.host, server.port)
        client.ehlo()
        if server.security == "starttls":
            client.starttls(context=_context())
            client.ehlo()
    except smtplib.SMTPNotSupportedError as exc:
        raise MailError("发信服务器不支持 STARTTLS，换成 SSL 试试", kind="config") from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise MailUnavailable(
            f"连接发信服务器 {server.host}:{server.port} 失败：{_describe_os_error(exc)}"
        ) from exc
    try:
        client.login(server.username, server.password)
    except smtplib.SMTPAuthenticationError as exc:
        _quit(client)
        raise MailError(
            f"邮箱拒绝登录发信服务器：{_smtp_reason(exc)}（检查授权码，以及是否开启了 SMTP 服务）",
            kind="auth",
        ) from exc
    except smtplib.SMTPNotSupportedError as exc:
        _quit(client)
        raise MailError("发信服务器不支持登录，检查端口和加密方式", kind="config") from exc
    except (OSError, smtplib.SMTPException) as exc:
        _quit(client)
        raise MailUnavailable(f"登录发信服务器失败：{_describe_os_error(exc)}") from exc
    return client


def _decoded(value: bytes | str) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)


def _smtp_reason(exc: smtplib.SMTPResponseException) -> str:
    return f"{exc.smtp_code} {_decoded(exc.smtp_error)}".strip()


def _quit(client: smtplib.SMTP) -> None:
    with contextlib.suppress(Exception):
        client.quit()


def check_smtp(server: Server, *, timeout: float, allow_private: bool) -> None:
    """登录发信服务器（不发信）。"""
    _quit(_smtp_connect(server, timeout=timeout, allow_private=allow_private))


def send(
    server: Server,
    message: EmailMessage,
    *,
    sender: str,
    recipients: list[str],
    timeout: float,
    allow_private: bool,
) -> None:
    client = _smtp_connect(server, timeout=timeout, allow_private=allow_private)
    try:
        refused = client.send_message(message, from_addr=sender, to_addrs=recipients)
    except smtplib.SMTPRecipientsRefused as exc:
        reasons = "；".join(
            f"{address}：{code} {_decoded(text)}"
            for address, (code, text) in exc.recipients.items()
        )
        raise MailError(f"收件人地址被拒绝：{reasons}") from exc
    except smtplib.SMTPSenderRefused as exc:
        raise MailError(f"发件人被拒绝：{_smtp_reason(exc)}") from exc
    except smtplib.SMTPDataError as exc:
        if 400 <= exc.smtp_code < 500:
            raise MailUnavailable(f"发信服务器暂时拒绝：{_smtp_reason(exc)}") from exc
        raise MailError(f"发信服务器拒绝了这封邮件：{_smtp_reason(exc)}") from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise MailUnavailable(f"发信失败：{_describe_os_error(exc)}") from exc
    finally:
        _quit(client)
    if refused:
        raise MailError("收件人地址被拒绝：" + "、".join(refused))
