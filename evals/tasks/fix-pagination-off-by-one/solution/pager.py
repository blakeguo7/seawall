"""Pagination helpers."""


def paginate(items, page, per_page):
    """Return the items on a 1-based page.

    paginate([1, 2, 3, 4, 5], 1, 2) -> [1, 2]
    paginate([1, 2, 3, 4, 5], 3, 2) -> [5]

    A page outside the valid range (0, negative, or past the end) returns an empty list.
    """
    if page < 1:
        return []
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end]


def page_count(total_items, per_page):
    """Number of pages needed to show total_items. Zero items need zero pages."""
    return -(-total_items // per_page)
