"""Searching sorted lists."""


def binary_search(items, target):
    """Index of target in the ascending list items, or -1 if it is not there.

    If target occurs several times, any of its indexes is acceptable.
    """
    lo, hi = 0, len(items) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if items[mid] == target:
            return mid
        if items[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


def insert_position(items, target):
    """Where to insert target in the ascending list items to keep it sorted.

    When target is already present, return the index of its first occurrence (so inserting there
    puts the new value before the existing equal ones).
    """
    lo, hi = 0, len(items) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if items[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo
