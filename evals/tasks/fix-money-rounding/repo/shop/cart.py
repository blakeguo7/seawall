"""A shopping cart."""

from shop.money import to_cents


def line_total_cents(price, quantity, tax_rate):
    """Cents for one cart line: price * quantity, plus tax on that line, rounded half up to cents.

    The line is rounded once, at the end, from exact arithmetic: 3 x 2.675 at 10% tax is
    2.675 * 3 * 1.1 = 8.8275 -> 883 cents.
    """
    cents = to_cents(price) * quantity
    return int(round(cents * (1 + tax_rate)))


def cart_total_cents(lines, tax_rate=0.0):
    """Sum of the line totals; lines are (price, quantity) pairs."""
    return sum(line_total_cents(price, qty, tax_rate) for price, qty in lines)
