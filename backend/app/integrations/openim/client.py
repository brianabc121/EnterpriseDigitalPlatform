"""OpenIM Server（v3.8）REST 客户端。

只封装平台用到的接口，全部以应用管理员身份调用：管理员令牌按有效期缓存，
遇到令牌失效类错误时换一次新令牌再重试。接口行为（错误码、返回结构）以
docs/plans/2026-09-28-p0-p1-implementation-plan.md §7.1 的实测结论为准。
"""

import asyncio
import base64
import binascii
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import httpx

WEB_PLATFORM_ID = 5
# 单聊会话，平台只用来给员工发在线信令。
SINGLE_SESSION_TYPE = 1
# 工作群会话（OpenIM 的 ReadGroupChatType），服务群都是这种类型。
GROUP_SESSION_TYPE = 3
_WORKING_GROUP = 2
_PULL_BATCH = 100
# 被删除的消息仍然占用 seq，拉取时会以这个状态返回。
_MSG_STATUS_DELETED = 4


class ContentType(IntEnum):
    TEXT = 101
    PICTURE = 102
    VOICE = 103
    VIDEO = 104
    FILE = 105
    AT_TEXT = 106
    CUSTOM = 110
    QUOTE = 114
    # 1000 到 5000 之间是各类系统通知（建群、进群、踢人等）。
    NOTIFICATION_BEGIN = 1000
    NOTIFICATION_END = 5000


class ErrCode(IntEnum):
    ARGS = 1001
    NO_PERMISSION = 1002
    RECORD_NOT_FOUND = 1004
    USER_NOT_FOUND = 1101
    USER_REGISTERED = 1102
    GROUP_NOT_FOUND = 1201
    GROUP_EXISTS = 1202


# 令牌类错误：Expired、Invalid、Malformed、NotValidYet、Unknown、Kicked、NotExist
_TOKEN_ERRORS = frozenset(range(1501, 1508))


class OpenIMError(Exception):
    """OpenIM 返回了业务错误（errCode 非 0）。"""

    def __init__(self, code: int, message: str, detail: str = "") -> None:
        super().__init__(f"OpenIM error {code}: {message} {detail}".strip())
        self.code = code
        self.message = message
        self.detail = detail


class OpenIMUnavailable(OpenIMError):
    """网络错误、超时或服务端异常：调用方可以稍后重试。"""

    def __init__(self, message: str) -> None:
        super().__init__(-1, message)


@dataclass(frozen=True)
class IMUser:
    user_id: str
    nickname: str
    face_url: str = ""


@dataclass(frozen=True)
class UserToken:
    token: str
    expires_in: int


@dataclass(frozen=True)
class SentMessage:
    server_msg_id: str
    client_msg_id: str
    send_time: int


@dataclass(frozen=True)
class SeqRange:
    conversation_id: str
    begin: int
    end: int


@dataclass(frozen=True)
class IMMessage:
    """拉取到的一条消息。content 是解码后的 JSON 文本（与回调里的 content 一致）。"""

    server_msg_id: str
    client_msg_id: str
    send_id: str
    group_id: str
    session_type: int
    msg_from: int
    content_type: int
    content: str
    seq: int
    send_time: int
    ex: str
    sender_nickname: str
    sender_platform_id: int
    status: int

    @property
    def is_notification(self) -> bool:
        return ContentType.NOTIFICATION_BEGIN <= self.content_type <= ContentType.NOTIFICATION_END

    @property
    def is_deleted(self) -> bool:
        return self.status == _MSG_STATUS_DELETED


@dataclass(frozen=True)
class PulledConversation:
    messages: list[IMMessage]
    is_end: bool
    end_seq: int


def group_conversation_id(group_id: str) -> str:
    return f"sg_{group_id}"


class OpenIMClient:
    def __init__(
        self,
        base_url: str,
        *,
        secret: str,
        admin_user_id: str = "imAdmin",
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self._secret = secret
        self._admin_user_id = admin_user_id
        self._admin_token: str | None = None
        self._refresh_at = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def ping(self) -> None:
        """健康检查：用管理员身份调用一个只读接口。"""
        await self._admin_call("/user/account_check", {"checkUserIDs": [self._admin_user_id]})

    # ---- 用户 ----

    async def ensure_users(self, users: Sequence[IMUser]) -> None:
        """注册尚未注册的用户。

        只要批次里有一个用户已存在，OpenIM 就拒绝整批（1102）。并发注册时重新检查，
        只注册仍然缺少的用户。
        """
        for _ in range(3):
            missing = await self._unregistered([u.user_id for u in users])
            if not missing:
                return
            body = {
                "users": [
                    {"userID": u.user_id, "nickname": u.nickname, "faceURL": u.face_url}
                    for u in users
                    if u.user_id in missing
                ]
            }
            try:
                await self._admin_call("/user/user_register", body)
                return
            except OpenIMError as exc:
                if exc.code != ErrCode.USER_REGISTERED:
                    raise
        raise OpenIMError(ErrCode.USER_REGISTERED, "concurrent registration did not settle")

    async def _unregistered(self, user_ids: list[str]) -> set[str]:
        data = await self._admin_call("/user/account_check", {"checkUserIDs": user_ids})
        return {r["userID"] for r in data["results"] if r["accountStatus"] != 1}

    async def get_user_token(self, user_id: str, platform_id: int = WEB_PLATFORM_ID) -> UserToken:
        data = await self._admin_call(
            "/auth/get_user_token", {"platformID": platform_id, "userID": user_id}
        )
        return UserToken(token=data["token"], expires_in=int(data["expireTimeSeconds"]))

    # ---- 群 ----

    async def ensure_group(
        self,
        *,
        group_id: str,
        name: str,
        owner_user_id: str,
        member_user_ids: Sequence[str],
        ex: str = "",
    ) -> bool:
        """创建工作群；群已存在时返回 False（不校验已有群的成员）。"""
        body = {
            "ownerUserID": owner_user_id,
            "memberUserIDs": list(member_user_ids),
            "adminUserIDs": [],
            "groupInfo": {
                "groupID": group_id,
                "groupName": name,
                "groupType": _WORKING_GROUP,
                "ex": ex,
            },
        }
        try:
            await self._admin_call("/group/create_group", body)
        except OpenIMError as exc:
            if exc.code == ErrCode.GROUP_EXISTS:
                return False
            raise
        return True

    async def group_member_ids(self, group_id: str, user_ids: Sequence[str]) -> set[str]:
        """user_ids 中已经是群成员的用户。"""
        data = await self._admin_call(
            "/group/get_group_members_info", {"groupID": group_id, "userIDs": list(user_ids)}
        )
        return {m["userID"] for m in data.get("members") or []}

    async def add_group_members(self, group_id: str, user_ids: Sequence[str]) -> None:
        """把用户拉进群。实测：邀请已是成员的用户会返回 500，所以只邀请还不在群里的用户。"""
        present = await self.group_member_ids(group_id, user_ids)
        missing = [u for u in user_ids if u not in present]
        if missing:
            await self._admin_call(
                "/group/invite_user_to_group",
                {"groupID": group_id, "invitedUserIDs": missing, "reason": ""},
            )

    async def remove_group_members(self, group_id: str, user_ids: Sequence[str]) -> None:
        """把用户移出群。

        实测：被移出的用户从未同步过这个会话时，OpenIM 已经移除成员，却返回
        1001 "maxSeq is invalid"。所以出错时以成员列表为准。
        """
        present = await self.group_member_ids(group_id, user_ids)
        if not present:
            return
        try:
            await self._admin_call(
                "/group/kick_group",
                {"groupID": group_id, "kickedUserIDs": sorted(present), "reason": ""},
            )
        except OpenIMError as exc:
            if exc.code != ErrCode.ARGS or await self.group_member_ids(group_id, sorted(present)):
                raise

    async def dismiss_group(self, group_id: str) -> bool:
        """解散群（租户注销删除数据时）。群不存在时返回 False。"""
        try:
            await self._admin_call(
                "/group/dismiss_group", {"groupID": group_id, "deleteMember": True}
            )
        except OpenIMError as exc:
            if exc.code in (ErrCode.GROUP_NOT_FOUND, ErrCode.RECORD_NOT_FOUND):
                return False
            raise
        return True

    # ---- 登录 ----

    async def force_logout(self, user_id: str, platform_id: int = WEB_PLATFORM_ID) -> None:
        """让用户在这个平台上的登录失效（员工停用时）。"""
        await self._admin_call("/auth/force_logout", {"platformID": platform_id, "userID": user_id})

    # ---- 好友 ----

    async def import_friends(self, owner_user_id: str, friend_user_ids: Sequence[str]) -> None:
        """直接建立好友关系（幂等）。单聊开启了好友校验，信令发送方必须是员工的好友。"""
        await self._admin_call(
            "/friend/import_friend",
            {"ownerUserID": owner_user_id, "friendUserIDs": list(friend_user_ids)},
        )

    # ---- 消息 ----

    async def send_online_only(
        self, *, send_id: str, recv_id: str, content: dict[str, Any]
    ) -> SentMessage:
        """单聊在线消息（不落库、不占 seq）。接收方离线时直接丢弃，用于实时信令。"""
        body = {
            "sendID": send_id,
            "recvID": recv_id,
            "senderPlatformID": WEB_PLATFORM_ID,
            "content": content,
            "contentType": ContentType.CUSTOM,
            "sessionType": SINGLE_SESSION_TYPE,
            "isOnlineOnly": True,
        }
        data = await self._admin_call("/msg/send_msg", body)
        return SentMessage(
            server_msg_id=data["serverMsgID"],
            client_msg_id=data["clientMsgID"],
            send_time=int(data["sendTime"]),
        )

    async def send_group_message(
        self,
        *,
        send_id: str,
        group_id: str,
        content_type: int,
        content: dict[str, Any],
        sender_nickname: str = "",
        ex: str = "",
        online_only: bool = False,
        platform_id: int = WEB_PLATFORM_ID,
    ) -> SentMessage:
        body = {
            "sendID": send_id,
            "groupID": group_id,
            "senderNickname": sender_nickname,
            "senderPlatformID": platform_id,
            "content": content,
            "contentType": content_type,
            "sessionType": GROUP_SESSION_TYPE,
            "isOnlineOnly": online_only,
            "ex": ex,
        }
        data = await self._admin_call("/msg/send_msg", body)
        return SentMessage(
            server_msg_id=data["serverMsgID"],
            client_msg_id=data["clientMsgID"],
            send_time=int(data["sendTime"]),
        )

    async def max_seqs(self, user_id: str, conversation_ids: Sequence[str]) -> dict[str, int]:
        """各会话当前的最大 seq。

        实测：返回值与 user_id 是否在群里无关；只有拉取消息时才按成员身份过滤。
        """
        data = await self._admin_call(
            "/msg/get_conversations_has_read_and_max_seq",
            {"userID": user_id, "conversationIDs": list(conversation_ids)},
        )
        seqs = data.get("seqs") or {}
        return {cid: int(v.get("maxSeq", 0)) for cid, v in seqs.items()}

    async def pull_messages(self, user_id: str, seq_range: SeqRange) -> PulledConversation:
        """按 seq 区间（含两端）升序拉取一个会话的消息，每次最多 100 条。"""
        end = min(seq_range.end, seq_range.begin + _PULL_BATCH - 1)
        body = {
            "userID": user_id,
            "seqRanges": [
                {
                    "conversationID": seq_range.conversation_id,
                    "begin": seq_range.begin,
                    "end": end,
                    "num": end - seq_range.begin + 1,
                }
            ],
            "order": 0,
        }
        data = await self._admin_call("/msg/pull_msg_by_seq", body)
        conv = (data.get("msgs") or {}).get(seq_range.conversation_id) or {}
        messages = [_parse_message(m) for m in conv.get("Msgs") or []]
        return PulledConversation(
            messages=messages,
            is_end=bool(conv.get("isEnd", True)),
            end_seq=int(conv.get("endSeq", end)),
        )

    # ---- 底层调用 ----

    async def _admin_call(self, path: str, body: dict[str, Any]) -> Any:
        token = await self._admin_token_value()
        try:
            return await self._post(path, body, token=token)
        except OpenIMError as exc:
            if exc.code not in _TOKEN_ERRORS:
                raise
        return await self._post(path, body, token=await self._admin_token_value(stale=token))

    async def _admin_token_value(self, stale: str | None = None) -> str:
        """缓存的管理员令牌。stale 是刚被拒绝的令牌：缓存里还是它时才重新获取，
        避免并发请求在令牌失效时各自重复获取。"""
        async with self._token_lock:
            token = self._admin_token
            if token is not None and token != stale and time.monotonic() < self._refresh_at:
                return token
            data = await self._post(
                "/auth/get_admin_token",
                {"secret": self._secret, "userID": self._admin_user_id},
                token=None,
            )
            ttl = int(data["expireTimeSeconds"])
            self._admin_token = str(data["token"])
            # 在到期前留出 10%（最多 1 小时）的余量。
            self._refresh_at = time.monotonic() + ttl - min(ttl // 10, 3600)
            return self._admin_token

    async def _post(self, path: str, body: dict[str, Any], *, token: str | None) -> Any:
        headers = {"operationID": uuid.uuid4().hex}
        if token is not None:
            headers["token"] = token
        try:
            response = await self._http.post(path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise OpenIMUnavailable(f"{path}: {exc.__class__.__name__}") from exc
        if response.status_code >= 500:
            raise OpenIMUnavailable(f"{path}: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpenIMUnavailable(f"{path}: invalid JSON (HTTP {response.status_code})") from exc
        code = int(payload.get("errCode", 0))
        if code != 0:
            raise OpenIMError(code, payload.get("errMsg", ""), payload.get("errDlt", ""))
        return payload.get("data")


def _parse_message(raw: dict[str, Any]) -> IMMessage:
    try:
        content = base64.b64decode(raw.get("content") or "").decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        content = ""
    return IMMessage(
        server_msg_id=raw.get("serverMsgID", ""),
        client_msg_id=raw.get("clientMsgID", ""),
        send_id=raw.get("sendID", ""),
        group_id=raw.get("groupID", ""),
        session_type=int(raw.get("sessionType", 0)),
        msg_from=int(raw.get("msgFrom", 0)),
        content_type=int(raw.get("contentType", 0)),
        content=content,
        seq=int(raw.get("seq", 0)),
        send_time=int(raw.get("sendTime", 0)),
        ex=raw.get("ex", ""),
        sender_nickname=raw.get("senderNickname", ""),
        sender_platform_id=int(raw.get("senderPlatformID", 0)),
        status=int(raw.get("status", 0)),
    )
