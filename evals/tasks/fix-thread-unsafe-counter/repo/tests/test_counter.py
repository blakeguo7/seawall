import threading

from counter import Counter


def test_threads_do_not_lose_increments():
    counter = Counter()

    def work():
        for _ in range(200):
            counter.increment()

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counter.value == 1600
