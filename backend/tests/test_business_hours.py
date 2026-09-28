from datetime import UTC, datetime

import pytest

from app.modules.routing.hours import in_business_hours, validate_business_hours

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
