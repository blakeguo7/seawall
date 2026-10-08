from shop.models import Product


def test_price_with_tax():
    assert Product("lamp", 10.0).price_with_tax() == 12.0
