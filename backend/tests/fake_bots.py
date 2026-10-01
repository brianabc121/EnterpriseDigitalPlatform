"""模拟的 IM 平台（Telegram、飞书、钉钉、WhatsApp、企业微信机器人的回复地址）：实现 AI 助理
用到的服务端接口，记录发出的消息；单元测试里挂在 httpx.MockTransport 上。"""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

TELEGRAM_USERNAME = "edp_assistant_bot"


@dataclass
class Sent:
    provider: str
    target: str
    text: str
    url: str


@dataclass
class FakeBots:
    sent: list[Sent] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    webhooks: list[dict[str, Any]] = field(default_factory=list)
    # 让某个平台的发送接口失败（errcode / 4xx）。
    failing: set[str] = field(default_factory=set)

    def texts(self, provider: str | None = None, target: str | None = None) -> list[str]:
        return [
            s.text
            for s in self.sent
            if (provider is None or s.provider == provider)
            and (target is None or s.target == target)
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        try:
            body = json.loads(request.content or b"{}")
        except json.JSONDecodeError:
            body = {}
        if host == "api.telegram.org":
            return self._telegram(path, body)
        if host == "open.feishu.cn":
            return self._feishu(request, path, body)
        if host == "api.dingtalk.com":
            return self._dingtalk(path, body)
        if host == "oapi.dingtalk.com":
            if "dingtalk" in self.failing:
                return httpx.Response(200, json={"errcode": 310000, "errmsg": "sign not match"})
            self.sent.append(Sent("dingtalk", "session", body["text"]["content"], str(request.url)))
            return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        if host == "graph.facebook.com":
            if "whatsapp" in self.failing:
                return httpx.Response(400, json={"error": {"message": "window closed"}})
            self.sent.append(Sent("whatsapp", body["to"], body["text"]["body"], str(request.url)))
            return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})
        if host == "qyapi.weixin.qq.com":
            self.sent.append(
                Sent("wecom", "response_url", body["text"]["content"], str(request.url))
            )
            return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        return httpx.Response(404, json={"error": "unknown host"})

    def _telegram(self, path: str, body: dict[str, Any]) -> httpx.Response:
        if path.endswith("/getMe"):
            if "/botbad:" in path:
                return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
            return httpx.Response(
                200, json={"ok": True, "result": {"username": TELEGRAM_USERNAME, "is_bot": True}}
            )
        if path.endswith("/setWebhook"):
            self.webhooks.append(body)
            return httpx.Response(200, json={"ok": True, "result": True})
        if path.endswith("/sendMessage"):
            if "telegram" in self.failing:
                return httpx.Response(
                    403, json={"ok": False, "description": "Forbidden: bot was blocked"}
                )
            self.sent.append(Sent("telegram", str(body["chat_id"]), body["text"], path))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        return httpx.Response(404, json={"ok": False, "description": "not found"})

    def _feishu(self, request: httpx.Request, path: str, body: dict[str, Any]) -> httpx.Response:
        if path.endswith("/tenant_access_token/internal"):
            if body.get("app_secret") == "bad":
                return httpx.Response(200, json={"code": 10003, "msg": "invalid app_secret"})
            return httpx.Response(
                200,
                json={"code": 0, "msg": "ok", "tenant_access_token": "t-feishu", "expire": 7200},
            )
        if path.endswith("/im/v1/messages"):
            if "feishu" in self.failing:
                return httpx.Response(200, json={"code": 230001, "msg": "bot not in chat"})
            text = json.loads(body["content"])["text"]
            kind = request.url.params.get("receive_id_type", "")
            self.sent.append(Sent("feishu", f"{kind}:{body['receive_id']}", text, path))
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {}})
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    def _dingtalk(self, path: str, body: dict[str, Any]) -> httpx.Response:
        if path.endswith("/oauth2/accessToken"):
            return httpx.Response(200, json={"accessToken": "t-dingtalk", "expireIn": 7200})
        if path.endswith("/oToMessages/batchSend") or path.endswith("/groupMessages/send"):
            text = json.loads(body["msgParam"])["content"]
            target = body.get("userIds", [body.get("openConversationId")])[0]
            self.sent.append(Sent("dingtalk", str(target), text, path))
            return httpx.Response(200, json={"processQueryKey": "k"})
        return httpx.Response(404, json={"code": "notFound", "message": "not found"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)
