import pytest

from orders import Order


def test_total():
    assert Order("o1", [("a", 2, 1.5)]).total() == 3.0


def test_rejects_empty_items():
    with pytest.raises(ValueError):
        Order("o1", [])
