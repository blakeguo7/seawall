from datetime import datetime, timedelta, timezone

import pytest

from clock import format_local, to_local

UTC_NOON = datetime(2024, 3, 10, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (0, "2024-03-10 12:00 +00:00"),
        (1, "2024-03-10 13:00 +01:00"),
        (5.5, "2024-03-10 17:30 +05:30"),
        (-3.5, "2024-03-10 08:30 -03:30"),
        (-12, "2024-03-10 00:00 -12:00"),
        (14, "2024-03-11 02:00 +14:00"),
        (5.75, "2024-03-10 17:45 +05:45"),
    ],
)
def test_format_local(offset, expected):
    assert format_local(UTC_NOON, offset) == expected


def test_result_is_aware_with_the_requested_offset():
    local = to_local(UTC_NOON, 5.5)
    assert local.tzinfo is not None
    assert local.utcoffset() == timedelta(hours=5, minutes=30)


def test_same_instant():
    assert to_local(UTC_NOON, -7) == UTC_NOON
    assert to_local(UTC_NOON, 9.5) == UTC_NOON


def test_naive_input_is_rejected():
    with pytest.raises(ValueError):
        to_local(datetime(2024, 3, 10, 12, 0), 1)
    with pytest.raises(ValueError):
        format_local(datetime(2024, 3, 10, 12, 0), 1)


def test_other_zones_are_converted_properly():
    plus_two = timezone(timedelta(hours=2))
    ts = datetime(2024, 3, 10, 12, 0, tzinfo=plus_two)  # 10:00 UTC
    assert format_local(ts, 0) == "2024-03-10 10:00 +00:00"
    assert format_local(ts, 5.5) == "2024-03-10 15:30 +05:30"


@pytest.mark.parametrize("offset", [-12.5, 14.5, 20, -13])
def test_out_of_range_offsets(offset):
    with pytest.raises(ValueError):
        to_local(UTC_NOON, offset)


def test_date_rolls_over():
    late = datetime(2024, 12, 31, 23, 30, tzinfo=timezone.utc)
    assert format_local(late, 1) == "2025-01-01 00:30 +01:00"
