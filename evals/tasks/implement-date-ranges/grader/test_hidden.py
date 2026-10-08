from datetime import date

import pytest

from ranges import merge_ranges, overlaps


def d(day):
    return date(2024, 1, day)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ((d(1), d(5)), (d(5), d(9)), True),
        ((d(1), d(5)), (d(6), d(9)), False),
        ((d(1), d(10)), (d(3), d(4)), True),
        ((d(3), d(4)), (d(1), d(10)), True),
        ((d(1), d(1)), (d(1), d(1)), True),
        ((d(1), d(1)), (d(2), d(2)), False),
        ((d(5), d(9)), (d(1), d(4)), False),
    ],
)
def test_overlaps(a, b, expected):
    assert overlaps(a, b) is expected


def test_invalid_range_raises():
    with pytest.raises(ValueError):
        overlaps((d(5), d(1)), (d(1), d(2)))
    with pytest.raises(ValueError):
        overlaps((d(1), d(2)), (d(5), d(1)))
    with pytest.raises(ValueError):
        merge_ranges([(d(1), d(2)), (d(9), d(3))])


def test_merge_empty_and_single():
    assert merge_ranges([]) == []
    assert merge_ranges([(d(1), d(2))]) == [(d(1), d(2))]


def test_merge_overlapping_unsorted():
    ranges = [(d(10), d(12)), (d(1), d(3)), (d(2), d(6))]
    assert merge_ranges(ranges) == [(d(1), d(6)), (d(10), d(12))]
    assert ranges == [(d(10), d(12)), (d(1), d(3)), (d(2), d(6))]  # input untouched


def test_merge_touching_ranges():
    assert merge_ranges([(d(1), d(3)), (d(4), d(6))]) == [(d(1), d(6))]


def test_merge_keeps_gaps():
    assert merge_ranges([(d(1), d(3)), (d(5), d(6))]) == [(d(1), d(3)), (d(5), d(6))]


def test_merge_contained_range():
    assert merge_ranges([(d(1), d(10)), (d(3), d(4))]) == [(d(1), d(10))]


def test_merge_chain():
    ranges = [(d(1), d(2)), (d(3), d(4)), (d(5), d(6)), (d(8), d(9))]
    assert merge_ranges(ranges) == [(d(1), d(6)), (d(8), d(9))]


def test_merge_across_month_boundary():
    jan = (date(2024, 1, 30), date(2024, 1, 31))
    feb = (date(2024, 2, 1), date(2024, 2, 2))
    assert merge_ranges([feb, jan]) == [(date(2024, 1, 30), date(2024, 2, 2))]
