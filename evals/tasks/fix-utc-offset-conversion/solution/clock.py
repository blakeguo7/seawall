"""Convert UTC timestamps to a fixed-offset local time."""

from datetime import timedelta, timezone


def to_local(ts_utc, offset_hours):
    """Convert an aware UTC datetime to a datetime with the given UTC offset.

    * offset_hours may be fractional: 5.5 is UTC+05:30, -3.5 is UTC-03:30
    * the result is timezone-aware, carries the requested offset (tzinfo.utcoffset), and denotes
      the same instant as the input
    * a naive datetime (no tzinfo) is rejected with ValueError, because its zone is unknown
    * an aware datetime in another zone is fine: it is converted properly
    * offsets outside -12 .. +14 hours raise ValueError
    """
    if ts_utc.tzinfo is None or ts_utc.utcoffset() is None:
        raise ValueError("naive datetime: the time zone is unknown")
    if not -12 <= offset_hours <= 14:
        raise ValueError(f"offset out of range: {offset_hours}")
    return ts_utc.astimezone(timezone(timedelta(hours=offset_hours)))


def format_local(ts_utc, offset_hours):
    """Format as "YYYY-MM-DD HH:MM +HH:MM", for example "2024-03-10 15:30 +05:30".

    The sign is always shown ("+00:00" for UTC, "-03:30" for negative offsets).
    """
    local = to_local(ts_utc, offset_hours)
    minutes = round(offset_hours * 60)
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"{local:%Y-%m-%d %H:%M} {sign}{hours:02d}:{rest:02d}"
