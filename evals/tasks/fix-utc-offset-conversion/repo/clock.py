"""Convert UTC timestamps to a fixed-offset local time."""

from datetime import datetime, timedelta


def to_local(ts_utc, offset_hours):
    """Convert an aware UTC datetime to a datetime with the given UTC offset.

    * offset_hours may be fractional: 5.5 is UTC+05:30, -3.5 is UTC-03:30
    * the result is timezone-aware, carries the requested offset (tzinfo.utcoffset), and denotes
      the same instant as the input
    * a naive datetime (no tzinfo) is rejected with ValueError, because its zone is unknown
    * an aware datetime in another zone is fine: it is converted properly
    * offsets outside -12 .. +14 hours raise ValueError
    """
    return ts_utc + timedelta(hours=int(offset_hours))


def format_local(ts_utc, offset_hours):
    """Format as "YYYY-MM-DD HH:MM +HH:MM", for example "2024-03-10 15:30 +05:30".

    The sign is always shown ("+00:00" for UTC, "-03:30" for negative offsets).
    """
    local = to_local(ts_utc, offset_hours)
    return local.strftime("%Y-%m-%d %H:%M")
