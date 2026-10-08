import bisect
import random

import pytest

from search import binary_search, insert_position


def test_empty():
    assert binary_search([], 1) == -1
    assert insert_position([], 1) == 0


@pytest.mark.parametrize("items", [[1], [1, 2], [1, 2, 3], list(range(10)), list(range(0, 100, 3))])
def test_finds_every_present_element(items):
    for index, value in enumerate(items):
        assert binary_search(items, value) == index


@pytest.mark.parametrize("items", [[1], [1, 3], [2, 4, 6], list(range(0, 50, 5))])
def test_absent_elements_give_minus_one(items):
    for value in [-5, min(items) - 1, max(items) + 1, 1000] + [v + 1 for v in items]:
        if value not in items:
            assert binary_search(items, value) == -1


def test_duplicates_return_a_valid_index():
    items = [1, 2, 2, 2, 3]
    assert items[binary_search(items, 2)] == 2


def test_insert_position_matches_bisect_left_on_random_data():
    rng = random.Random(7)
    for _ in range(300):
        items = sorted(rng.randint(0, 20) for _ in range(rng.randint(0, 12)))
        target = rng.randint(-2, 22)
        assert insert_position(items, target) == bisect.bisect_left(items, target), (items, target)


def test_binary_search_agrees_with_membership_on_random_data():
    rng = random.Random(11)
    for _ in range(300):
        items = sorted(rng.sample(range(40), rng.randint(0, 15)))
        target = rng.randint(-1, 41)
        found = binary_search(items, target)
        if target in items:
            assert items[found] == target, (items, target)
        else:
            assert found == -1, (items, target)


def test_insert_position_past_the_end():
    assert insert_position([1, 2, 3], 4) == 3
    assert insert_position([1, 2, 3], 0) == 0
