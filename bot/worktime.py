from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def ensure_tz(dt: datetime, timezone: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone)
    return dt.astimezone(timezone)


def workday_bounds(dt: datetime, start: time, end: time) -> tuple[datetime, datetime]:
    return (
        datetime.combine(dt.date(), start, tzinfo=dt.tzinfo),
        datetime.combine(dt.date(), end, tzinfo=dt.tzinfo),
    )


def is_work_time(dt: datetime, start: time, end: time, timezone: ZoneInfo) -> bool:
    local_dt = ensure_tz(dt, timezone)
    day_start, day_end = workday_bounds(local_dt, start, end)
    return day_start <= local_dt < day_end


def next_work_time(dt: datetime, start: time, end: time, timezone: ZoneInfo) -> datetime:
    local_dt = ensure_tz(dt, timezone)
    day_start, day_end = workday_bounds(local_dt, start, end)

    if local_dt < day_start:
        return day_start
    if local_dt < day_end:
        return local_dt

    tomorrow = local_dt.date() + timedelta(days=1)
    return datetime.combine(tomorrow, start, tzinfo=timezone)


def add_working_minutes(
    dt: datetime,
    minutes: int,
    start: time,
    end: time,
    timezone: ZoneInfo,
) -> datetime:
    local_dt = ensure_tz(dt, timezone)
    candidate = local_dt + timedelta(minutes=max(minutes, 0))
    return next_work_time(candidate, start, end, timezone)
