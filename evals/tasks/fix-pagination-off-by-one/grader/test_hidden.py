import pytest

from pager import page_count, paginate

ITEMS = list(range(1, 11))


@pytest.mark.parametrize(
    ("page", "per_page", "expected"),
    [(1, 3, [1, 2, 3]), (2, 3, [4, 5, 6]), (4, 3, [10]), (5, 3, []), (0, 3, []), (-1, 3, []), (1, 10, ITEMS), (1, 20, ITEMS)],
)
def test_paginate(page, per_page, expected):
    assert paginate(ITEMS, page, per_page) == expected


def test_paginate_empty_list():
    assert paginate([], 1, 5) == []


@pytest.mark.parametrize(("total", "per_page", "expected"), [(0, 5, 0), (1, 5, 1), (5, 5, 1), (6, 5, 2), (10, 5, 2), (11, 5, 3)])
def test_page_count(total, per_page, expected):
    assert page_count(total, per_page) == expected
