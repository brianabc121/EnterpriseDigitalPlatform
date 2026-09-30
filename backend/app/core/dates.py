"""按时区划分日期：用量汇总与报表共用。"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.errors import Unprocessable

MAX_DAYS = 366


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise Unprocessable(f"未知的时区：{name}") from exc


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """某一天在 tz 时区的起止时间（左闭右开）。"""
    return (
        datetime.combine(day, time.min, tzinfo=tz),
        datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz),
    )


def today(tz: ZoneInfo, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(tz).date()


def date_range(
    start: date | None, end: date | None, tz: ZoneInfo, *, default_days: int
) -> tuple[date, date]:
    """查询范围（含两端）：默认截至今天的 default_days 天，最多一年。"""
    end = end or today(tz)
    start = start or end - timedelta(days=default_days - 1)
    if start > end:
        raise Unprocessable("开始日期不能晚于结束日期")
    if (end - start).days + 1 > MAX_DAYS:
        raise Unprocessable(f"查询范围不能超过 {MAX_DAYS} 天")
    return start, end
