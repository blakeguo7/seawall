from pager import page_count, paginate


def test_first_page():
    assert paginate([1, 2, 3, 4, 5], 1, 2) == [1, 2]


def test_last_partial_page():
    assert paginate([1, 2, 3, 4, 5], 3, 2) == [5]


def test_page_count_rounds_up():
    assert page_count(5, 2) == 3
