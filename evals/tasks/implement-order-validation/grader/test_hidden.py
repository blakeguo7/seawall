import pytest

from orders import STATUSES, InvalidTransition, Order


def make(**overrides):
    values = {"id": "o1", "items": [("sku1", 2, 3.5)], "status": "new"}
    values.update(overrides)
    return Order(**values)


def test_valid_order():
    order = make()
    assert order.total() == 7.0 and order.status == "new"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"id": ""}, "id must be a non-empty string"),
        ({"id": 5}, "id must be a non-empty string"),
        ({"items": []}, "order has no items"),
        ({"items": [("", 1, 1.0)]}, "item 1: sku must be a non-empty string"),
        ({"items": [("a", 1, 1.0), (None, 1, 1.0)]}, "item 2: sku must be a non-empty string"),
        ({"items": [("a", 0, 1.0)]}, "item 1: quantity must be a positive integer"),
        ({"items": [("a", -2, 1.0)]}, "item 1: quantity must be a positive integer"),
        ({"items": [("a", 1.5, 1.0)]}, "item 1: quantity must be a positive integer"),
        ({"items": [("a", True, 1.0)]}, "item 1: quantity must be a positive integer"),
        ({"items": [("a", 1, 1.0), ("b", 1, 1.0), ("c", 1, -0.01)]}, "item 3: unit_price must be a non-negative number"),
        ({"items": [("a", 1, "5")]}, "item 1: unit_price must be a non-negative number"),
        ({"items": [("a", 1, True)]}, "item 1: unit_price must be a non-negative number"),
        ({"status": "refunded"}, "unknown status: 'refunded'"),
    ],
)
def test_validation_messages(overrides, message):
    with pytest.raises(ValueError) as excinfo:
        make(**overrides)
    assert str(excinfo.value) == message


def test_first_failure_wins():
    with pytest.raises(ValueError) as excinfo:
        make(id="", items=[], status="nope")
    assert str(excinfo.value) == "id must be a non-empty string"
    with pytest.raises(ValueError) as excinfo:
        make(items=[("a", 0, -1)], status="nope")
    assert str(excinfo.value) == "item 1: quantity must be a positive integer"


def test_zero_price_is_allowed():
    assert make(items=[("free", 1, 0)]).total() == 0


def test_full_lifecycle():
    order = make()
    assert order.transition("paid") is order
    assert order.status == "paid"
    assert order.transition("shipped").status == "shipped"


@pytest.mark.parametrize(
    ("start", "target"),
    [("new", "new"), ("new", "shipped"), ("paid", "new"), ("paid", "paid"), ("shipped", "paid"), ("shipped", "shipped")],
)
def test_invalid_transitions(start, target):
    order = make(status=start)
    with pytest.raises(InvalidTransition) as excinfo:
        order.transition(target)
    assert str(excinfo.value) == f"cannot go from '{start}' to '{target}'"
    assert order.status == start


def test_unknown_target_status_is_a_plain_value_error():
    order = make()
    with pytest.raises(ValueError) as excinfo:
        order.transition("lost")
    assert not isinstance(excinfo.value, InvalidTransition)
    assert str(excinfo.value) == "unknown status: 'lost'"


def test_constants():
    assert STATUSES == ("new", "paid", "shipped")
    assert issubclass(InvalidTransition, ValueError)
