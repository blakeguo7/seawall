import pytest

from cache import LRUCache


@pytest.mark.parametrize("capacity", [0, -1, 1.5, "3", None])
def test_invalid_capacity(capacity):
    with pytest.raises(ValueError):
        LRUCache(capacity)


def test_get_default():
    cache = LRUCache(2)
    assert cache.get("missing") is None
    assert cache.get("missing", 42) == 42


def test_get_refreshes_recency():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1  # a is now most recent
    cache.put("c", 3)  # evicts b
    assert "b" not in cache
    assert "a" in cache and "c" in cache


def test_update_does_not_evict_and_refreshes():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)
    assert len(cache) == 2
    assert cache.keys() == ["b", "a"]
    assert cache.get("a") == 10


def test_keys_order_lru_to_mru():
    cache = LRUCache(3)
    for key in "abc":
        cache.put(key, key)
    cache.get("a")
    assert cache.keys() == ["b", "c", "a"]


def test_contains_does_not_change_recency():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert "a" in cache
    cache.put("c", 3)  # a is still the least recently used
    assert "a" not in cache


def test_capacity_one():
    cache = LRUCache(1)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.keys() == ["b"]
    assert len(cache) == 1


def test_clear():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.clear()
    assert len(cache) == 0 and cache.keys() == []
    cache.put("b", 2)
    assert cache.get("b") == 2


def test_falsy_values_are_stored():
    cache = LRUCache(2)
    cache.put("zero", 0)
    cache.put("none", None)
    assert cache.get("zero", "d") == 0
    assert "none" in cache


def test_many_operations_stay_within_capacity():
    cache = LRUCache(5)
    for i in range(100):
        cache.put(i, i)
        assert len(cache) <= 5
    assert cache.keys() == [95, 96, 97, 98, 99]
