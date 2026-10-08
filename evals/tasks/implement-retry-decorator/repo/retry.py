"""A retry decorator."""

import time


def retry(times=3, exceptions=(Exception,), delay=0.0, sleep=time.sleep):
    """Decorator that calls the function again when it raises.

    * the function is called at most `times` times in total (times=3 means one call and up to two
      retries); `times` must be an int >= 1, otherwise ValueError is raised when the decorator is
      created
    * only exceptions that are instances of `exceptions` (an exception class or a tuple of them)
      trigger a retry; any other exception propagates immediately
    * after the last failed attempt the last exception is raised unchanged
    * between attempts it calls sleep(delay) - not after the final attempt and not at all when
      delay is 0
    * the return value of the first successful call is returned
    * the wrapper keeps the wrapped function's __name__ and __doc__ (use functools.wraps)
    * the wrapper has an attribute `attempts` holding the number of calls made by the most recent
      invocation of the wrapper
    """
    raise NotImplementedError
