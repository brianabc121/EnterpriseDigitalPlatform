from uuid import UUID

from uuid_utils.compat import uuid7


def new_id() -> UUID:
    """时间有序的 UUIDv7，作为所有表的主键。"""
    return uuid7()
