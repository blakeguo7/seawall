from stats import mean, median, variance


def test_mean():
    assert mean([1, 2, 3]) == 2


def test_median_even():
    assert median([1, 2, 3, 4]) == 2.5


def test_variance():
    assert variance([1, 2, 3, 4]) == 1.25
