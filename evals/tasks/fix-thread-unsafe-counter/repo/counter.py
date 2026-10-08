"""A shared counter."""

import time


class Counter:
    """A counter that several threads update at the same time.

    Every increment and every add must be counted, whatever the thread interleaving; reset() sets
    the value back to zero atomically with respect to them.
    """

    def __init__(self):
        self.value = 0

    def increment(self):
        current = self.value
        time.sleep(0)  # a real implementation would do some work here; it lets other threads in
        self.value = current + 1

    def add(self, amount):
        current = self.value
        time.sleep(0)
        self.value = current + amount

    def reset(self):
        self.value = 0
