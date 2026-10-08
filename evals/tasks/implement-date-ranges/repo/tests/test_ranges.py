from datetime import date

from ranges import merge_ranges, overlaps


def test_overlap_on_shared_day():
    assert overlaps((date(2024, 1, 1), date(2024, 1, 5)), (date(2024, 1, 5), date(2024, 1, 9)))


def test_merge_two():
    result = merge_ranges([(date(2024, 1, 1), date(2024, 1, 3)), (date(2024, 1, 2), date(2024, 1, 6))])
    assert result == [(date(2024, 1, 1), date(2024, 1, 6))]
