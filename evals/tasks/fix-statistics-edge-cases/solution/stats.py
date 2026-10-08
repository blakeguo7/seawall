"""Descriptive statistics."""


def mean(values):
    """Arithmetic mean. Returns None for an empty sequence."""
    if not values:
        return None
    return sum(values) / len(values)


def median(values):
    """Median. None for an empty sequence; for an even count, the average of the two middle values."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def variance(values):
    """Population variance (divide by n). None for an empty sequence."""
    if not values:
        return None
    m = mean(values)
    return sum((v - m) ** 2 for v in values) / len(values)
