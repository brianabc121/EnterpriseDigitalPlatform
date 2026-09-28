"""平台对象与 OpenIM ID 的对应约定（实施计划 §7.2）。

OpenIM 的用户 ID 只接受字母、数字和下划线（实测：含 "-" 等字符时返回 ArgsError）。
租户代码只含小写字母、数字和 "-"，所以 ID 前缀把 "-" 换成大写 "X"：租户代码里
不会出现大写字母，替换可逆。前缀里没有下划线，按第一个下划线就能拆出租户。
"""

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum

_IM_PREFIX = re.compile(r"[a-z][a-z0-9X]{2,31}")
_HEX_ID = re.compile(r"[0-9a-f]{32}")


class Kind(StrEnum):
    CUSTOMER = "c"
    STAFF = "s"
    ROOM = "r"
    BOT = "bot"
    SYSTEM = "sys"


def im_prefix(tenant_code: str) -> str:
    return tenant_code.replace("-", "X")


def customer_user(tenant_code: str, identity_id: uuid.UUID) -> str:
    return f"{im_prefix(tenant_code)}_{Kind.CUSTOMER}_{identity_id.hex}"


def staff_user(tenant_code: str, staff_id: uuid.UUID) -> str:
    return f"{im_prefix(tenant_code)}_{Kind.STAFF}_{staff_id.hex}"


def bot_user(tenant_code: str) -> str:
    return f"{im_prefix(tenant_code)}_{Kind.BOT}"


def system_user(tenant_code: str) -> str:
    return f"{im_prefix(tenant_code)}_{Kind.SYSTEM}"


def room_group(tenant_code: str, room_id: uuid.UUID) -> str:
    return f"{im_prefix(tenant_code)}_{Kind.ROOM}_{room_id.hex}"


@dataclass(frozen=True)
class ParsedId:
    tenant_code: str
    kind: Kind
    object_id: uuid.UUID | None


def parse(im_id: str) -> ParsedId | None:
    """解析平台约定的 IM ID；不符合约定的返回 None。返回的是租户代码（已还原 "-"）。"""
    prefix, sep, rest = im_id.partition("_")
    if not sep or not _IM_PREFIX.fullmatch(prefix):
        return None
    tenant_code = prefix.replace("X", "-")
    if rest in (Kind.BOT, Kind.SYSTEM):
        return ParsedId(tenant_code, Kind(rest), None)
    kind, sep, hex_id = rest.partition("_")
    if not sep or kind not in (Kind.CUSTOMER, Kind.STAFF, Kind.ROOM):
        return None
    if not _HEX_ID.fullmatch(hex_id):
        return None
    return ParsedId(tenant_code, Kind(kind), uuid.UUID(hex=hex_id))
