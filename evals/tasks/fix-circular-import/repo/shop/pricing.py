"""Tax rules."""

from shop.models import Product

RATES = {"general": 0.20, "food": 0.05, "books": 0.0}


def tax_for(product):
    """The tax on a product: its price times the rate of its category (20% for unknown ones)."""
    if not isinstance(product, Product):
        raise TypeError("tax_for needs a Product")
    return product.price * RATES.get(product.category, 0.20)
