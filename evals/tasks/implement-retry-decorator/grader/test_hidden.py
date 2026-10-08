import pytest

from retry import retry


class Boom(Exception):
    pass


def make_flaky(failures, exc=Boom):
    state = {"calls": 0}

    def flaky(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] <= failures:
            raise exc(f"failure {state['calls']}")
        return ("ok", args, kwargs)

    return flaky, state


def test_returns_the_value_and_passes_arguments():
    flaky, _ = make_flaky(0)
    assert retry()(flaky)(1, x=2) == ("ok", (1,), {"x": 2})


def test_retries_until_success():
    flaky, state = make_flaky(2)
    wrapped = retry(times=3)(flaky)
    assert wrapped()[0] == "ok"
    assert state["calls"] == 3
    assert wrapped.attempts == 3


def test_raises_the_last_exception_after_the_last_attempt():
    flaky, state = make_flaky(10)
    wrapped = retry(times=3)(flaky)
    with pytest.raises(Boom, match="failure 3"):
        wrapped()
    assert state["calls"] == 3
    assert wrapped.attempts == 3


def test_other_exceptions_propagate_immediately():
    flaky, state = make_flaky(5, exc=KeyError)
    wrapped = retry(times=5, exceptions=(Boom,))(flaky)
    with pytest.raises(KeyError):
        wrapped()
    assert state["calls"] == 1


def test_exceptions_may_be_a_single_class():
    flaky, _ = make_flaky(1)
    assert retry(times=2, exceptions=Boom)(flaky)()[0] == "ok"


def test_sleeps_between_attempts_only():
    naps = []
    flaky, _ = make_flaky(10)
    wrapped = retry(times=4, delay=0.5, sleep=naps.append)(flaky)
    with pytest.raises(Boom):
        wrapped()
    assert naps == [0.5, 0.5, 0.5]  # 4 attempts, 3 gaps


def test_no_sleep_when_delay_is_zero_or_first_try_works():
    naps = []
    flaky, _ = make_flaky(2)
    retry(times=3, delay=0, sleep=naps.append)(flaky)()
    ok, _ = make_flaky(0)
    retry(times=3, delay=1.0, sleep=naps.append)(ok)()
    assert naps == []


@pytest.mark.parametrize("times", [0, -1, 1.5, "3", None])
def test_invalid_times(times):
    with pytest.raises(ValueError):
        retry(times=times)


def test_times_one_means_no_retry():
    flaky, state = make_flaky(1)
    with pytest.raises(Boom):
        retry(times=1)(flaky)()
    assert state["calls"] == 1


def test_metadata_is_preserved():
    @retry()
    def documented():
        """The docs."""

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "The docs."


def test_attempts_reflect_the_most_recent_call():
    flaky, state = make_flaky(1)
    wrapped = retry(times=3)(flaky)
    wrapped()
    assert wrapped.attempts == 2
    wrapped()
    assert wrapped.attempts == 1
