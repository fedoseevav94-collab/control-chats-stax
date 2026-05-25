from datetime import datetime, time
from zoneinfo import ZoneInfo

from bot.worktime import add_working_minutes, is_work_time, next_work_time


TZ = ZoneInfo("Europe/Moscow")
START = time(9, 30)
END = time(19, 0)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=TZ)


def test_is_work_time_boundaries() -> None:
    assert not is_work_time(dt("2026-05-24T09:29:59"), START, END, TZ)
    assert is_work_time(dt("2026-05-24T09:30:00"), START, END, TZ)
    assert is_work_time(dt("2026-05-24T18:59:59"), START, END, TZ)
    assert not is_work_time(dt("2026-05-24T19:00:00"), START, END, TZ)


def test_next_work_time_moves_after_hours_to_next_morning() -> None:
    assert next_work_time(dt("2026-05-24T19:10:00"), START, END, TZ) == dt("2026-05-25T09:30:00")


def test_twenty_minute_reminder_at_1820_stays_same_day() -> None:
    assert add_working_minutes(dt("2026-05-24T18:20:00"), 20, START, END, TZ) == dt("2026-05-24T18:40:00")


def test_due_after_window_starts_at_next_morning() -> None:
    assert add_working_minutes(dt("2026-05-24T18:50:00"), 20, START, END, TZ) == dt("2026-05-25T09:30:00")


def test_sixty_working_minutes_crosses_evening_boundary() -> None:
    assert add_working_minutes(dt("2026-05-24T18:30:00"), 60, START, END, TZ) == dt("2026-05-25T09:30:00")
