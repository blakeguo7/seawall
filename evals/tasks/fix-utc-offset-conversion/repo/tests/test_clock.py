from datetime import datetime, timezone

from clock import format_local


def test_india():
    ts = datetime(2024, 3, 10, 10, 0, tzinfo=timezone.utc)
    assert format_local(ts, 5.5) == "2024-03-10 15:30 +05:30"
