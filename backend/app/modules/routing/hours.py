"""工作时间。

格式：{"tz": "Asia/Shanghai", "days": {"1": [["09:00", "18:00"]], ...}}，
days 的键是 ISO 星期（1 为周一，7 为周日），值是当天的若干个 [开始, 结束) 时段；
没有列出的日子全天休息。结束时间可以写 "24:00"。
"""

import re
from datetime import UTC, date, datetime, time, timedelta
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


# ---- 按工作时间计时（待办的时限，设计文档 §24.6） ----

# 最多向后找这么多天的工作时段（没有任何工作日的配置按自然时间计算）。
_SEARCH_DAYS = 400


def _segments(spec: dict[str, Any], day: date, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """某一天的工作时段（当地时间，按开始时间排序）。"""
    result = []
    for start, end in (spec.get("days") or {}).get(str(day.isoweekday()), []):
        begin = datetime.combine(day, _clock(start), tzinfo=tz)
        finish = (
            datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
            if end == "24:00"
            else datetime.combine(day, _clock(end), tzinfo=tz)
        )
        result.append((begin, finish))
    return sorted(result)


def _clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _has_hours(spec: dict[str, Any] | None) -> bool:
    return bool(spec) and any((spec or {}).get("days", {}).values())


def add_business_minutes(spec: dict[str, Any] | None, start: datetime, minutes: int) -> datetime:
    """从 start 开始累计 minutes 个工作分钟后的时间。spec 为空（全天服务）时按自然时间。"""
    if not _has_hours(spec):
        return start + timedelta(minutes=minutes)
    assert spec is not None
    tz = ZoneInfo(spec.get("tz") or DEFAULT_TZ)
    cursor = start.astimezone(tz)
    remaining = timedelta(minutes=max(0, minutes))
    day = cursor.date()
    for _ in range(_SEARCH_DAYS):
        for begin, finish in _segments(spec, day, tz):
            if finish <= cursor:
                continue
            begin = max(begin, cursor)
            if remaining <= finish - begin:
                return (begin + remaining).astimezone(UTC)
            remaining -= finish - begin
            cursor = finish
        day += timedelta(days=1)
    return start + timedelta(minutes=minutes)


def business_minutes_between(spec: dict[str, Any] | None, start: datetime, end: datetime) -> int:
    """start 到 end 之间的工作分钟数（end 早于 start 时为 0）。"""
    if end <= start:
        return 0
    if not _has_hours(spec):
        return int((end - start).total_seconds() // 60)
    assert spec is not None
    tz = ZoneInfo(spec.get("tz") or DEFAULT_TZ)
    total = timedelta()
    day = start.astimezone(tz).date()
    last = end.astimezone(tz).date()
    for _ in range(_SEARCH_DAYS):
        if day > last:
            break
        for begin, finish in _segments(spec, day, tz):
            overlap = min(finish, end) - max(begin, start)
            if overlap > timedelta():
                total += overlap
        day += timedelta(days=1)
    return int(total.total_seconds() // 60)


def business_day_minutes(spec: dict[str, Any] | None) -> int:
    """一个工作日有多少工作分钟（取各工作日中最长的一天；全天服务为 24 小时）。"""
    if not _has_hours(spec):
        return 24 * 60
    assert spec is not None
    longest = 0
    for ranges in (spec.get("days") or {}).values():
        minutes = 0
        for start, end in ranges:
            finish = 24 * 60 if end == "24:00" else _clock(end).hour * 60 + _clock(end).minute
            minutes += finish - (_clock(start).hour * 60 + _clock(start).minute)
        longest = max(longest, minutes)
    return longest or 24 * 60


def is_business_day(spec: dict[str, Any] | None, now: datetime) -> bool:
    """now 所在的这一天是否有工作时段（全天服务时每天都是）。"""
    if not _has_hours(spec):
        return True
    assert spec is not None
    local = now.astimezone(ZoneInfo(spec.get("tz") or DEFAULT_TZ))
    return bool((spec.get("days") or {}).get(str(local.isoweekday())))


def day_start(spec: dict[str, Any] | None, now: datetime) -> datetime | None:
    """now 所在这一天的上班时间（全天服务时按 9 点）；这一天休息时为空。"""
    tz = ZoneInfo((spec or {}).get("tz") or DEFAULT_TZ)
    local = now.astimezone(tz)
    if not _has_hours(spec):
        return datetime.combine(local.date(), time(9), tzinfo=tz)
    assert spec is not None
    segments = _segments(spec, local.date(), tz)
    return segments[0][0] if segments else None
