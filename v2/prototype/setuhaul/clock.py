"""Time helpers. The snapshot has no single 'now', so every function takes as_of."""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def ts(s):
    """ISO text -> aware datetime (None stays None)."""
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=IST)


def iso(d):
    return None if d is None else d.astimezone(IST).isoformat(timespec="seconds")


def hhmm(d):
    return d.astimezone(IST).strftime("%H:%M")


def ampm(d):
    """12:30 -> '12:30 PM' (drivers read 12-hour time)."""
    return d.astimezone(IST).strftime("%I:%M %p").lstrip("0")


def add(d, minutes):
    return d + timedelta(minutes=minutes)


def minutes_between(a, b):
    return int(round((b - a).total_seconds() / 60))


def at(as_of, hh, mm=0):
    """Same day as as_of, given clock time."""
    return as_of.astimezone(IST).replace(hour=hh, minute=mm, second=0, microsecond=0)
