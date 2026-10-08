"""Receipts."""

from shop.cart import cart_total_cents, line_total_cents
from shop.money import format_cents


def render(lines, tax_rate=0.0):
    """One line per cart line ("2 x 2.675 = 5.89"), then "TOTAL 12.34"."""
    out = []
    for price, qty in lines:
        out.append(f"{qty} x {price} = {format_cents(line_total_cents(price, qty, tax_rate))}")
    out.append(f"TOTAL {format_cents(cart_total_cents(lines, tax_rate))}")
    return "\n".join(out)
