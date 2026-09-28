"""工作时间。

格式：{"tz": "Asia/Shanghai", "days": {"1": [["09:00", "18:00"]], ...}}，
days 的键是 ISO 星期（1 为周一，7 为周日），值是当天的若干个 [开始, 结束) 时段；
没有列出的日子全天休息。结束时间可以写 "24:00"。
"""

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "Asia/Shanghai"
_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$")


def validate_business_hours(spec: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化工作时间配置，不合法时抛出 ValueError。"""
    tz = spec.get("tz", DEFAULT_TZ)
    try:
        ZoneInfo(str(tz))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"未知时区：{tz}") from exc
    days = spec.get("days")
    if not isinstance(days, dict):
        raise ValueError("days 必须是以星期（1-7）为键的对象")
    normalized: dict[str, list[list[str]]] = {}
    for day, ranges in days.items():
        if str(day) not in {"1", "2", "3", "4", "5", "6", "7"}:
            raise ValueError(f"星期必须是 1 到 7：{day}")
        if not isinstance(ranges, list):
            raise ValueError("每天的时段必须是列表")
        clean: list[list[str]] = []
        for item in ranges:
            if (
                not isinstance(item, list | tuple)
                or len(item) != 2
                or not all(isinstance(t, str) and _TIME.match(t) for t in item)
                or item[0] >= item[1]
            ):
                raise ValueError(f'时段格式应为 ["HH:MM", "HH:MM"] 且开始早于结束：{item}')
            clean.append([item[0], item[1]])
        normalized[str(day)] = sorted(clean)
    return {"tz": str(tz), "days": normalized}


def in_business_hours(spec: dict[str, Any] | None, now: datetime) -> bool:
    """spec 为空表示全天服务。"""
    if not spec:
        return True
    local = now.astimezone(ZoneInfo(spec.get("tz") or DEFAULT_TZ))
    moment = local.strftime("%H:%M")
    ranges = (spec.get("days") or {}).get(str(local.isoweekday()), [])
    return any(start <= moment < end for start, end in ranges)
