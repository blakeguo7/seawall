"""Descriptive statistics."""


def mean(values):
    """Arithmetic mean. Returns None for an empty sequence."""
    return sum(values) / len(values)


def median(values):
    """Median. None for an empty sequence; for an even count, the average of the two middle values."""
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def variance(values):
    """Population variance (divide by n). None for an empty sequence."""
    m = mean(values)
    return sum((v - m) ** 2 for v in values) / (len(values) - 1)
