"""盈利报表的期间（设计文档 §30.3）：按月移动的上期、去年同期，以及每月趋势的月份。

期间是两端都含的日期（租户时区）。按月移动时，开始日取目标月的同一天（没有这一天时取月底）；
结束日是月底时移过去也是月底，否则取同一天——这样"本月 1–2 日"的上期是"上月 1–2 日"，
"9 月"的上期是"8 月"。
"""

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.errors import Unprocessable

MAX_MONTHS = 36
TREND_MONTHS = 12


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    def bounds(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        """租户时区的起止时间（左闭右开）。"""
        return (
            datetime.combine(self.start, time.min, tzinfo=tz),
            datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=tz),
        )


def month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def is_month_end(day: date) -> bool:
    return day == month_end(day)


def add_months(day: date, months: int, *, to_month_end: bool = False) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    last = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, last if to_month_end else min(day.day, last))


def month_span(start: date, end: date) -> int:
    """跨了几个月（含两端所在的月）。"""
    return (end.year - start.year) * 12 + end.month - start.month + 1


def check(start: date, end: date) -> Period:
    if start > end:
        raise Unprocessable("开始日期不能晚于结束日期")
    if month_span(start, end) > MAX_MONTHS:
        raise Unprocessable(f"期间最长 {MAX_MONTHS} 个月")
    return Period(start, end)


def shifted(period: Period, months: int) -> Period:
    """往前移 months 个月。"""
    return Period(
        add_months(period.start, -months),
        add_months(period.end, -months, to_month_end=is_month_end(period.end)),
    )


def previous(period: Period, shift: int | None = None) -> Period:
    """上期：指定了移几个月就按月移；从 1 日开始的期间按跨的月数移；其他期间取紧挨着的同样天数。"""
    if shift is not None:
        return shifted(period, shift)
    if period.start.day == 1:
        return shifted(period, month_span(period.start, period.end))
    days = (period.end - period.start).days + 1
    return Period(period.start - timedelta(days=days), period.start - timedelta(days=1))


def last_year(period: Period) -> Period:
    return shifted(period, 12)


def trend_months(end: date, months: int = TREND_MONTHS) -> list[date]:
    """截至 end 所在月的 months 个月（每月 1 日，从早到晚）。"""
    first = end.replace(day=1)
    return [add_months(first, -i) for i in range(months - 1, -1, -1)]


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def parse_month(value: str) -> date:
    """ "2026-10" → 2026-10-01。"""
    try:
        year, month = (int(part) for part in value.split("-"))
        return date(year, month, 1)
    except ValueError as exc:
        raise Unprocessable("月份的格式是 2026-10") from exc
