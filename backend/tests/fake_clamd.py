"""模拟的 clamd（INSTREAM、PING）：内容里带 EICAR 测试串的判为感染。

uv run python -m tests.fake_clamd --port 3310
"""

import argparse
import asyncio
import struct

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
SIGNATURE = "Eicar-Test-Signature"


class FakeClamd:
    def __init__(self) -> None:
        self.scanned: list[bytes] = []
        self.server: asyncio.Server | None = None

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self.server = await asyncio.start_server(self._handle, host, port)
        return int(self.server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            command = await reader.readuntil(b"\0")
            if command == b"zPING\0":
                writer.write(b"PONG\0")
            elif command == b"zINSTREAM\0":
                data = bytearray()
                while True:
                    (size,) = struct.unpack("!I", await reader.readexactly(4))
                    if size == 0:
                        break
                    data += await reader.readexactly(size)
                self.scanned.append(bytes(data))
                found = EICAR in data
                writer.write(f"stream: {SIGNATURE} FOUND\0".encode() if found else b"stream: OK\0")
            else:
                writer.write(b"UNKNOWN COMMAND\0")
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def _serve(host: str, port: int) -> None:
    clamd = FakeClamd()
    await clamd.start(host, port)
    print(f"fake clamd listening on {host}:{port}", flush=True)
    await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3310)
    args = parser.parse_args()
    asyncio.run(_serve(args.host, args.port))


if __name__ == "__main__":
    main()
