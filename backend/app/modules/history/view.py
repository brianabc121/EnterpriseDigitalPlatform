"""查看历史时的上下文：名称、时区、查看人能不能看成本价；以及金额、数量、时间的显示格式。"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from app.modules.history.names import Names


@dataclass
class View:
    names: Names
    tz: ZoneInfo
    can_see_cost: bool = False

    def time(self, value: Any) -> str:
        """ISO 时间 → "2026-10-01 09:30"（租户的时区）。"""
        if not value:
            return ""
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
        if moment.tzinfo is None:
            return moment.strftime("%Y-%m-%d %H:%M")
        return moment.astimezone(self.tz).strftime("%Y-%m-%d %H:%M")


def money(value: Any, empty: str = "") -> str:
    if value is None or value == "":
        return empty
    try:
        return f"¥{Decimal(str(value)):,.2f}"
    except InvalidOperation:
        return str(value)


def qty(value: Any) -> str:
    """数量去掉多余的 0：2.500 → 2.5。"""
    if value is None or value == "":
        return ""
    try:
        text = f"{Decimal(str(value)).normalize():f}"
    except InvalidOperation:
        return str(value)
    return "0" if text in ("-0", "0") else text


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return "、".join(str(v) for v in value if v not in (None, ""))
    return str(value)
