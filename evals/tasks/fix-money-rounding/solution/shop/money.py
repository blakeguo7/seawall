"""Money helpers. Amounts are handled in whole cents."""

from decimal import ROUND_HALF_UP, Decimal


def to_cents(amount):
    """Convert a price such as 2.675 or "2.675" to whole cents, rounding half up (2.675 -> 268)."""
    exact = Decimal(str(amount)) * 100
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def format_cents(cents):
    """Format whole cents as a string with two decimals: 268 -> "2.68"; negatives keep the sign."""
    sign = "-" if cents < 0 else ""
    whole, rest = divmod(abs(cents), 100)
    return f"{sign}{whole}.{rest:02d}"
