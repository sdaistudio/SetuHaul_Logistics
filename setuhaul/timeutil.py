from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromisoformat(ts)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(IST).isoformat(timespec="seconds")


def hhmm(dt: datetime | None) -> str:
    return "" if dt is None else dt.astimezone(IST).strftime("%H:%M")


def at(day: datetime, hhmm_str: str) -> datetime:
    h, m = (int(x) for x in hhmm_str.split(":"))
    return day.astimezone(IST).replace(hour=h, minute=m, second=0, microsecond=0)


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)


def diff_min(a: datetime, b: datetime) -> int:
    """a - b in whole minutes."""
    return int((a - b).total_seconds() // 60)
