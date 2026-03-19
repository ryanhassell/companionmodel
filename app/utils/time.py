from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo


def parse_clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(hour=int(hour), minute=int(minute))


def in_time_range(now_local: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now_local <= end
    return now_local >= start or now_local <= end


def now_in_timezone(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def utc_now() -> datetime:
    return datetime.now(UTC)


def local_today(tz_name: str) -> date:
    return now_in_timezone(tz_name).date()
