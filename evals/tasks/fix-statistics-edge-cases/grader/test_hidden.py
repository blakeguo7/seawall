import pytest

from stats import mean, median, variance


def test_empty_inputs_return_none():
    assert mean([]) is None
    assert median([]) is None
    assert variance([]) is None


def test_mean_of_floats_and_negatives():
    assert mean([-1.5, 1.5, 3.0]) == pytest.approx(1.0)


@pytest.mark.parametrize(("values", "expected"), [([5], 5), ([3, 1, 2], 2), ([4, 1, 3, 2], 2.5), ([1, 1, 2, 2], 1.5), ([10, -10], 0)])
def test_median(values, expected):
    assert median(values) == expected


def test_median_does_not_modify_input():
    values = [3, 1, 2]
    median(values)
    assert values == [3, 1, 2]


@pytest.mark.parametrize(("values", "expected"), [([5], 0), ([1, 1, 1], 0), ([1, 2, 3, 4], 1.25), ([2, 4, 4, 4, 5, 5, 7, 9], 4.0)])
def test_variance_is_population_variance(values, expected):
    assert variance(values) == pytest.approx(expected)
