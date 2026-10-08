"""Inclusive date ranges, as (start, end) tuples of datetime.date."""


def overlaps(a, b):
    """True if the two ranges share at least one day.

    Both ranges are inclusive: (Jan 1, Jan 5) and (Jan 5, Jan 9) overlap on Jan 5.
    Raises ValueError if either range ends before it starts.
    """
    raise NotImplementedError


def merge_ranges(ranges):
    """Combine ranges that overlap or touch into the smallest list of ranges.

    * ranges that overlap are merged
    * ranges that touch are merged too: (Jan 1, Jan 3) and (Jan 4, Jan 6) become (Jan 1, Jan 6),
      because no day lies between them
    * the result is sorted by start date; the input is not modified and may be unsorted
    * an empty list gives an empty list
    * raises ValueError if any range ends before it starts
    """
    raise NotImplementedError
