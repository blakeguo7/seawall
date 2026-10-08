from shop.cart import line_total_cents


def test_half_cent_rounds_up():
    assert line_total_cents("2.675", 1, 0.0) == 268
