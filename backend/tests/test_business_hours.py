from datetime import UTC, datetime, timedelta

import pytest

from app.modules.routing.hours import (
    add_business_minutes,
    business_day_minutes,
    business_minutes_between,
    day_start,
    in_business_hours,
    is_business_day,
    validate_business_hours,
)

WEEKDAYS = {"tz": "Asia/Shanghai", "days": {str(d): [["09:00", "18:00"]] for d in range(1, 6)}}


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # 2026-09-28 是周一；上海时间 = UTC + 8。
        (datetime(2026, 9, 28, 1, 0, tzinfo=UTC), True),  # 周一 09:00
        (datetime(2026, 9, 28, 0, 59, tzinfo=UTC), False),  # 周一 08:59
        (datetime(2026, 9, 28, 10, 0, tzinfo=UTC), False),  # 周一 18:00（结束时刻不含）
        (datetime(2026, 10, 3, 3, 0, tzinfo=UTC), False),  # 周六
        (datetime(2026, 10, 2, 16, 30, tzinfo=UTC), False),  # UTC 周五，上海已是周六 00:30
    ],
)
def test_in_business_hours_uses_the_policy_timezone(utc: datetime, expected: bool) -> None:
    assert in_business_hours(WEEKDAYS, utc) is expected


def test_empty_spec_means_always_open() -> None:
    assert in_business_hours(None, datetime(2026, 10, 3, tzinfo=UTC))
    assert in_business_hours({}, datetime(2026, 10, 3, tzinfo=UTC))


def test_until_midnight() -> None:
    spec = validate_business_hours({"days": {"1": [["20:00", "24:00"]]}})
    assert in_business_hours(spec, datetime(2026, 9, 28, 15, 59, tzinfo=UTC))  # 周一 23:59


@pytest.mark.parametrize(
    "spec",
    [
        {"tz": "Nowhere/City", "days": {}},
        {"days": []},
        {"days": {"8": []}},
        {"days": {"1": [["9:00", "18:00"]]}},
        {"days": {"1": [["18:00", "18:00"]]}},
        {"days": {"1": [["09:00"]]}},
        {"days": {"1": "09:00-18:00"}},
    ],
)
def test_invalid_specs_are_rejected(spec: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_business_hours(spec)


def test_spec_is_normalized() -> None:
    assert validate_business_hours({"days": {1: [["13:00", "18:00"], ["09:00", "12:00"]]}}) == {
        "tz": "Asia/Shanghai",
        "days": {"1": [["09:00", "12:00"], ["13:00", "18:00"]]},
    }


# ---- 按工作时间计时（待办的时限） ----

# 上海时间，2026-10-02 是周五。
FRIDAY_5PM = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
MONDAY_9AM = datetime(2026, 10, 5, 1, 0, tzinfo=UTC)


def test_business_minutes_skip_nights_and_weekends() -> None:
    # 周五 17:00 开始 4 个工作小时：周五 1 小时 + 周一 3 小时 → 周一 12:00。
    assert add_business_minutes(WEEKDAYS, FRIDAY_5PM, 240) == MONDAY_9AM + timedelta(hours=3)
    # 周六开始：从周一上班算起。
    saturday = datetime(2026, 10, 3, 2, 0, tzinfo=UTC)
    assert add_business_minutes(WEEKDAYS, saturday, 60) == MONDAY_9AM + timedelta(hours=1)
    # 0 分钟：非工作时间时是下一个上班时间。
    assert add_business_minutes(WEEKDAYS, saturday, 0) == MONDAY_9AM
    # 全天服务或没有任何工作日时按自然时间。
    assert add_business_minutes(None, FRIDAY_5PM, 240) == FRIDAY_5PM + timedelta(hours=4)
    assert add_business_minutes({"days": {}}, FRIDAY_5PM, 60) == FRIDAY_5PM + timedelta(hours=1)


def test_business_minutes_between_and_a_business_day() -> None:
    assert business_minutes_between(WEEKDAYS, FRIDAY_5PM, MONDAY_9AM + timedelta(hours=3)) == 240
    assert business_minutes_between(WEEKDAYS, MONDAY_9AM, FRIDAY_5PM) == 0
    assert business_minutes_between(None, FRIDAY_5PM, FRIDAY_5PM + timedelta(hours=2)) == 120
    assert business_day_minutes(WEEKDAYS) == 540
    assert business_day_minutes(None) == 1440
    # 一个工作日：同一时间的下一个工作日。
    monday_3pm = MONDAY_9AM + timedelta(hours=6)
    assert add_business_minutes(WEEKDAYS, monday_3pm, 540) == monday_3pm + timedelta(days=1)


def test_business_day_and_day_start() -> None:
    assert is_business_day(WEEKDAYS, MONDAY_9AM)
    assert not is_business_day(WEEKDAYS, datetime(2026, 10, 3, 2, 0, tzinfo=UTC))
    assert day_start(WEEKDAYS, MONDAY_9AM + timedelta(hours=5)) == MONDAY_9AM
    assert day_start(WEEKDAYS, datetime(2026, 10, 3, 2, 0, tzinfo=UTC)) is None
    # 全天服务按 9 点上班。
    assert day_start(None, MONDAY_9AM + timedelta(hours=5)) == MONDAY_9AM
