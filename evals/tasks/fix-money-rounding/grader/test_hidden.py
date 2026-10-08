import pytest

from shop.cart import cart_total_cents, line_total_cents
from shop.money import format_cents, to_cents
from shop.receipt import render


@pytest.mark.parametrize(
    ("amount", "cents"),
    [("2.675", 268), (2.675, 268), ("0.005", 1), ("1.005", 101), ("19.99", 1999), ("0", 0), ("0.004", 0), ("1234.565", 123457)],
)
def test_to_cents_rounds_half_up(amount, cents):
    assert to_cents(amount) == cents


@pytest.mark.parametrize(("cents", "text"), [(268, "2.68"), (5, "0.05"), (0, "0.00"), (100000, "1000.00"), (-250, "-2.50"), (-5, "-0.05")])
def test_format_cents(cents, text):
    assert format_cents(cents) == text


@pytest.mark.parametrize(
    ("price", "qty", "tax", "expected"),
    [
        ("2.675", 1, 0.0, 268),
        ("2.675", 3, 0.1, 883),
        ("0.10", 3, 0.0, 30),
        ("19.99", 3, 0.2, 7196),
        ("1.005", 1, 0.0, 101),
        ("0.01", 1, 0.5, 2),
        ("0.05", 1, 0.1, 6),
        ("10.00", 1, 0.075, 1075),
        ("33.33", 3, 0.0825, 10824),
    ],
)
def test_line_total(price, qty, tax, expected):
    assert line_total_cents(price, qty, tax) == expected


def test_cart_total_sums_rounded_lines():
    lines = [("0.05", 1), ("0.05", 1), ("0.05", 1)]
    assert cart_total_cents(lines, 0.1) == 3 * 6  # each line is rounded on its own
    assert cart_total_cents([], 0.2) == 0


def test_receipt_text():
    lines = [("2.675", 2), ("0.10", 3)]
    assert render(lines, 0.0) == "2 x 2.675 = 5.35\n3 x 0.10 = 0.30\nTOTAL 5.65"
    assert render([("2.675", 3)], 0.1) == "3 x 2.675 = 8.83\nTOTAL 8.83"
