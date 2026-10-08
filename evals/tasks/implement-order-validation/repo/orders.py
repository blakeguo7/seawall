"""Orders."""

from dataclasses import dataclass, field

STATUSES = ("new", "paid", "shipped")


class InvalidTransition(ValueError):
    """An order was asked to move to a status it cannot move to."""


@dataclass
class Order:
    """An order with an id, items and a status.

    items is a list of (sku, quantity, unit_price) tuples. Creating an Order validates it, raising
    ValueError with a message that names the problem:

    * id must be a non-empty string -> "id must be a non-empty string"
    * items must not be empty -> "order has no items"
    * sku must be a non-empty string -> "item 2: sku must be a non-empty string" (items are
      numbered from 1)
    * quantity must be an int greater than zero (bool does not count) -> "item 1: quantity must
      be a positive integer"
    * unit_price must be an int or float, not negative (bool does not count) -> "item 3: unit_price
      must be a non-negative number"
    * status must be one of STATUSES -> "unknown status: 'refunded'"
    The checks run in the order listed, and the first failure is the one reported.

    transition(new_status) moves the order along new -> paid -> shipped, one step at a time:
    * a valid step changes self.status and returns self
    * moving to the status it already has, going backwards, or skipping a step raises
      InvalidTransition with the message "cannot go from 'new' to 'shipped'" (using the actual
      statuses)
    * an unknown target status raises ValueError with "unknown status: 'x'" (not InvalidTransition)
    """

    id: str
    items: list = field(default_factory=list)
    status: str = "new"

    def total(self):
        return round(sum(qty * price for _sku, qty, price in self.items), 2)

    def transition(self, new_status):
        raise NotImplementedError
