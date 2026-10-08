"""Money helpers. Amounts are handled in whole cents."""


def to_cents(amount):
    """Convert a price such as 2.675 or "2.675" to whole cents, rounding half up (2.675 -> 268)."""
    return int(round(float(amount) * 100))


def format_cents(cents):
    """Format whole cents as a string with two decimals: 268 -> "2.68"; negatives keep the sign."""
    return f"{cents / 100:.2f}"
