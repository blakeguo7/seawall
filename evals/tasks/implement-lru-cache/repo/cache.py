"""A least-recently-used cache."""


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
        raise NotImplementedError

    def get(self, key, default=None):
        raise NotImplementedError

    def put(self, key, value):
        raise NotImplementedError

    def keys(self):
        raise NotImplementedError

    def clear(self):
        raise NotImplementedError

    def __len__(self):
        raise NotImplementedError

    def __contains__(self, key):
        raise NotImplementedError
