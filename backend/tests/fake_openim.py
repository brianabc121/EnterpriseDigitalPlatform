"""内存版 OpenIM：实现平台用到的 REST 接口，行为与 v3.8.3 实测一致（见实施计划 §7.1）。

- 挂在 httpx.MockTransport 上使用，不需要真实服务。
- 建群会产生一条群通知并占用 seq；在线信令不占 seq；回调里的 seq 为 0。
- 发送消息后把 afterSendGroupMsg 回调放进 callbacks，测试决定是否投递给平台
  （不投递就相当于回调丢失）。
- 单聊只用于在线信令：发送方必须是接收方的好友（管理员除外），信令记录在 signals 里。
- 踢人时与实测一致：成员被移除，但返回 1001 "maxSeq is invalid"（kick_quirk 可关闭）。
- tests/test_openim_contract.py 用同一组断言分别验证它和真实 OpenIM，保证两者一致。
"""

import base64
import itertools
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

ADMIN_USER_ID = "imAdmin"
SECRET = "fake-openim-secret"
GROUP_CREATED_NOTIFICATION = 1501
MEMBER_KICKED_NOTIFICATION = 1508
MEMBER_INVITED_NOTIFICATION = 1509
# 实测：用户 ID 只能包含字母、数字和下划线。
_USER_ID = re.compile(r"[A-Za-z0-9_]+")


@dataclass
class _Message:
    server_msg_id: str
    client_msg_id: str
    send_id: str
    group_id: str
    content_type: int
    content: str
    seq: int
    send_time: int
    ex: str
    msg_from: int
    sender_nickname: str
    sender_platform_id: int


@dataclass
class _Group:
    group_id: str
    name: str
    owner: str
    members: set[str]
    ex: str
    messages: list[_Message] = field(default_factory=list)


class FakeOpenIM:
    def __init__(self) -> None:
        self.users: dict[str, str] = {}
        self.groups: dict[str, _Group] = {}
        self.tokens: dict[str, str] = {}
        self.callbacks: list[dict[str, Any]] = []
        self.friends: set[frozenset[str]] = set()
        self.signals: list[dict[str, Any]] = []
        self.kick_quirk = True
        self.down = False
        self.calls: list[str] = []
        self._counter = itertools.count(1)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # ---- 测试辅助 ----

    def expire_tokens(self) -> None:
        self.tokens.clear()

    def seqs(self, group_id: str) -> list[int]:
        return [m.seq for m in self.groups[group_id].messages]

    def signals_to(self, user_id: str) -> list[dict[str, Any]]:
        """发给某个用户的在线信令（解析后的 data）。"""
        return [json.loads(s["content"]["data"]) for s in self.signals if s["recv_id"] == user_id]

    def send_as(self, send_id: str, group_id: str, text: str, *, msg_from: int = 100) -> _Message:
        """模拟某个用户通过 SDK 发群消息（msgFrom=100）。"""
        return self._store(
            send_id=send_id,
            group_id=group_id,
            content_type=101,
            content=json.dumps({"content": text}, ensure_ascii=False),
            ex="",
            msg_from=msg_from,
            nickname="",
        )

    # ---- 路由 ----

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if self.down:
            return httpx.Response(502, text="bad gateway")
        body = json.loads(request.content or b"{}")
        if request.url.path == "/auth/get_admin_token":
            return self._get_admin_token(body)
        caller = self.tokens.get(request.headers.get("token", ""))
        if caller is None:
            return _error(1501, "TokenExpiredError")
        handler = {
            "/user/account_check": self._account_check,
            "/user/user_register": self._user_register,
            "/auth/get_user_token": self._get_user_token,
            "/group/create_group": self._create_group,
            "/group/get_group_members_info": self._members_info,
            "/group/invite_user_to_group": self._invite,
            "/group/kick_group": self._kick,
            "/group/dismiss_group": self._dismiss,
            "/friend/import_friend": self._import_friend,
            "/msg/send_msg": self._send_msg,
            "/msg/get_conversations_has_read_and_max_seq": self._max_seq,
            "/msg/pull_msg_by_seq": self._pull,
        }.get(request.url.path)
        if handler is None:
            return httpx.Response(404, text="404 page not found")
        if caller != ADMIN_USER_ID and request.url.path != "/group/create_group":
            return _error(1002, "NoPermissionError")
        return handler(body, caller)

    def _get_admin_token(self, body: dict[str, Any]) -> httpx.Response:
        if body.get("secret") != SECRET or body.get("userID") != ADMIN_USER_ID:
            return _error(1002, "NoPermissionError")
        token = secrets.token_hex(8)
        self.tokens[token] = ADMIN_USER_ID
        return _ok({"token": token, "expireTimeSeconds": 7776000})

    def _account_check(self, body: dict[str, Any], _: str) -> httpx.Response:
        results = [
            {"userID": uid, "accountStatus": 1 if uid in self.users else 0}
            for uid in body["checkUserIDs"]
        ]
        return _ok({"results": results})

    def _user_register(self, body: dict[str, Any], _: str) -> httpx.Response:
        users = body["users"]
        if any(not _USER_ID.fullmatch(u["userID"]) for u in users):
            return _error(1001, "ArgsError")
        if any(u["userID"] in self.users for u in users):
            return _error(1102, "RegisteredAlreadyError", "userID registered already")
        for u in users:
            self.users[u["userID"]] = u.get("nickname", "")
        return _ok(None)

    def _get_user_token(self, body: dict[str, Any], _: str) -> httpx.Response:
        if body["userID"] not in self.users:
            return _error(1004, "RecordNotFoundError", "record not found")
        token = secrets.token_hex(8)
        self.tokens[token] = body["userID"]
        return _ok({"token": token, "expireTimeSeconds": 7776000})

    def _create_group(self, body: dict[str, Any], caller: str) -> httpx.Response:
        owner = body["ownerUserID"]
        if caller not in (ADMIN_USER_ID, owner):
            return _error(1002, "NoPermissionError", f"ownerUserID, {owner}=MISSING")
        info = body["groupInfo"]
        group_id = info.get("groupID") or uuid.uuid4().hex
        if group_id in self.groups:
            return _error(1202, "GroupIDExisted", f"group id existed {group_id}")
        members = {owner, *body.get("memberUserIDs", [])}
        if any(m not in self.users for m in members):
            return _error(1101, "UserIDNotFoundError", "user not found")
        self.groups[group_id] = _Group(group_id, info["groupName"], owner, members, info["ex"])
        self._store(
            send_id=ADMIN_USER_ID,
            group_id=group_id,
            content_type=GROUP_CREATED_NOTIFICATION,
            content=json.dumps({"detail": json.dumps({"group": {"groupID": group_id}})}),
            ex="",
            msg_from=200,
            nickname="",
            callback=False,
        )
        return _ok({"groupInfo": {"groupID": group_id, "ownerUserID": owner}})

    def _members_info(self, body: dict[str, Any], _: str) -> httpx.Response:
        group = self.groups.get(body["groupID"])
        if group is None:
            return _error(1201, "GroupIDNotFoundError")
        members = [{"userID": u, "roleLevel": 20} for u in body["userIDs"] if u in group.members]
        return _ok({"members": members or None})

    def _invite(self, body: dict[str, Any], _: str) -> httpx.Response:
        group = self.groups.get(body["groupID"])
        if group is None:
            return _error(1201, "GroupIDNotFoundError")
        invited = body["invitedUserIDs"]
        if any(u not in self.users for u in invited):
            return _error(1101, "UserIDNotFoundError")
        if any(u in group.members for u in invited):
            # 实测：邀请已是成员的用户返回 500。
            return _error(500, "mongo insert many", "mongo insert many")
        group.members.update(invited)
        self._notify(group.group_id, MEMBER_INVITED_NOTIFICATION)
        return _ok(None)

    def _kick(self, body: dict[str, Any], _: str) -> httpx.Response:
        group = self.groups.get(body["groupID"])
        if group is None:
            return _error(1201, "GroupIDNotFoundError")
        kicked = body["kickedUserIDs"]
        if any(u not in group.members for u in kicked):
            return _error(1101, "UserIDNotFoundError", kicked[0])
        group.members.difference_update(kicked)
        self._notify(group.group_id, MEMBER_KICKED_NOTIFICATION)
        if self.kick_quirk:
            return _error(1001, "ArgsError", "maxSeq is invalid")
        return _ok(None)

    def _dismiss(self, body: dict[str, Any], _: str) -> httpx.Response:
        if self.groups.pop(body["groupID"], None) is None:
            return _error(1201, "GroupIDNotFoundError")
        return _ok(None)

    def _import_friend(self, body: dict[str, Any], _: str) -> httpx.Response:
        owner = body["ownerUserID"]
        if any(u not in self.users for u in [owner, *body["friendUserIDs"]]):
            return _error(1101, "UserIDNotFoundError")
        for friend in body["friendUserIDs"]:
            self.friends.add(frozenset((owner, friend)))
        return _ok(None)

    def _send_msg(self, body: dict[str, Any], caller: str) -> httpx.Response:
        if body["sessionType"] == 1:
            return self._send_single(body)
        group = self.groups.get(body["groupID"])
        if group is None:
            return _error(1201, "GroupIDNotFoundError")
        if body["sendID"] not in group.members:
            return _error(1203, "NotInGroupYetError")
        msg = self._store(
            send_id=body["sendID"],
            group_id=body["groupID"],
            content_type=body["contentType"],
            content=json.dumps(body["content"], ensure_ascii=False),
            ex=body.get("ex", ""),
            msg_from=200,
            nickname=body.get("senderNickname", ""),
            persist=not body.get("isOnlineOnly", False),
        )
        return _ok(
            {
                "serverMsgID": msg.server_msg_id,
                "clientMsgID": msg.client_msg_id,
                "sendTime": msg.send_time,
            }
        )

    def _send_single(self, body: dict[str, Any]) -> httpx.Response:
        send_id, recv_id = body["sendID"], body["recvID"]
        if send_id != ADMIN_USER_ID and frozenset((send_id, recv_id)) not in self.friends:
            return _error(1303, "NotPeersFriend", "NotPeersFriend")
        if not body.get("isOnlineOnly"):
            raise NotImplementedError("平台只用单聊发在线信令")
        self.signals.append({"send_id": send_id, "recv_id": recv_id, "content": body["content"]})
        now = int(time.time() * 1000) + next(self._counter)
        return _ok(
            {"serverMsgID": uuid.uuid4().hex, "clientMsgID": uuid.uuid4().hex, "sendTime": now}
        )

    def _max_seq(self, body: dict[str, Any], _: str) -> httpx.Response:
        seqs = {}
        for cid in body["conversationIDs"]:
            group = self.groups.get(cid.removeprefix("sg_"))
            # 与真实 OpenIM 一致：不检查 userID 是否为群成员。
            if group is not None:
                max_seq = group.messages[-1].seq if group.messages else 0
                seqs[cid] = {"maxSeq": max_seq, "hasReadSeq": 0, "maxSeqTime": 0}
        return _ok({"seqs": seqs})

    def _pull(self, body: dict[str, Any], _: str) -> httpx.Response:
        result = {}
        for r in body["seqRanges"]:
            group = self.groups.get(r["conversationID"].removeprefix("sg_"))
            if group is None or body["userID"] not in group.members:
                continue
            picked = [m for m in group.messages if r["begin"] <= m.seq <= r["end"]][: r["num"]]
            max_seq = group.messages[-1].seq if group.messages else 0
            end_seq = picked[-1].seq if picked else r["begin"] - 1
            result[r["conversationID"]] = {
                "Msgs": [_wire(m) for m in picked],
                "isEnd": end_seq >= max_seq,
                "endSeq": end_seq,
            }
        return _ok({"msgs": result, "notificationMsgs": None})

    # ---- 存储 ----

    def _notify(self, group_id: str, content_type: int) -> None:
        self._store(
            send_id=ADMIN_USER_ID,
            group_id=group_id,
            content_type=content_type,
            content=json.dumps({"detail": "{}"}),
            ex="",
            msg_from=200,
            nickname="",
            callback=False,
        )

    def _store(
        self,
        *,
        send_id: str,
        group_id: str,
        content_type: int,
        content: str,
        ex: str,
        msg_from: int,
        nickname: str,
        persist: bool = True,
        callback: bool = True,
    ) -> _Message:
        group = self.groups[group_id]
        now = int(time.time() * 1000) + next(self._counter)
        seq = group.messages[-1].seq + 1 if (persist and group.messages) else (1 if persist else 0)
        msg = _Message(
            server_msg_id=uuid.uuid4().hex,
            client_msg_id=uuid.uuid4().hex,
            send_id=send_id,
            group_id=group_id,
            content_type=content_type,
            content=content,
            seq=seq,
            send_time=now,
            ex=ex,
            msg_from=msg_from,
            sender_nickname=nickname,
            sender_platform_id=5,
        )
        if persist:
            group.messages.append(msg)
        if callback:
            self.callbacks.append(_callback(msg))
        return msg


def _wire(m: _Message) -> dict[str, Any]:
    return {
        "sendID": m.send_id,
        "recvID": m.group_id,
        "groupID": m.group_id,
        "clientMsgID": m.client_msg_id,
        "serverMsgID": m.server_msg_id,
        "senderPlatformID": m.sender_platform_id,
        "senderNickname": m.sender_nickname,
        "senderFaceURL": "",
        "sessionType": 3,
        "msgFrom": m.msg_from,
        "contentType": m.content_type,
        "content": base64.b64encode(m.content.encode()).decode(),
        "seq": m.seq,
        "sendTime": m.send_time,
        "createTime": m.send_time,
        "status": 0,
        "isRead": False,
        "options": None,
        "offlinePushInfo": None,
        "atUserIDList": None,
        "attachedInfo": "",
        "ex": m.ex,
    }


def _callback(m: _Message) -> dict[str, Any]:
    return {
        "sendID": m.send_id,
        "callbackCommand": "callbackAfterSendGroupMsgCommand",
        "serverMsgID": m.server_msg_id,
        "clientMsgID": m.client_msg_id,
        "operationID": uuid.uuid4().hex,
        "senderPlatformID": m.sender_platform_id,
        "senderNickname": m.sender_nickname,
        "sessionType": 3,
        "msgFrom": m.msg_from,
        "contentType": m.content_type,
        "status": 0,
        "sendTime": m.send_time,
        "createTime": m.send_time,
        "content": m.content,
        "seq": 0,
        "atUserList": None,
        "faceURL": "",
        "ex": m.ex,
        "groupID": m.group_id,
    }


def _ok(data: Any) -> httpx.Response:
    body: dict[str, Any] = {"errCode": 0, "errMsg": "", "errDlt": ""}
    if data is not None:
        body["data"] = data
    return httpx.Response(200, json=body)


def _error(code: int, message: str, detail: str = "") -> httpx.Response:
    return httpx.Response(200, json={"errCode": code, "errMsg": message, "errDlt": detail})
