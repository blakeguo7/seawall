from pricing import compute_total


def test_compute_total_without_tax():
    assert compute_total([("pen", 1.5, 2), ("book", 10, 1)]) == 13.0


def test_compute_total_with_tax():
    assert compute_total([("pen", 10, 1)], tax_rate=0.5) == 15.0
