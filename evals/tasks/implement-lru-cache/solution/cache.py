"""A least-recently-used cache."""

from collections import OrderedDict


class LRUCache:
    """Keeps at most `capacity` items and drops the least recently used one when full.

    * LRUCache(capacity): capacity must be an int >= 1, otherwise raise ValueError.
    * get(key, default=None): the value, or default. A hit makes the key the most recently used.
    * put(key, value): stores the value and makes the key the most recently used. Updating an
      existing key never evicts anything. Inserting a new key into a full cache evicts the least
      recently used key first.
    * len(cache), `key in cache` (does not change recency).
    * keys(): the keys from least recently used to most recently used.
    * clear(): removes everything.
    """

    def __init__(self, capacity):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be an int >= 1")
        self._capacity = capacity
        self._data = OrderedDict()

    def get(self, key, default=None):
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key, value):
        if key in self._data:
            self._data.move_to_end(key)
        elif len(self._data) >= self._capacity:
            self._data.popitem(last=False)
        self._data[key] = value

    def keys(self):
        return list(self._data)

    def clear(self):
        self._data.clear()

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return key in self._data
