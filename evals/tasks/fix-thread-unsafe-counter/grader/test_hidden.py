import threading

from counter import Counter


def run_threads(target, n=8):
    threads = [threading.Thread(target=target) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_increments():
    counter = Counter()
    run_threads(lambda: [counter.increment() for _ in range(300)])
    assert counter.value == 2400


def test_adds():
    counter = Counter()
    run_threads(lambda: [counter.add(3) for _ in range(300)])
    assert counter.value == 7200


def test_mixed_operations():
    counter = Counter()

    def work():
        for _ in range(200):
            counter.increment()
            counter.add(2)

    run_threads(work)
    assert counter.value == 8 * 200 * 3


def test_reset_and_interface():
    counter = Counter()
    counter.increment()
    counter.add(4)
    assert counter.value == 5
    counter.reset()
    assert counter.value == 0
    assert hasattr(counter, "value")


def test_it_uses_a_lock():
    import inspect

    import counter as module

    source = inspect.getsource(module)
    assert "Lock" in source or "RLock" in source
