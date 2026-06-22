from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from environs import Env


def _parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":", maxsplit=1)
    return time(hour=int(hours), minute=int(minutes))


@dataclass(frozen=True)
class Settings:
    bot_token: str
    timezone_name: str
    timezone: ZoneInfo
    workday_start: time
    workday_end: time
    daily_report_time: time
    reminder_interval_minutes: int
    direct_message_after_minutes: int
    leader_username: str
    direct_manager_escalations: dict[str, str]
    escalate_after_reminders: int
    max_group_reminders_if_dm_unreachable: int
    fine_amount_rubles: int
    seen_delay_minutes: int
    reason_request_before_fine_minutes: int
    smart_reply_delay_minutes: int
    enable_reason_before_fine: bool
    enable_seen_button: bool
    enable_smart_reply_detection: bool
    enable_warning_decision: bool
    bot_update_date: str
    bot_update_time: str
    database_path: Path
    scheduler_tick_seconds: int = 30
    scheduler_startup_grace_seconds: int = 45


def _parse_direct_manager_escalations(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_pair in value.split(","):
        pair = raw_pair.strip()
        if not pair or ":" not in pair:
            continue
        employee, manager = pair.split(":", maxsplit=1)
        employee_key = employee.strip().removeprefix("@").casefold()
        manager_username = manager.strip().removeprefix("@").casefold()
        if employee_key and manager_username:
            result[employee_key] = manager_username
    return result


def load_settings() -> Settings:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN не найден")

    env = Env()
    env.read_env()

    timezone_name = env.str("TIMEZONE", "Europe/Moscow")
    return Settings(
        bot_token=bot_token,
        timezone_name=timezone_name,
        timezone=ZoneInfo(timezone_name),
        workday_start=_parse_hhmm(env.str("WORKDAY_START", "09:30")),
        workday_end=_parse_hhmm(env.str("WORKDAY_END", "19:00")),
        daily_report_time=_parse_hhmm(env.str("DAILY_REPORT_TIME", "09:30")),
        reminder_interval_minutes=env.int("REMINDER_INTERVAL_MINUTES", 10),
        direct_message_after_minutes=env.int("DIRECT_MESSAGE_AFTER_MINUTES", 60),
        leader_username=env.str("LEADER_USERNAME", "Fedos_AV").removeprefix("@").lower(),
        direct_manager_escalations=_parse_direct_manager_escalations(
            env.str("DIRECT_MANAGER_ESCALATIONS", "k_kram1:dislavsergeevich,полина:dislavsergeevich")
        ),
        escalate_after_reminders=env.int("ESCALATE_AFTER_REMINDERS", 3),
        max_group_reminders_if_dm_unreachable=env.int("MAX_GROUP_REMINDERS_IF_DM_UNREACHABLE", 3),
        fine_amount_rubles=env.int("FINE_AMOUNT_RUBLES", 500),
        seen_delay_minutes=env.int("SEEN_DELAY_MINUTES", 30),
        reason_request_before_fine_minutes=env.int("REASON_REQUEST_BEFORE_FINE_MINUTES", 15),
        smart_reply_delay_minutes=env.int("SMART_REPLY_DELAY_MINUTES", 30),
        enable_reason_before_fine=env.bool("ENABLE_REASON_BEFORE_FINE", True),
        enable_seen_button=env.bool("ENABLE_SEEN_BUTTON", True),
        enable_smart_reply_detection=env.bool("ENABLE_SMART_REPLY_DETECTION", True),
        enable_warning_decision=env.bool("ENABLE_WARNING_DECISION", True),
        bot_update_date=env.str("BOT_UPDATE_DATE", "28.05.2026"),
        bot_update_time=env.str("BOT_UPDATE_TIME", "12:43"),
        database_path=Path(env.str("DATABASE_PATH", "bot.sqlite3")),
        scheduler_tick_seconds=env.int("SCHEDULER_TICK_SECONDS", 30),
        scheduler_startup_grace_seconds=env.int("SCHEDULER_STARTUP_GRACE_SECONDS", 45),
    )
