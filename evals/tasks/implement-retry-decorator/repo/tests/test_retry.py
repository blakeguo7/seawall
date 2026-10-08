from retry import retry


def test_succeeds_after_failures():
    calls = []

    @retry(times=3)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("no")
        return "ok"

    assert flaky() == "ok"
