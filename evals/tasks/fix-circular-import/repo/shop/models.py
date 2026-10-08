"""Product models."""

from shop.pricing import tax_for


class Product:
    def __init__(self, name, price, category="general"):
        self.name = name
        self.price = price
        self.category = category

    def price_with_tax(self):
        return round(self.price + tax_for(self), 2)
