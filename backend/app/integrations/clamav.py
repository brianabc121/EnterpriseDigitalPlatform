"""ClamAV（clamd）病毒扫描：INSTREAM 协议，把文件内容分块发给 clamd，读取一行结果。

结果形如 "stream: OK"、"stream: Eicar-Test-Signature FOUND" 或 "... ERROR"。
"""

import asyncio
import contextlib
import struct
from dataclasses import dataclass

CHUNK = 64 * 1024


class ScanError(Exception):
    """clamd 不可用或返回了错误。"""


@dataclass(frozen=True)
class ScanResult:
    clean: bool
    signature: str | None = None


def parse(reply: str) -> ScanResult:
    reply = reply.strip().rstrip("\0").strip()
    _, _, verdict = reply.partition(": ")
    if verdict == "OK":
        return ScanResult(clean=True)
    if verdict.endswith(" FOUND"):
        return ScanResult(clean=False, signature=verdict[: -len(" FOUND")])
    raise ScanError(reply or "empty reply")


class ClamAV:
    def __init__(self, host: str, port: int = 3310, *, timeout: float = 30.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    async def scan(self, data: bytes) -> ScanResult:
        try:
            return await asyncio.wait_for(self._scan(data), self.timeout)
        except (OSError, TimeoutError) as exc:
            raise ScanError(f"clamd unavailable: {exc!r}") from exc

    async def _scan(self, data: bytes) -> ScanResult:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            writer.write(b"zINSTREAM\0")
            for start in range(0, len(data), CHUNK):
                chunk = data[start : start + CHUNK]
                writer.write(struct.pack("!I", len(chunk)) + chunk)
                await writer.drain()
            writer.write(struct.pack("!I", 0))
            await writer.drain()
            reply = await reader.readuntil(b"\0")
        except asyncio.IncompleteReadError as exc:
            raise ScanError("connection closed by clamd") from exc
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        return parse(reply.decode(errors="replace"))

    async def ping(self) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout
            )
        except (OSError, TimeoutError):
            return False
        try:
            writer.write(b"zPING\0")
            await writer.drain()
            reply = await asyncio.wait_for(reader.readuntil(b"\0"), self.timeout)
            return reply.rstrip(b"\0") == b"PONG"
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            return False
        finally:
            writer.close()
