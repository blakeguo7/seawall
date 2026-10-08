"""Inclusive date ranges, as (start, end) tuples of datetime.date."""

from datetime import timedelta


def _check(rng):
    start, end = rng
    if end < start:
        raise ValueError(f"range ends before it starts: {rng!r}")


def overlaps(a, b):
    """True if the two ranges share at least one day.

    Both ranges are inclusive: (Jan 1, Jan 5) and (Jan 5, Jan 9) overlap on Jan 5.
    Raises ValueError if either range ends before it starts.
    """
    _check(a)
    _check(b)
    return a[0] <= b[1] and b[0] <= a[1]


def merge_ranges(ranges):
    """Combine ranges that overlap or touch into the smallest list of ranges.

    * ranges that overlap are merged
    * ranges that touch are merged too: (Jan 1, Jan 3) and (Jan 4, Jan 6) become (Jan 1, Jan 6),
      because no day lies between them
    * the result is sorted by start date; the input is not modified and may be unsorted
    * an empty list gives an empty list
    * raises ValueError if any range ends before it starts
    """
    for rng in ranges:
        _check(rng)
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
