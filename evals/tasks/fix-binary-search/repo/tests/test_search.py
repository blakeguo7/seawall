from search import binary_search


def test_finds_last_element():
    assert binary_search([1, 3, 5, 7], 7) == 3
