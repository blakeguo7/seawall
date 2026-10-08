"""Price calculations."""


def compute_total(items, tax_rate=0.0):
    """Total price of (name, unit_price, quantity) items, with tax applied to the sum."""
    subtotal = sum(price * qty for _name, price, qty in items)
    return round(subtotal * (1 + tax_rate), 2)
