"""平台对象与 OpenIM ID 的对应约定（实施计划 §7.2）。

租户代码只含小写字母、数字和 "-"，不含下划线，所以按第一个下划线就能拆出租户。
"""

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum

_TENANT_CODE = re.compile(r"[a-z][a-z0-9-]{2,31}")
_HEX_ID = re.compile(r"[0-9a-f]{32}")


class Kind(StrEnum):
    CUSTOMER = "c"
    STAFF = "s"
    ROOM = "r"
    BOT = "bot"
    SYSTEM = "sys"


def customer_user(tenant_code: str, identity_id: uuid.UUID) -> str:
    return f"{tenant_code}_{Kind.CUSTOMER}_{identity_id.hex}"


def staff_user(tenant_code: str, staff_id: uuid.UUID) -> str:
    return f"{tenant_code}_{Kind.STAFF}_{staff_id.hex}"


def bot_user(tenant_code: str) -> str:
    return f"{tenant_code}_{Kind.BOT}"


def system_user(tenant_code: str) -> str:
    return f"{tenant_code}_{Kind.SYSTEM}"


def room_group(tenant_code: str, room_id: uuid.UUID) -> str:
    return f"{tenant_code}_{Kind.ROOM}_{room_id.hex}"


@dataclass(frozen=True)
class ParsedId:
    tenant_code: str
    kind: Kind
    object_id: uuid.UUID | None


def parse(im_id: str) -> ParsedId | None:
    """解析平台约定的 IM ID；不符合约定的返回 None。"""
    tenant_code, sep, rest = im_id.partition("_")
    if not sep or not _TENANT_CODE.fullmatch(tenant_code):
        return None
    if rest in (Kind.BOT, Kind.SYSTEM):
        return ParsedId(tenant_code, Kind(rest), None)
    kind, sep, hex_id = rest.partition("_")
    if not sep or kind not in (Kind.CUSTOMER, Kind.STAFF, Kind.ROOM):
        return None
    if not _HEX_ID.fullmatch(hex_id):
        return None
    return ParsedId(tenant_code, Kind(kind), uuid.UUID(hex=hex_id))
