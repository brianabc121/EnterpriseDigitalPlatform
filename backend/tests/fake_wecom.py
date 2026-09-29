"""模拟的企业微信服务（服务商代开发应用视角）：实现平台用到的服务端接口，并能向平台推送加密回调。

- 单元测试里挂在 httpx.MockTransport 上（transport()），回调经测试客户端投递给平台（platform）。
- 浏览器验收时独立运行：uv run python -m tests.fake_wecom --port 8901 --platform http://127.0.0.1:8000
  除接口外还提供授权安装页、扫码登录页和网页授权跳转，以及 /_fake/* 控制接口（模拟客户发消息、
  员工添加客户、推送 suite_ticket 等）。
- 接口行为按官方文档实现（errcode、字段名、游标分页、48 小时内最多 5 条等），只保留平台用到的部分。
  真实联调需要服务商资质，行为以官方为准。
"""

import argparse
import base64
import hashlib
import itertools
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from app.integrations.wecom.crypto import CallbackCrypto, to_xml

SUITE_ID = "wwsuitefake0001"
SUITE_SECRET = "fake-suite-secret"
TOKEN = "fakeCallbackToken"
# 43 个字符：32 字节 Base64 编码去掉末尾的 "="。
AES_KEY = base64.b64encode(hashlib.sha256(b"edp-fake-wecom").digest()).decode().rstrip("=")
CORP_ID = "wwcorpfake0001"
CORP_NAME = "示例科技有限公司"
AGENT_ID = 1000017
KF_WINDOW_SECONDS = 48 * 3600
KF_WINDOW_LIMIT = 5

ERR_INVALID_TOKEN = 40014
ERR_TOKEN_EXPIRED = 42001
ERR_INVALID_CODE = 40029
ERR_INVALID_AUTH_CODE = 40078
ERR_INVALID_CREDENTIAL = 40001
ERR_KF_LIMIT = 95018
ERR_NOT_FOUND = 60111


def _now() -> int:
    return int(time.time())


@dataclass
class Corp:
    corp_id: str = CORP_ID
    corp_name: str = CORP_NAME
    agent_id: int = AGENT_ID
    permanent_code: str = ""
    admin_userid: str = "admin"
    members: dict[str, dict[str, Any]] = field(default_factory=dict)
    follow_users: set[str] = field(default_factory=set)
    contacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    # (external_userid, userid) → 添加关系
    follows: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    tag_groups: list[dict[str, Any]] = field(default_factory=list)
    group_chats: dict[str, dict[str, Any]] = field(default_factory=dict)
    kf_accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    kf_customers: dict[str, dict[str, Any]] = field(default_factory=dict)
    kf_messages: list[dict[str, Any]] = field(default_factory=list)
    kf_states: dict[tuple[str, str], int] = field(default_factory=dict)
    transfers: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)


class FakeWeCom:
    def __init__(self) -> None:
        self.crypto = CallbackCrypto(TOKEN, AES_KEY)
        self.corp = Corp()
        self.suite_ticket = secrets.token_hex(12)
        self.suite_tokens: set[str] = set()
        self.corp_tokens: dict[str, str] = {}
        self.pre_auth_codes: set[str] = set()
        self.auth_codes: dict[str, str] = {}  # auth_code → state
        self.login_codes: dict[str, str] = {}  # code → userid
        self.media: dict[str, tuple[bytes, str, str]] = {}
        self.uploaded: dict[str, tuple[bytes, str, str]] = {}
        self.sent: list[dict[str, Any]] = []  # kf/send_msg
        self.event_replies: list[dict[str, Any]] = []  # kf/send_msg_on_event
        self.welcomes: list[dict[str, Any]] = []  # externalcontact/send_welcome_msg
        self.app_messages: list[dict[str, Any]] = []  # message/send
        self.marked: list[dict[str, Any]] = []  # externalcontact/mark_tag
        self.calls: list[str] = []
        self.down = False
        self.platform: httpx.AsyncClient | None = None
        self._ids = itertools.count(1)
        self.seed()

    # ---- 示例数据 ----

    def seed(self) -> None:
        corp = self.corp
        corp.members = {
            "admin": {"name": "管理员", "department": [1]},
            "zhangsan": {"name": "张三", "department": [1, 2]},
            "lisi": {"name": "李四", "department": [2]},
        }
        corp.follow_users = {"zhangsan", "lisi"}
        corp.tag_groups = [
            {
                "group_id": "tg1",
                "group_name": "客户等级",
                "tag": [
                    {"id": "tag-vip", "name": "VIP", "order": 1},
                    {"id": "tag-new", "name": "新客户", "order": 2},
                ],
            },
            {
                "group_id": "tg2",
                "group_name": "意向",
                "tag": [{"id": "tag-hot", "name": "高意向", "order": 1}],
            },
        ]
        corp.kf_accounts = {
            "wkfake0001": {"name": "官方客服", "avatar": "https://example.com/kf.png"},
        }

    def next_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids):06d}"

    # ---- 测试辅助：推送回调 ----

    async def deliver(self, kind: str, event: dict[str, Any], receive_id: str) -> httpx.Response:
        assert self.platform is not None, "platform client is not set"
        body, params = self.crypto.seal(to_xml(event), receive_id)
        return await self.platform.post(f"/hooks/wecom/{kind}", params=params, content=body)

    async def push_suite_ticket(self) -> httpx.Response:
        self.suite_ticket = secrets.token_hex(12)
        return await self.deliver(
            "cmd",
            {
                "SuiteId": SUITE_ID,
                "InfoType": "suite_ticket",
                "TimeStamp": _now(),
                "SuiteTicket": self.suite_ticket,
            },
            SUITE_ID,
        )

    def authorize(self, state: str) -> str:
        """管理员在授权页同意授权：生成临时授权码。"""
        auth_code = self.next_id("authcode")
        self.auth_codes[auth_code] = state
        return auth_code

    async def push_create_auth(self, auth_code: str, state: str) -> httpx.Response:
        return await self.deliver(
            "cmd",
            {
                "SuiteId": SUITE_ID,
                "AuthCode": auth_code,
                "InfoType": "create_auth",
                "TimeStamp": _now(),
                "State": state,
            },
            SUITE_ID,
        )

    async def push_cancel_auth(self) -> httpx.Response:
        return await self.deliver(
            "cmd",
            {
                "SuiteId": SUITE_ID,
                "InfoType": "cancel_auth",
                "TimeStamp": _now(),
                "AuthCorpId": self.corp.corp_id,
            },
            SUITE_ID,
        )

    async def _data_event(self, event: dict[str, Any]) -> httpx.Response:
        payload = {
            "ToUserName": self.corp.corp_id,
            "FromUserName": "sys",
            "CreateTime": _now(),
            "MsgType": "event",
            **event,
        }
        return await self.deliver("data", payload, self.corp.corp_id)

    def add_kf_customer(self, external_userid: str, nickname: str, **extra: Any) -> None:
        self.corp.kf_customers[external_userid] = {
            "external_userid": external_userid,
            "nickname": nickname,
            "avatar": f"https://example.com/{external_userid}.png",
            "gender": extra.get("gender", 0),
            "unionid": extra.get("unionid", ""),
        }

    def kf_message(
        self, open_kfid: str, external_userid: str, msgtype: str, body: dict[str, Any], **extra: Any
    ) -> dict[str, Any]:
        message = {
            "msgid": self.next_id("kfmsg"),
            "open_kfid": open_kfid,
            "external_userid": external_userid,
            "send_time": extra.pop("send_time", _now()),
            "origin": extra.pop("origin", 3),
            "msgtype": msgtype,
            msgtype: body,
            **extra,
        }
        self.corp.kf_messages.append(message)
        if message["origin"] == 3:
            self.corp.kf_states.setdefault((open_kfid, external_userid), 0)
        return message

    async def notify_kf(self, open_kfid: str) -> httpx.Response:
        token = self.next_id("kftoken")
        return await self._data_event(
            {"Event": "kf_msg_or_event", "Token": token, "OpenKfId": open_kfid}
        )

    async def customer_says(
        self, text: str, *, external_userid: str = "wmcust0001", open_kfid: str = "wkfake0001"
    ) -> dict[str, Any]:
        """微信客户在客服入口发一条文字消息，并推送 kf_msg_or_event 回调。"""
        message = self.kf_message(open_kfid, external_userid, "text", {"content": text})
        await self.notify_kf(open_kfid)
        return message

    async def customer_enters(
        self,
        *,
        external_userid: str = "wmcust0001",
        open_kfid: str = "wkfake0001",
        scene: str = "website",
    ) -> dict[str, Any]:
        """客户进入会话（enter_session 事件，带 20 秒有效的 welcome_code）。"""
        message = self.kf_message(
            open_kfid,
            external_userid,
            "event",
            {},
            origin=4,
        )
        message.pop("event")
        message["event"] = {
            "event_type": "enter_session",
            "open_kfid": open_kfid,
            "external_userid": external_userid,
            "scene": scene,
            "scene_param": "from-test",
            "welcome_code": self.next_id("welcome"),
        }
        await self.notify_kf(open_kfid)
        return message

    def add_media(self, data: bytes, content_type: str, filename: str) -> str:
        media_id = self.next_id("media")
        self.media[media_id] = (data, content_type, filename)
        return media_id

    def add_contact(
        self,
        external_userid: str,
        name: str,
        *,
        userid: str = "zhangsan",
        remark: str = "",
        tags: list[str] | None = None,
        unionid: str = "",
    ) -> None:
        self.corp.contacts[external_userid] = {
            "external_userid": external_userid,
            "name": name,
            "avatar": f"https://example.com/{external_userid}.png",
            "type": 1,
            "gender": 1,
            "unionid": unionid,
        }
        self.corp.follows[(external_userid, userid)] = {
            "userid": userid,
            "remark": remark,
            "description": "",
            "createtime": _now() - len(self.corp.follows),
            "tag_id": list(tags or []),
            "add_way": 1,
            "state": "",
        }

    async def staff_adds_contact(
        self, external_userid: str, name: str, *, userid: str = "zhangsan", **kwargs: Any
    ) -> str:
        """员工添加了新客户：推送 add_external_contact（带欢迎语 code）。返回 WelcomeCode。"""
        self.add_contact(external_userid, name, userid=userid, **kwargs)
        welcome_code = self.next_id("wc")
        await self._data_event(
            {
                "Event": "change_external_contact",
                "ChangeType": "add_external_contact",
                "UserID": userid,
                "ExternalUserID": external_userid,
                "State": "",
                "WelcomeCode": welcome_code,
            }
        )
        return welcome_code

    async def contact_changed(
        self,
        external_userid: str,
        *,
        userid: str = "zhangsan",
        change: str = "edit_external_contact",
    ) -> httpx.Response:
        return await self._data_event(
            {
                "Event": "change_external_contact",
                "ChangeType": change,
                "UserID": userid,
                "ExternalUserID": external_userid,
            }
        )

    def add_group(
        self, chat_id: str, name: str, *, owner: str = "zhangsan", externals: list[str] = ()
    ) -> None:
        members = [
            {"userid": owner, "type": 1, "join_time": _now(), "join_scene": 1},
        ] + [
            {"userid": ext, "type": 2, "join_time": _now(), "join_scene": 3, "name": ext}
            for ext in externals
        ]
        self.corp.group_chats[chat_id] = {
            "chat_id": chat_id,
            "name": name,
            "owner": owner,
            "create_time": _now(),
            "notice": "",
            "member_list": members,
        }

    async def group_changed(self, chat_id: str, change: str = "update") -> httpx.Response:
        return await self._data_event(
            {
                "Event": "change_external_chat",
                "ChatId": chat_id,
                "ChangeType": change,
                "UpdateDetail": "add_member",
            }
        )

    def login_code(self, userid: str) -> str:
        code = self.next_id("logincode")
        self.login_codes[code] = userid
        return code

    def expire_tokens(self) -> None:
        self.suite_tokens.clear()
        self.corp_tokens.clear()

    def sent_texts(self, external_userid: str = "wmcust0001") -> list[str]:
        return [
            m["text"]["content"]
            for m in self.sent
            if m["touser"] == external_userid and m["msgtype"] == "text"
        ]

    # ---- 路由 ----

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            status, headers, content = self.handle(
                request.method,
                request.url.path,
                dict(request.url.params),
                request.content,
                request.headers.get("content-type", ""),
            )
            return httpx.Response(status, headers=headers, content=content)

        return httpx.MockTransport(handler)

    def handle(
        self, method: str, path: str, params: dict[str, str], raw: bytes, content_type: str
    ) -> tuple[int, dict[str, str], bytes]:
        self.calls.append(path)
        if self.down:
            return 502, {"content-type": "text/plain"}, b"bad gateway"
        if path == "/cgi-bin/media/upload":
            return self._json(self._media_upload(params, raw, content_type))
        if path == "/cgi-bin/media/get":
            return self._media_get(params)
        body: dict[str, Any] = {}
        if raw and "json" in content_type:
            body = json.loads(raw)
        return self._json(self._api(method, path, params, body))

    @staticmethod
    def _json(payload: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        return (
            200,
            {"content-type": "application/json"},
            json.dumps(payload, ensure_ascii=False).encode(),
        )

    def _api(
        self, method: str, path: str, params: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        provider = {
            "/cgi-bin/service/get_suite_token": self._get_suite_token,
            "/cgi-bin/service/get_pre_auth_code": self._get_pre_auth_code,
            "/cgi-bin/service/get_permanent_code": self._get_permanent_code,
            "/cgi-bin/service/get_auth_info": self._get_auth_info,
        }
        if path == "/cgi-bin/service/get_suite_token":
            return self._get_suite_token(params, body)
        if path in provider:
            if params.get("suite_access_token") not in self.suite_tokens:
                return _error(ERR_INVALID_TOKEN, "invalid suite_access_token")
            return provider[path](params, body)
        if path == "/cgi-bin/gettoken":
            return self._gettoken(params, body)
        handler = {
            "/cgi-bin/get_jsapi_ticket": self._jsapi_ticket,
            "/cgi-bin/ticket/get": self._agent_ticket,
            "/cgi-bin/kf/account/list": self._kf_account_list,
            "/cgi-bin/kf/add_contact_way": self._kf_add_contact_way,
            "/cgi-bin/kf/sync_msg": self._kf_sync_msg,
            "/cgi-bin/kf/send_msg": self._kf_send_msg,
            "/cgi-bin/kf/send_msg_on_event": self._kf_send_msg_on_event,
            "/cgi-bin/kf/customer/batchget": self._kf_customer_batchget,
            "/cgi-bin/kf/service_state/get": self._kf_state_get,
            "/cgi-bin/kf/service_state/trans": self._kf_state_trans,
            "/cgi-bin/user/list_id": self._user_list_id,
            "/cgi-bin/user/get": self._user_get,
            "/cgi-bin/auth/getuserinfo": self._auth_getuserinfo,
            "/cgi-bin/externalcontact/get_follow_user_list": self._follow_user_list,
            "/cgi-bin/externalcontact/batch/get_by_user": self._batch_get_by_user,
            "/cgi-bin/externalcontact/get": self._contact_get,
            "/cgi-bin/externalcontact/get_corp_tag_list": self._corp_tag_list,
            "/cgi-bin/externalcontact/mark_tag": self._mark_tag,
            "/cgi-bin/externalcontact/send_welcome_msg": self._send_welcome_msg,
            "/cgi-bin/externalcontact/groupchat/list": self._groupchat_list,
            "/cgi-bin/externalcontact/groupchat/get": self._groupchat_get,
            "/cgi-bin/externalcontact/transfer_customer": self._transfer_customer,
            "/cgi-bin/externalcontact/transfer_result": self._transfer_result,
            "/cgi-bin/message/send": self._message_send,
        }.get(path)
        if handler is None:
            return _error(404, f"unknown api {path}")
        token = params.get("access_token", "")
        if token not in self.corp_tokens:
            return _error(ERR_TOKEN_EXPIRED if token else ERR_INVALID_TOKEN, "invalid access_token")
        return handler(params, body)

    # ---- 服务商 ----

    def _get_suite_token(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        if (
            body.get("suite_id") != SUITE_ID
            or body.get("suite_secret") != SUITE_SECRET
            or body.get("suite_ticket") != self.suite_ticket
        ):
            return _error(ERR_INVALID_CREDENTIAL, "invalid suite credential")
        token = secrets.token_hex(16)
        self.suite_tokens.add(token)
        return _ok({"suite_access_token": token, "expires_in": 7200})

    def _get_pre_auth_code(self, _: dict[str, str], __: dict[str, Any]) -> dict[str, Any]:
        code = self.next_id("preauth")
        self.pre_auth_codes.add(code)
        return _ok({"pre_auth_code": code, "expires_in": 1200})

    def _auth_payload(self) -> dict[str, Any]:
        corp = self.corp
        return {
            "auth_corp_info": {
                "corpid": corp.corp_id,
                "corp_name": corp.corp_name,
                "corp_type": "verified",
                "corp_user_max": 200,
                "subject_type": 1,
            },
            "auth_info": {
                "agent": [
                    {
                        "agentid": corp.agent_id,
                        "name": "智能客服平台",
                        "privilege": {"level": 1, "allow_user": ["zhangsan", "lisi"]},
                    }
                ]
            },
        }

    def _get_permanent_code(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        auth_code = str(body.get("auth_code") or "")
        if auth_code not in self.auth_codes:
            return _error(ERR_INVALID_AUTH_CODE, "invalid auth_code")
        del self.auth_codes[auth_code]
        self.corp.permanent_code = secrets.token_hex(16)
        return _ok(
            {
                "access_token": secrets.token_hex(8),
                "expires_in": 7200,
                "permanent_code": self.corp.permanent_code,
                **self._auth_payload(),
                "auth_user_info": {"userid": self.corp.admin_userid, "name": "管理员"},
            }
        )

    def _get_auth_info(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        if body.get("permanent_code") != self.corp.permanent_code:
            return _error(40084, "invalid permanent_code")
        return _ok(self._auth_payload())

    def _gettoken(self, params: dict[str, str], _: dict[str, Any]) -> dict[str, Any]:
        corp = self.corp
        if params.get("corpid") != corp.corp_id or (
            not corp.permanent_code or params.get("corpsecret") != corp.permanent_code
        ):
            return _error(ERR_INVALID_CREDENTIAL, "invalid credential")
        token = secrets.token_hex(16)
        self.corp_tokens[token] = corp.corp_id
        return _ok({"access_token": token, "expires_in": 7200})

    def _jsapi_ticket(self, _: dict[str, str], __: dict[str, Any]) -> dict[str, Any]:
        return _ok({"ticket": "fake-jsapi-ticket", "expires_in": 7200})

    def _agent_ticket(self, params: dict[str, str], _: dict[str, Any]) -> dict[str, Any]:
        if params.get("type") != "agent_config":
            return _error(40001, "invalid type")
        return _ok({"ticket": "fake-agent-ticket", "expires_in": 7200})

    # ---- 微信客服 ----

    def _kf_account_list(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        items = [
            {"open_kfid": k, "name": v["name"], "avatar": v["avatar"], "manage_privilege": True}
            for k, v in self.corp.kf_accounts.items()
        ]
        offset, limit = int(body.get("offset") or 0), int(body.get("limit") or 100)
        return _ok({"account_list": items[offset : offset + limit]})

    def _kf_add_contact_way(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        open_kfid = str(body.get("open_kfid") or "")
        if open_kfid not in self.corp.kf_accounts:
            return _error(95004, "open_kfid not exist")
        return _ok({"url": f"https://work.weixin.qq.com/kfid/{open_kfid}?enc_scene=edp"})

    def _kf_sync_msg(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        cursor = int(body.get("cursor") or 0)
        limit = int(body.get("limit") or 1000)
        open_kfid = body.get("open_kfid")
        remaining = [
            (i, m)
            for i, m in enumerate(self.corp.kf_messages)
            if i >= cursor and (not open_kfid or m["open_kfid"] == open_kfid)
        ]
        page = remaining[:limit]
        next_cursor = str(page[-1][0] + 1) if page else str(cursor)
        return _ok(
            {
                "next_cursor": next_cursor,
                "has_more": 1 if len(remaining) > limit else 0,
                "msg_list": [json.loads(json.dumps(m)) for _, m in page],
            }
        )

    def _window_used(self, open_kfid: str, external_userid: str) -> tuple[int | None, int]:
        last = max(
            (
                m["send_time"]
                for m in self.corp.kf_messages
                if m["origin"] == 3
                and m["open_kfid"] == open_kfid
                and m["external_userid"] == external_userid
            ),
            default=None,
        )
        used = sum(
            1
            for m in self.sent
            if m["open_kfid"] == open_kfid
            and m["touser"] == external_userid
            and last is not None
            and m["_time"] >= last
        )
        return last, used

    def _kf_send_msg(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        open_kfid = str(body.get("open_kfid") or "")
        touser = str(body.get("touser") or "")
        if open_kfid not in self.corp.kf_accounts:
            return _error(95004, "open_kfid not exist")
        last, used = self._window_used(open_kfid, touser)
        if last is None or _now() - last > KF_WINDOW_SECONDS:
            return _error(95018, "send msg out of 48 hours")
        if used >= KF_WINDOW_LIMIT:
            return _error(ERR_KF_LIMIT, "send msg count limit exceed")
        msgtype = body.get("msgtype")
        if msgtype in ("image", "file") and body[msgtype].get("media_id") not in self.uploaded:
            return _error(40007, "invalid media_id")
        msgid = str(body.get("msgid") or self.next_id("sent"))
        self.sent.append({**body, "msgid": msgid, "_time": _now()})
        return _ok({"msgid": msgid})

    def _kf_send_msg_on_event(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        code = str(body.get("code") or "")
        valid = any(
            (m.get("event") or {}).get("welcome_code") == code for m in self.corp.kf_messages
        )
        if not valid or any(r["code"] == code for r in self.event_replies):
            return _error(95007, "invalid msg code")
        msgid = str(body.get("msgid") or self.next_id("evt"))
        self.event_replies.append({**body, "msgid": msgid})
        return _ok({"msgid": msgid})

    def _kf_customer_batchget(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        ids = body.get("external_userid_list") or []
        found = [self.corp.kf_customers[i] for i in ids if i in self.corp.kf_customers]
        invalid = [i for i in ids if i not in self.corp.kf_customers]
        return _ok({"customer_list": found, "invalid_external_userid": invalid})

    def _kf_state_get(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        key = (str(body.get("open_kfid")), str(body.get("external_userid")))
        return _ok({"service_state": self.corp.kf_states.get(key, 0), "servicer_userid": ""})

    def _kf_state_trans(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        key = (str(body.get("open_kfid")), str(body.get("external_userid")))
        current = self.corp.kf_states.get(key, 0)
        target = int(body.get("service_state") or 0)
        if current == target or (current == 3 and target == 1):
            return _error(95016, "invalid state transition")
        self.corp.kf_states[key] = target
        return _ok({"msg_code": ""})

    # ---- 素材 ----

    def _media_get(self, params: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        if params.get("access_token") not in self.corp_tokens:
            return self._json(_error(ERR_TOKEN_EXPIRED, "invalid access_token"))
        media = self.media.get(params.get("media_id", ""))
        if media is None:
            return self._json(_error(40007, "invalid media_id"))
        data, content_type, filename = media
        return (
            200,
            {
                "content-type": content_type,
                "content-disposition": f'attachment; filename="{filename}"',
            },
            data,
        )

    def _media_upload(
        self, params: dict[str, str], raw: bytes, content_type: str
    ) -> dict[str, Any]:
        if params.get("access_token") not in self.corp_tokens:
            return _error(ERR_TOKEN_EXPIRED, "invalid access_token")
        if "multipart/form-data" not in content_type:
            return _error(40005, "invalid file type")
        media_id = self.next_id("up")
        self.uploaded[media_id] = (raw, params.get("type", "file"), "")
        return _ok({"type": params.get("type"), "media_id": media_id, "created_at": str(_now())})

    # ---- 通讯录与登录 ----

    def _user_list_id(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        entries = [
            {"userid": u, "department": d}
            for u, info in self.corp.members.items()
            for d in info["department"]
        ]
        return _ok({"next_cursor": "", "dept_user": entries})

    def _user_get(self, params: dict[str, str], _: dict[str, Any]) -> dict[str, Any]:
        member = self.corp.members.get(params.get("userid", ""))
        if member is None:
            return _error(ERR_NOT_FOUND, "invalid userid")
        return _ok({"userid": params["userid"], "name": member["name"]})

    def _auth_getuserinfo(self, params: dict[str, str], _: dict[str, Any]) -> dict[str, Any]:
        userid = self.login_codes.pop(params.get("code", ""), None)
        if userid is None:
            return _error(ERR_INVALID_CODE, "invalid code")
        if userid not in self.corp.members:
            return _ok({"openid": f"o-{userid}"})
        return _ok({"userid": userid, "user_ticket": secrets.token_hex(8)})

    # ---- 客户联系 ----

    def _follow_user_list(self, _: dict[str, str], __: dict[str, Any]) -> dict[str, Any]:
        return _ok({"follow_user": sorted(self.corp.follow_users)})

    def _batch_get_by_user(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        userids = set(body.get("userid_list") or [])
        entries = [
            {"external_contact": self.corp.contacts[ext], "follow_info": info}
            for (ext, userid), info in sorted(self.corp.follows.items())
            if userid in userids and ext in self.corp.contacts
        ]
        cursor = int(body.get("cursor") or 0)
        limit = int(body.get("limit") or 50)
        page = entries[cursor : cursor + limit]
        next_cursor = str(cursor + limit) if cursor + limit < len(entries) else ""
        return _ok({"external_contact_list": page, "next_cursor": next_cursor})

    def _tag_objects(self, tag_ids: list[str]) -> list[dict[str, Any]]:
        result = []
        for group in self.corp.tag_groups:
            for tag in group["tag"]:
                if tag["id"] in tag_ids:
                    result.append(
                        {
                            "group_name": group["group_name"],
                            "tag_name": tag["name"],
                            "tag_id": tag["id"],
                            "type": 1,
                        }
                    )
        return result

    def _contact_get(self, params: dict[str, str], _: dict[str, Any]) -> dict[str, Any]:
        ext = params.get("external_userid", "")
        contact = self.corp.contacts.get(ext)
        if contact is None:
            return _error(84061, "not external contact")
        follows = [
            {
                **{k: v for k, v in info.items() if k != "tag_id"},
                "tags": self._tag_objects(info["tag_id"]),
            }
            for (e, _userid), info in sorted(self.corp.follows.items())
            if e == ext
        ]
        return _ok({"external_contact": contact, "follow_user": follows})

    def _corp_tag_list(self, _: dict[str, str], __: dict[str, Any]) -> dict[str, Any]:
        return _ok({"tag_group": self.corp.tag_groups})

    def _mark_tag(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        key = (str(body.get("external_userid")), str(body.get("userid")))
        follow = self.corp.follows.get(key)
        if follow is None:
            return _error(84061, "not external contact")
        tags = set(follow["tag_id"]) | set(body.get("add_tag") or [])
        follow["tag_id"] = sorted(tags - set(body.get("remove_tag") or []))
        self.marked.append(body)
        return _ok({})

    def _send_welcome_msg(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        self.welcomes.append(body)
        return _ok({})

    def _groupchat_list(self, _: dict[str, str], __: dict[str, Any]) -> dict[str, Any]:
        return _ok(
            {
                "group_chat_list": [
                    {"chat_id": c, "status": 0} for c in sorted(self.corp.group_chats)
                ],
                "next_cursor": "",
            }
        )

    def _groupchat_get(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        chat = self.corp.group_chats.get(str(body.get("chat_id") or ""))
        if chat is None:
            return _error(701008, "chat not exist")
        return _ok({"group_chat": chat})

    def _transfer_customer(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        handover, takeover = str(body.get("handover_userid")), str(body.get("takeover_userid"))
        results = []
        for ext in body.get("external_userid") or []:
            if (ext, handover) not in self.corp.follows:
                results.append({"external_userid": ext, "errcode": 40130})
                continue
            done = sum(1 for (e, _h, _t) in self.corp.transfers if e == ext)
            if done >= 2:
                results.append({"external_userid": ext, "errcode": 40129})
                continue
            self.corp.transfers[(ext, handover, takeover)] = {"status": 2, "takeover_time": 0}
            results.append({"external_userid": ext, "errcode": 0})
        return _ok({"customer": results})

    def _transfer_result(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        handover, takeover = str(body.get("handover_userid")), str(body.get("takeover_userid"))
        return _ok(
            {
                "customer": [
                    {"external_userid": ext, **result}
                    for (ext, h, t), result in self.corp.transfers.items()
                    if h == handover and t == takeover
                ],
                "next_cursor": "",
            }
        )

    def complete_transfers(self) -> None:
        """模拟 24 小时后自动接替：等待中的转接变为接替完毕，添加人随之变更。"""
        for (ext, handover, takeover), result in self.corp.transfers.items():
            if result["status"] != 2:
                continue
            result.update(status=1, takeover_time=_now())
            info = self.corp.follows.pop((ext, handover), None)
            if info is not None:
                self.corp.follows[(ext, takeover)] = {**info, "userid": takeover}

    def _message_send(self, _: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        if int(body.get("agentid") or 0) != self.corp.agent_id:
            return _error(40056, "invalid agentid")
        self.app_messages.append(body)
        return _ok({"invaliduser": "", "msgid": self.next_id("app")})

    # ---- 独立运行 ----

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
            headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
            params = {k: v[0] for k, v in parse_qs(scope.get("query_string", b"").decode()).items()}
            path = scope["path"]
            if path.startswith("/_fake/") or path in _PAGES:
                status, out_headers, content = await fake.page(path, params, raw)
            else:
                status, out_headers, content = fake.handle(
                    scope["method"], path, params, raw, headers.get("content-type", "")
                )
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(k.encode(), v.encode()) for k, v in out_headers.items()],
                }
            )
            await send({"type": "http.response.body", "body": content})

        return app

    async def page(
        self, path: str, params: dict[str, str], raw: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """浏览器页面（授权、扫码登录、网页授权）和控制接口。"""
        body: dict[str, Any] = json.loads(raw) if raw else {}
        match path:
            case "/3rdapp/install":
                confirm = "/3rdapp/install/confirm?" + urlencode(params)
                return _html(
                    f"<h1>企业微信 · 授权应用</h1><p>{self.corp.corp_name} 授权"
                    f"「智能客服平台」代开发应用</p>"
                    f'<a id="authorize" href="{confirm}">同意授权</a>'
                )
            case "/3rdapp/install/confirm":
                if params.get("pre_auth_code") not in self.pre_auth_codes:
                    return _html("<h1>授权链接已失效</h1>")
                state = params.get("state", "")
                auth_code = self.authorize(state)
                await self.push_create_auth(auth_code, state)
                target = f"{params['redirect_uri']}?" + urlencode(
                    {"auth_code": auth_code, "state": state, "expires_in": 1200}
                )
                return 302, {"location": target}, b""
            case "/wwlogin/sso/login" | "/connect/oauth2/authorize":
                links = "".join(
                    f'<li><a data-userid="{u}" href="/_fake/login?'
                    + urlencode(
                        {
                            "userid": u,
                            "redirect_uri": params.get("redirect_uri", ""),
                            "state": params.get("state", ""),
                        }
                    )
                    + f'">{info["name"]}（{u}）</a></li>'
                    for u, info in self.corp.members.items()
                )
                return _html(f"<h1>企业微信 · 扫码登录</h1><ul>{links}</ul>")
            case "/_fake/login":
                code = self.login_code(params["userid"])
                redirect = params["redirect_uri"]
                sep = "&" if urlsplit(redirect).query else "?"
                target = f"{redirect}{sep}" + urlencode(
                    {"code": code, "state": params.get("state", "")}
                )
                return 302, {"location": target}, b""
            case "/_fake/reset":
                # 验收脚本每次用新租户：先让平台解除上一次的绑定，再清空模拟数据。
                await self.push_cancel_auth()
                platform = self.platform
                self.__init__()  # type: ignore[misc]
                self.platform = platform
                return self._json({})
            case "/_fake/add_contact":
                self.add_contact(
                    body["external_userid"],
                    body["name"],
                    userid=body.get("userid", "zhangsan"),
                    tags=body.get("tags"),
                    remark=body.get("remark", ""),
                )
                return self._json({})
            case "/_fake/suite_ticket":
                response = await self.push_suite_ticket()
                return self._json({"status": response.status_code})
            case "/_fake/customer_says":
                self.add_kf_customer(
                    body.get("external_userid", "wmcust0001"), body.get("nickname", "微信用户")
                )
                message = await self.customer_says(
                    body["text"], external_userid=body.get("external_userid", "wmcust0001")
                )
                return self._json({"msgid": message["msgid"]})
            case "/_fake/customer_enters":
                self.add_kf_customer(
                    body.get("external_userid", "wmcust0001"), body.get("nickname", "微信用户")
                )
                await self.customer_enters(
                    external_userid=body.get("external_userid", "wmcust0001")
                )
                return self._json({})
            case "/_fake/staff_adds_contact":
                code = await self.staff_adds_contact(
                    body["external_userid"],
                    body["name"],
                    userid=body.get("userid", "zhangsan"),
                    tags=body.get("tags"),
                    remark=body.get("remark", ""),
                )
                return self._json({"welcome_code": code})
            case "/_fake/add_group":
                self.add_group(
                    body["chat_id"],
                    body["name"],
                    owner=body.get("owner", "zhangsan"),
                    externals=body.get("externals", []),
                )
                await self.group_changed(body["chat_id"], "create")
                return self._json({})
            case "/_fake/complete_transfers":
                self.complete_transfers()
                return self._json({})
            case "/_fake/state":
                return self._json(
                    {
                        "sent": [{k: v for k, v in m.items() if k != "_time"} for m in self.sent],
                        "event_replies": self.event_replies,
                        "welcomes": self.welcomes,
                        "app_messages": self.app_messages,
                        "marked": self.marked,
                        "follows": [
                            {"external_userid": e, **info}
                            for (e, _u), info in self.corp.follows.items()
                        ],
                        "transfers": [
                            {"external_userid": e, "handover": h, "takeover": t, **r}
                            for (e, h, t), r in self.corp.transfers.items()
                        ],
                    }
                )
        return 404, {"content-type": "text/plain"}, b"not found"


_PAGES = {
    "/3rdapp/install",
    "/3rdapp/install/confirm",
    "/wwlogin/sso/login",
    "/connect/oauth2/authorize",
}


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"errcode": 0, "errmsg": "ok", **data}


def _error(errcode: int, errmsg: str) -> dict[str, Any]:
    return {"errcode": errcode, "errmsg": errmsg}


def _html(body: str) -> tuple[int, dict[str, str], bytes]:
    page = f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><body>{body}</body></html>'
    return 200, {"content-type": "text/html; charset=utf-8"}, page.encode()


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="模拟的企业微信服务")
    parser.add_argument("--port", type=int, default=8901)
    parser.add_argument("--platform", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    fake = FakeWeCom()
    fake.platform = httpx.AsyncClient(base_url=args.platform, timeout=30)
    uvicorn.run(fake.asgi(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
