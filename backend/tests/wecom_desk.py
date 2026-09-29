"""企业微信场景的测试工具：配置了服务商的应用、模拟企业微信、内存对象存储，以及授权等常用步骤。"""

import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from app.core.config import Settings
from tests.desk import Agent, Desk
from tests.fake_openim import FakeOpenIM
from tests.fake_wecom import AES_KEY, SUITE_ID, SUITE_SECRET, TOKEN, FakeWeCom
from tests.support import DatabaseUrls

API = "/api/v1/admin/integrations/wecom"


class FakeStorage:
    """内存对象存储：按路径保存 PUT 的内容。"""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "PUT":
                self.objects[path] = (request.content, request.headers.get("content-type", ""))
                return httpx.Response(200)
            if request.method == "GET" and path in self.objects:
                data, content_type = self.objects[path]
                return httpx.Response(200, content=data, headers={"content-type": content_type})
            return httpx.Response(404)

        return httpx.MockTransport(handler)


def wecom_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "wecom_suite_id": SUITE_ID,
            "wecom_suite_secret": SecretStr(SUITE_SECRET),
            "wecom_token": SecretStr(TOKEN),
            "wecom_encoding_aes_key": SecretStr(AES_KEY),
            "wecom_api_url": "http://wecom",
            "public_api_url": "http://testserver",
            "console_public_url": "http://console",
        }
    )


class WecomDesk(Desk):
    def __init__(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        fake_im: FakeOpenIM,
        settings: Settings,
        database_urls: DatabaseUrls,
        wecom: FakeWeCom,
        storage: FakeStorage,
    ) -> None:
        super().__init__(app, client, fake_im, settings, database_urls)
        self.wecom = wecom
        self.storage = storage

    async def authorize(self) -> None:
        """管理员扫码授权：生成授权链接 → 同意授权 → 浏览器跳回平台 → 全量同步。"""
        assert (await self.wecom.push_suite_ticket()).status_code == 200
        response = await self.client.post(f"{API}/install", headers=self.admin)
        assert response.status_code == 200, response.text
        state = parse_qs(urlsplit(response.json()["url"]).query)["state"][0]
        auth_code = self.wecom.authorize(state)
        response = await self.client.get(
            "/api/v1/wecom/install/callback", params={"auth_code": auth_code, "state": state}
        )
        assert response.status_code == 302, response.text
        assert "installed=1" in response.headers["location"], response.headers["location"]
        await self.flush()

    async def status(self) -> dict[str, Any]:
        response = await self.client.get(API, headers=self.admin)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    async def bind(self, userid: str, agent: Agent | None) -> None:
        response = await self.client.put(
            f"{API}/members/{userid}",
            headers=self.admin,
            json={"staff_id": str(agent.staff_id) if agent else None},
        )
        assert response.status_code == 200, response.text

    async def kf_channel(self) -> dict[str, Any]:
        response = await self.client.get("/api/v1/channels", headers=self.admin)
        [channel] = [c for c in response.json()["items"] if c["type"] == "wecom_kf"]
        return dict(channel)

    async def customer_says(self, text: str, **kwargs: Any) -> None:
        await self.wecom.customer_says(text, **kwargs)
        await self.flush()

    async def kf_room(self, external_userid: str = "wmcust0001") -> uuid.UUID:
        [row] = await self.sql(
            "SELECT r.id FROM rooms r JOIN customer_identities i ON i.id = r.identity_id "
            "WHERE i.external_id = $1",
            external_userid,
        )
        room_id: uuid.UUID = row["id"]
        return room_id

    async def kf_session(self, external_userid: str = "wmcust0001") -> Any:
        room_id = await self.kf_room(external_userid)
        [row] = await self.sql(
            "SELECT * FROM sessions WHERE room_id = $1 ORDER BY created_at DESC LIMIT 1", room_id
        )
        return row
