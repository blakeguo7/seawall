"""Reports."""

import pricing


def revenue(orders):
    """Sum of the totals of several orders (each a list of items), without tax."""
    return round(sum(pricing.calc_total(items) for items in orders), 2)
