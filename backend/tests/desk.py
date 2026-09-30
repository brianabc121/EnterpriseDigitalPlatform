"""会话相关测试的场景工具：一个租户、若干坐席和访客，以及"让消息流动起来"的 flush。

flush 模拟生产环境里各进程的协作：把内存版 OpenIM 产生的回调投递给平台（入库并发布事件），
再由实时消费进程处理事件（归入会话、排队分配），直到没有新的回调和事件为止。
"""

import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI

from app.context import AppContext
from app.core.config import Settings
from app.events.bus import EventProcessor
from app.modules.ai.responder import run_due
from app.modules.conversation import imids
from app.modules.sessions.handlers import event_handlers
from tests.factories import STAFF_PASSWORD, bearer, create_staff, login, provision
from tests.fake_openim import FakeOpenIM
from tests.support import DatabaseUrls
from tests.test_openim_hooks import deliver
from tests.test_visitor import channel_key, init


@dataclass(frozen=True)
class Agent:
    staff_id: uuid.UUID
    username: str
    token: str
    im_user: str

    @property
    def headers(self) -> dict[str, str]:
        return bearer(self.token)


@dataclass(frozen=True)
class Visitor:
    room_id: uuid.UUID
    user_id: str
    group_id: str
    visitor_token: str


class Desk:
    def __init__(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        fake_im: FakeOpenIM,
        settings: Settings,
        database_urls: DatabaseUrls,
    ) -> None:
        self.app = app
        self.client = client
        self.im = fake_im
        self.settings = settings
        self.database_urls = database_urls
        self.code = ""
        self.tenant_id = uuid.UUID(int=0)
        self.admin_token = ""

    @property
    def ctx(self) -> AppContext:
        ctx: AppContext = self.app.state.ctx
        return ctx

    @property
    def admin(self) -> dict[str, str]:
        return bearer(self.admin_token)

    async def open(self, code: str = "acme") -> "Desk":
        self.code = code
        self.tenant_id = await provision(self.app, code)
        self.admin_token = await login(self.client, code)
        return self

    async def agent(
        self, username: str, *, roles: list[str] | None = None, online: bool = True
    ) -> Agent:
        staff_id = uuid.UUID(await create_staff(self.client, self.admin_token, username, roles))
        token = await login(self.client, self.code, username, STAFF_PASSWORD)
        agent = Agent(staff_id, username, token, imids.staff_user(self.code, staff_id))
        if online:
            # 与工作台一致：先开通 IM（注册用户、与系统用户互为好友，才能收到信令），再上线。
            response = await self.client.post("/api/v1/agent/im-token", headers=agent.headers)
            assert response.status_code == 200, response.text
            await self.set_status(agent, "online")
        return agent

    async def set_status(self, agent: Agent, status: str) -> dict[str, Any]:
        response = await self.client.put(
            "/api/v1/agent/state", headers=agent.headers, json={"status": status}
        )
        assert response.status_code == 200, response.text
        await self.flush()
        body: dict[str, Any] = response.json()
        return body

    async def visitor(self) -> Visitor:
        response = await init(self.client, await channel_key(self.client, self.code))
        assert response.status_code == 200, response.text
        body = response.json()
        return Visitor(
            room_id=uuid.UUID(body["room_id"]),
            user_id=body["im"]["user_id"],
            group_id=body["im"]["group_id"],
            visitor_token=body["visitor_token"],
        )

    async def say(self, visitor: Visitor, text: str) -> None:
        """访客在 Widget 里发一条消息。"""
        self.im.send_as(visitor.user_id, visitor.group_id, text)
        await self.flush()

    async def reply(self, agent: Agent, visitor: Visitor, text: str) -> None:
        """坐席以自己的 IM 身份发一条消息（M4 之后改为经平台 API 发送）。"""
        self.im.send_as(agent.im_user, visitor.group_id, text)
        await self.flush()

    async def flush(self) -> None:
        """投递回调、处理事件、执行到期的 AI 回复，直到没有新的动作（测试里 AI 不等待合并）。"""
        processor = EventProcessor(self.ctx.bus, event_handlers(self.ctx), consumer="test")
        await self.ctx.bus.ensure_groups()
        while True:
            callbacks = list(self.im.callbacks)
            self.im.callbacks.clear()
            await deliver(self.client, self.settings, callbacks)
            processed = await processor.process_available()
            answered = await run_due(self.ctx) if self.ctx.llm.enabled else 0
            if not callbacks and not processed and not answered and not self.im.callbacks:
                return

    # ---- 观察 ----

    def room_texts(self, visitor: Visitor, *, sender: str | None = None) -> list[str]:
        """服务群里的文本消息（可按发送者过滤）。"""
        import json

        return [
            json.loads(m.content)["content"]
            for m in self.im.groups[visitor.group_id].messages
            if m.content_type == 101 and (sender is None or m.send_id == sender)
        ]

    def notices(self, visitor: Visitor) -> list[str]:
        return self.room_texts(visitor, sender=imids.system_user(self.code))

    def members(self, visitor: Visitor) -> set[str]:
        return set(self.im.groups[visitor.group_id].members)

    async def sql(self, query: str, *args: Any) -> list[asyncpg.Record]:
        conn = await asyncpg.connect(self.database_urls.owner_dsn)
        try:
            return await conn.fetch(query, *args)
        finally:
            await conn.close()

    async def session_of(self, visitor: Visitor) -> asyncpg.Record:
        [row] = await self.sql(
            "SELECT * FROM sessions WHERE room_id = $1 ORDER BY created_at DESC LIMIT 1",
            visitor.room_id,
        )
        return row

    async def events_of(self, session_id: uuid.UUID) -> list[str]:
        rows = await self.sql(
            "SELECT type FROM session_events WHERE session_id = $1 ORDER BY created_at, id",
            session_id,
        )
        return [r["type"] for r in rows]
