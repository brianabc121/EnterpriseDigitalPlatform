"""真实 OpenIM 契约测试用的回调应答器。

开发环境的 OpenIM 把回调发到宿主机的 8000 端口（deploy/compose/openim/docker-compose.yml），
建群前回调不通就拒绝建群。本地通常有开发后端在监听；CI 的后端测试作业没有，所以契约测试在
端口空闲时自己应答，答复和后端的回调入口一样（app.modules.conversation.hooks.decide）。
"""

import asyncio
import contextlib
import json

from app.modules.conversation import hooks


class HookResponder:
    """监听 OpenIM 回调端口的最小 HTTP 服务，只处理 OpenIM 发来的 POST。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.commands: list[str] = []
        self._server: asyncio.Server | None = None

    @property
    def active(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        """端口空闲时开始监听；已有进程（开发后端）在监听时什么也不做。"""
        try:
            self._server = await asyncio.start_server(
                self._handle, "0.0.0.0", self.port, reuse_address=True
            )
        except OSError:
            self._server = None

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            request_line, _, header_lines = head.partition(b"\r\n")
            length = 0
            for line in header_lines.split(b"\r\n"):
                name, sep, value = line.partition(b":")
                if sep and name.strip().lower() == b"content-length":
                    length = int(value.strip())
            body = await reader.readexactly(length) if length else b""
            command = request_line.split()[1].decode().rsplit("/", 1)[-1]
            self.commands.append(command)
            reply = json.dumps(hooks.decide(command, json.loads(body or b"{}"))).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(reply)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + reply
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ValueError, IndexError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
