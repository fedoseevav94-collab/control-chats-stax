from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import aiosqlite


def dt_to_db(value: datetime) -> str:
    return value.isoformat()


def dt_from_db(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class PendingWait:
    id: int
    chat_id: int
    chat_title: str | None
    username: str
    display_name: str | None
    user_id: int | None
    source_message_id: int
    source_message_link: str | None
    source_quote: str
    created_at: datetime
    next_reminder_at: datetime
    direct_message_due_at: datetime
    direct_message_attempted_at: datetime | None
    direct_message_sent_at: datetime | None
    last_reminder_message_id: int | None
    group_reminders_stopped_at: datetime | None
    reminder_count: int
    seen_at: datetime | None
    seen_by_user_id: int | None
    last_intermediate_at: datetime | None
    reason_requested_at: datetime | None
    reason_due_at: datetime | None
    delay_reason: str | None
    delay_reason_at: datetime | None
    leader_request_sent_at: datetime | None
    leader_request_chat_id: int | None
    leader_request_message_id: int | None
    status: str


@dataclass(frozen=True)
class DailyReportItem:
    id: int
    chat_id: int
    chat_title: str | None
    username: str
    display_name: str | None
    user_id: int | None
    source_message_link: str | None
    source_quote: str
    created_at: datetime
    reminder_count: int
    direct_message_sent_at: datetime | None
    group_reminders_stopped_at: datetime | None


@dataclass(frozen=True)
class FineReportItem:
    username: str
    display_name: str | None
    user_id: int | None
    fine_count: int
    total_amount: int


@dataclass(frozen=True)
class FineReportDetail:
    id: int
    username: str
    display_name: str | None
    user_id: int | None
    chat_id: int
    chat_title: str | None
    source_message_link: str | None
    source_quote: str
    amount_rubles: int
    decided_at: datetime


@dataclass(frozen=True)
class WaitEvent:
    id: int
    wait_id: int
    chat_id: int
    username: str
    user_id: int | None
    event_type: str
    actor_user_id: int | None
    actor_label: str | None
    text: str | None
    created_at: datetime


@dataclass(frozen=True)
class EmployeeStats:
    username: str
    display_name: str | None
    user_id: int | None
    requests_7d: int
    closed_on_time_7d: int
    overdue_7d: int
    warnings_month: int
    fines_month: int
    avg_response_seconds_7d: int | None
    seen_count_7d: int
    answered_not_seen_count_7d: int
    bot_missed_confirmed_month: int
    delay_reasons_7d: dict[str, int]


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.migrate()

    async def close(self) -> None:
        if self.db:
            await self.db.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self.db:
            raise RuntimeError("Storage is not connected")
        return self.db

    async def migrate(self) -> None:
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS known_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                username_lower TEXT UNIQUE,
                first_name TEXT,
                last_name TEXT,
                private_chat_started INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS waits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                chat_title TEXT,
                username_lower TEXT NOT NULL,
                display_name TEXT,
                user_id INTEGER,
                source_message_id INTEGER NOT NULL,
                source_message_link TEXT,
                source_quote TEXT NOT NULL,
                mentioned_by_user_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                next_reminder_at TEXT NOT NULL,
                direct_message_due_at TEXT NOT NULL,
                direct_message_attempted_at TEXT,
                direct_message_sent_at TEXT,
                last_reminder_message_id INTEGER,
                group_reminders_stopped_at TEXT,
                reminder_count INTEGER NOT NULL DEFAULT 0,
                seen_at TEXT,
                seen_by_user_id INTEGER,
                last_intermediate_at TEXT,
                reason_requested_at TEXT,
                reason_due_at TEXT,
                delay_reason TEXT,
                delay_reason_at TEXT,
                leader_request_sent_at TEXT,
                leader_request_chat_id INTEGER,
                leader_request_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                closed_at TEXT,
                closed_by_user_id INTEGER
            );

            DROP INDEX IF EXISTS idx_waits_one_active_per_user_chat;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_waits_one_active_per_user_source
                ON waits(chat_id, username_lower, source_message_id)
                WHERE status = 'active';
            CREATE INDEX IF NOT EXISTS idx_waits_due
                ON waits(status, next_reminder_at, direct_message_due_at);
            CREATE INDEX IF NOT EXISTS idx_waits_user
                ON waits(status, chat_id, user_id, username_lower);

            CREATE TABLE IF NOT EXISTS metric_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                chat_id INTEGER,
                username_lower TEXT,
                wait_id INTEGER,
                value INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_metric_events_type
                ON metric_events(event_type, created_at);

            CREATE TABLE IF NOT EXISTS daily_reports (
                chat_id INTEGER NOT NULL,
                report_date TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, report_date)
            );

            CREATE TABLE IF NOT EXISTS leader_daily_reports (
                report_date TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fine_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wait_id INTEGER NOT NULL UNIQUE,
                chat_id INTEGER NOT NULL,
                chat_title TEXT,
                username_lower TEXT NOT NULL,
                display_name TEXT,
                user_id INTEGER,
                source_message_id INTEGER NOT NULL,
                source_message_link TEXT,
                source_quote TEXT NOT NULL,
                decision TEXT NOT NULL,
                amount_rubles INTEGER NOT NULL DEFAULT 0,
                decided_by_user_id INTEGER NOT NULL,
                decided_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_fine_decisions_month
                ON fine_decisions(decision, decided_at);
            CREATE INDEX IF NOT EXISTS idx_fine_decisions_user
                ON fine_decisions(username_lower, user_id, decided_at);

            CREATE TABLE IF NOT EXISTS wait_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wait_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                username_lower TEXT NOT NULL,
                user_id INTEGER,
                event_type TEXT NOT NULL,
                actor_user_id INTEGER,
                actor_label TEXT,
                text TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_wait_events_wait
                ON wait_events(wait_id, event_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_wait_events_user
                ON wait_events(username_lower, user_id, event_type, created_at);
            """
        )
        await self._add_column_if_missing("waits", "direct_message_attempted_at", "TEXT")
        await self._add_column_if_missing("waits", "last_reminder_message_id", "INTEGER")
        await self._add_column_if_missing("waits", "group_reminders_stopped_at", "TEXT")
        await self._add_column_if_missing("waits", "display_name", "TEXT")
        await self._add_column_if_missing("waits", "leader_request_sent_at", "TEXT")
        await self._add_column_if_missing("waits", "leader_request_chat_id", "INTEGER")
        await self._add_column_if_missing("waits", "leader_request_message_id", "INTEGER")
        await self._add_column_if_missing("waits", "delay_reason_at", "TEXT")
        await self._add_column_if_missing("waits", "delay_reason", "TEXT")
        await self._add_column_if_missing("waits", "reason_due_at", "TEXT")
        await self._add_column_if_missing("waits", "reason_requested_at", "TEXT")
        await self._add_column_if_missing("waits", "last_intermediate_at", "TEXT")
        await self._add_column_if_missing("waits", "seen_by_user_id", "INTEGER")
        await self._add_column_if_missing("waits", "seen_at", "TEXT")
        await self.conn.commit()

    async def leader_daily_report_was_sent(self, *, report_date: date) -> bool:
        cursor = await self.conn.execute(
            """
            SELECT 1
            FROM leader_daily_reports
            WHERE report_date = ?
            """,
            (report_date.isoformat(),),
        )
        return await cursor.fetchone() is not None

    async def mark_leader_daily_report_sent(self, *, report_date: date, sent_at: datetime) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO leader_daily_reports (report_date, sent_at)
            VALUES (?, ?)
            """,
            (report_date.isoformat(), dt_to_db(sent_at)),
        )
        await self.conn.commit()

    async def record_fine_decisions(
        self,
        *,
        waits: list[PendingWait],
        decision: str,
        amount_rubles: int,
        decided_by_user_id: int,
        decided_at: datetime,
    ) -> None:
        for wait in waits:
            await self.conn.execute(
                """
                INSERT INTO fine_decisions (
                    wait_id, chat_id, chat_title, username_lower, display_name, user_id,
                    source_message_id, source_message_link, source_quote,
                    decision, amount_rubles, decided_by_user_id, decided_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wait_id) DO UPDATE SET
                    decision=excluded.decision,
                    amount_rubles=excluded.amount_rubles,
                    decided_by_user_id=excluded.decided_by_user_id,
                    decided_at=excluded.decided_at
                """,
                (
                    wait.id,
                    wait.chat_id,
                    wait.chat_title,
                    wait.username,
                    wait.display_name,
                    wait.user_id,
                    wait.source_message_id,
                    wait.source_message_link,
                    wait.source_quote,
                    decision,
                    amount_rubles,
                    decided_by_user_id,
                    dt_to_db(decided_at),
                    dt_to_db(decided_at),
                ),
            )
        await self.conn.commit()

    async def fine_total_for_month(self, *, start_at: datetime, end_at: datetime) -> int:
        cursor = await self.conn.execute(
            """
            SELECT coalesce(sum(amount_rubles), 0) AS total
            FROM fine_decisions
            WHERE decision = 'issued'
              AND decided_at >= ?
              AND decided_at < ?
            """,
            (dt_to_db(start_at), dt_to_db(end_at)),
        )
        row = await cursor.fetchone()
        return int(row["total"])

    async def fine_total_for_month_by_users(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        username_lowers: list[str],
    ) -> int:
        if not username_lowers:
            return 0

        unique_usernames = sorted(set(username_lowers))
        placeholders = ", ".join("?" for _ in unique_usernames)
        cursor = await self.conn.execute(
            f"""
            SELECT coalesce(sum(amount_rubles), 0) AS total
            FROM fine_decisions
            WHERE decision = 'issued'
              AND decided_at >= ?
              AND decided_at < ?
              AND username_lower IN ({placeholders})
            """,
            (dt_to_db(start_at), dt_to_db(end_at), *unique_usernames),
        )
        row = await cursor.fetchone()
        return int(row["total"])

    async def fine_report_for_month(self, *, start_at: datetime, end_at: datetime) -> list[FineReportItem]:
        cursor = await self.conn.execute(
            """
            SELECT
                username_lower,
                max(display_name) AS display_name,
                max(user_id) AS user_id,
                count(*) AS fine_count,
                coalesce(sum(amount_rubles), 0) AS total_amount
            FROM fine_decisions
            WHERE decision = 'issued'
              AND decided_at >= ?
              AND decided_at < ?
            GROUP BY username_lower
            ORDER BY total_amount DESC, fine_count DESC, username_lower
            """,
            (dt_to_db(start_at), dt_to_db(end_at)),
        )
        rows = await cursor.fetchall()
        return [
            FineReportItem(
                username=row["username_lower"],
                display_name=row["display_name"],
                user_id=row["user_id"],
                fine_count=row["fine_count"],
                total_amount=row["total_amount"],
            )
            for row in rows
        ]

    async def fine_details_for_month(self, *, start_at: datetime, end_at: datetime) -> list[FineReportDetail]:
        cursor = await self.conn.execute(
            """
            SELECT
                id,
                username_lower,
                display_name,
                user_id,
                chat_id,
                chat_title,
                source_message_link,
                source_quote,
                amount_rubles,
                decided_at
            FROM fine_decisions
            WHERE decision = 'issued'
              AND decided_at >= ?
              AND decided_at < ?
            ORDER BY decided_at DESC, id DESC
            """,
            (dt_to_db(start_at), dt_to_db(end_at)),
        )
        rows = await cursor.fetchall()
        return [
            FineReportDetail(
                id=row["id"],
                username=row["username_lower"],
                display_name=row["display_name"],
                user_id=row["user_id"],
                chat_id=row["chat_id"],
                chat_title=row["chat_title"],
                source_message_link=row["source_message_link"],
                source_quote=row["source_quote"],
                amount_rubles=row["amount_rubles"],
                decided_at=dt_from_db(row["decided_at"]),
            )
            for row in rows
        ]

    async def chats_with_waits_created_between(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> list[int]:
        cursor = await self.conn.execute(
            """
            SELECT DISTINCT chat_id
            FROM waits
            WHERE created_at >= ? AND created_at < ?
            ORDER BY chat_id
            """,
            (dt_to_db(start_at), dt_to_db(end_at)),
        )
        rows = await cursor.fetchall()
        return [row["chat_id"] for row in rows]

    async def daily_report_was_sent(self, *, chat_id: int, report_date: date) -> bool:
        cursor = await self.conn.execute(
            """
            SELECT 1
            FROM daily_reports
            WHERE chat_id = ? AND report_date = ?
            """,
            (chat_id, report_date.isoformat()),
        )
        return await cursor.fetchone() is not None

    async def mark_daily_report_sent(self, *, chat_id: int, report_date: date, sent_at: datetime) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO daily_reports (chat_id, report_date, sent_at)
            VALUES (?, ?, ?)
            """,
            (chat_id, report_date.isoformat(), dt_to_db(sent_at)),
        )
        await self.conn.commit()

    async def unanswered_waits_created_between(
        self,
        *,
        chat_id: int,
        start_at: datetime,
        end_at: datetime,
    ) -> list[DailyReportItem]:
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM waits
            WHERE chat_id = ?
              AND status = 'active'
              AND created_at >= ?
              AND created_at < ?
            ORDER BY created_at, id
            """,
            (chat_id, dt_to_db(start_at), dt_to_db(end_at)),
        )
        rows = await cursor.fetchall()
        return [self._daily_report_item(row) for row in rows]

    async def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        cursor = await self.conn.execute(f"PRAGMA table_info({table})")
        columns = {row["name"] for row in await cursor.fetchall()}
        if column not in columns:
            await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def upsert_user(
        self,
        *,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        private_chat_started: bool,
        now: datetime,
    ) -> None:
        username_lower = username.lower() if username else None
        await self.conn.execute(
            """
            INSERT INTO known_users (
                user_id, username, username_lower, first_name, last_name,
                private_chat_started, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                username_lower=excluded.username_lower,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                private_chat_started=max(known_users.private_chat_started, excluded.private_chat_started),
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                username,
                username_lower,
                first_name,
                last_name,
                1 if private_chat_started else 0,
                dt_to_db(now),
            ),
        )
        await self.conn.commit()

    async def get_user_by_username(self, username_lower: str) -> aiosqlite.Row | None:
        cursor = await self.conn.execute(
            "SELECT * FROM known_users WHERE username_lower = ?",
            (username_lower,),
        )
        return await cursor.fetchone()

    async def get_user_by_id(self, user_id: int) -> aiosqlite.Row | None:
        cursor = await self.conn.execute(
            "SELECT * FROM known_users WHERE user_id = ?",
            (user_id,),
        )
        return await cursor.fetchone()

    async def known_users_for_matching(self) -> list[aiosqlite.Row]:
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM known_users
            WHERE username_lower IS NOT NULL
               OR first_name IS NOT NULL
               OR last_name IS NOT NULL
            ORDER BY updated_at DESC
            """
        )
        return await cursor.fetchall()

    async def upsert_wait(
        self,
        *,
        chat_id: int,
        chat_title: str | None,
        username_lower: str,
        display_name: str,
        user_id: int | None,
        source_message_id: int,
        source_message_link: str | None,
        source_quote: str,
        mentioned_by_user_id: int | None,
        now: datetime,
        next_reminder_at: datetime,
        direct_message_due_at: datetime,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO waits (
                chat_id, chat_title, username_lower, display_name, user_id, source_message_id,
                source_message_link, source_quote, mentioned_by_user_id,
                created_at, updated_at, next_reminder_at, direct_message_due_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, username_lower, source_message_id) WHERE status = 'active' DO UPDATE SET
                chat_title=excluded.chat_title,
                display_name=excluded.display_name,
                user_id=coalesce(excluded.user_id, waits.user_id),
                source_message_link=excluded.source_message_link,
                source_quote=excluded.source_quote,
                mentioned_by_user_id=excluded.mentioned_by_user_id,
                updated_at=excluded.updated_at
            """,
            (
                chat_id,
                chat_title,
                username_lower,
                display_name,
                user_id,
                source_message_id,
                source_message_link,
                source_quote,
                mentioned_by_user_id,
                dt_to_db(now),
                dt_to_db(now),
                dt_to_db(next_reminder_at),
                dt_to_db(direct_message_due_at),
            ),
        )
        await self.conn.commit()

    async def active_waits_for_source_message(
        self,
        *,
        chat_id: int,
        source_message_id: int,
    ) -> list[PendingWait]:
        return await self.active_waits_for_source_messages(
            chat_id=chat_id,
            source_message_ids=[source_message_id],
        )

    async def active_waits_for_user(
        self,
        *,
        chat_id: int,
        user_id: int | None,
        username_lower: str | None,
    ) -> list[PendingWait]:
        if user_id is None and username_lower is None:
            return []

        clauses: list[str] = []
        params: list[object] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if username_lower is not None:
            clauses.append("username_lower = ?")
            params.append(username_lower)

        cursor = await self.conn.execute(
            f"""
            SELECT *
            FROM waits
            WHERE status = 'active'
              AND chat_id = ?
              AND ({" OR ".join(clauses)})
            """,
            (chat_id, *params),
        )
        rows = await cursor.fetchall()
        return [self._pending_wait(row) for row in rows]

    async def active_waits_in_chat(self, *, chat_id: int) -> list[PendingWait]:
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM waits
            WHERE status = 'active'
              AND chat_id = ?
            ORDER BY created_at, id
            """,
            (chat_id,),
        )
        rows = await cursor.fetchall()
        return [self._pending_wait(row) for row in rows]

    async def active_waits_for_source_messages(
        self,
        *,
        chat_id: int,
        source_message_ids: list[int],
    ) -> list[PendingWait]:
        if not source_message_ids:
            return []

        unique_ids = sorted(set(source_message_ids))
        placeholders = ", ".join("?" for _ in unique_ids)
        cursor = await self.conn.execute(
            f"""
            SELECT *
            FROM waits
            WHERE status = 'active'
              AND chat_id = ?
              AND source_message_id IN ({placeholders})
            ORDER BY source_message_id, id
            """,
            (chat_id, *unique_ids),
        )
        rows = await cursor.fetchall()
        return [self._pending_wait(row) for row in rows]

    async def close_waits_for_user(
        self,
        *,
        chat_id: int,
        user_id: int | None,
        username_lower: str | None,
        now: datetime,
    ) -> list[PendingWait]:
        if user_id is None and username_lower is None:
            return []

        clauses: list[str] = []
        params: list[object] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if username_lower is not None:
            clauses.append("username_lower = ?")
            params.append(username_lower)

        cursor = await self.conn.execute(
            f"""
            UPDATE waits
            SET status = 'closed',
                closed_at = ?,
                closed_by_user_id = ?,
                updated_at = ?
            WHERE status = 'active'
              AND chat_id = ?
              AND ({" OR ".join(clauses)})
            RETURNING *
            """,
            (dt_to_db(now), user_id, dt_to_db(now), chat_id, *params),
        )
        rows = await cursor.fetchall()
        await self.conn.commit()
        return sorted((self._pending_wait(row) for row in rows), key=lambda wait: wait.id)

    async def close_wait_by_id(self, wait_id: int, closed_by_user_id: int, now: datetime) -> bool:
        cursor = await self.conn.execute(
            """
            UPDATE waits
            SET status = 'closed',
                closed_at = ?,
                closed_by_user_id = ?,
                updated_at = ?
            WHERE id = ? AND status = 'active'
            RETURNING id
            """,
            (dt_to_db(now), closed_by_user_id, dt_to_db(now), wait_id),
        )
        row = await cursor.fetchone()
        await self.conn.commit()
        return row is not None

    async def close_waits_for_source_messages(
        self,
        *,
        chat_id: int,
        source_message_ids: list[int],
        closed_by_user_id: int | None,
        now: datetime,
    ) -> list[PendingWait]:
        if not source_message_ids:
            return []

        unique_ids = sorted(set(source_message_ids))
        placeholders = ", ".join("?" for _ in unique_ids)
        cursor = await self.conn.execute(
            f"""
            UPDATE waits
            SET status = 'closed',
                closed_at = ?,
                closed_by_user_id = ?,
                updated_at = ?
            WHERE status = 'active'
              AND chat_id = ?
              AND source_message_id IN ({placeholders})
            RETURNING *
            """,
            (dt_to_db(now), closed_by_user_id, dt_to_db(now), chat_id, *unique_ids),
        )
        rows = await cursor.fetchall()
        await self.conn.commit()
        return sorted((self._pending_wait(row) for row in rows), key=lambda wait: wait.id)

    async def reschedule_waits_for_source_messages(
        self,
        *,
        chat_id: int,
        source_message_ids: list[int],
        next_reminder_at: datetime,
    ) -> list[PendingWait]:
        waits = await self.active_waits_for_source_messages(
            chat_id=chat_id,
            source_message_ids=source_message_ids,
        )
        if not waits:
            return []

        wait_ids = [wait.id for wait in waits]
        placeholders = ", ".join("?" for _ in wait_ids)
        await self.conn.execute(
            f"""
            UPDATE waits
            SET next_reminder_at = ?,
                group_reminders_stopped_at = NULL,
                updated_at = ?
            WHERE id IN ({placeholders}) AND status = 'active'
            """,
            (dt_to_db(next_reminder_at), dt_to_db(next_reminder_at), *wait_ids),
        )
        await self.conn.commit()
        return waits

    async def mark_source_seen(
        self,
        *,
        chat_id: int,
        source_message_ids: list[int],
        seen_by_user_id: int,
        seen_at: datetime,
        next_reminder_at: datetime,
    ) -> list[PendingWait]:
        waits = await self.active_waits_for_source_messages(chat_id=chat_id, source_message_ids=source_message_ids)
        if not waits:
            return []

        wait_ids = [wait.id for wait in waits]
        placeholders = ", ".join("?" for _ in wait_ids)
        await self.conn.execute(
            f"""
            UPDATE waits
            SET seen_at = ?,
                seen_by_user_id = ?,
                next_reminder_at = ?,
                group_reminders_stopped_at = NULL,
                updated_at = ?
            WHERE id IN ({placeholders}) AND status = 'active'
            """,
            (dt_to_db(seen_at), seen_by_user_id, dt_to_db(next_reminder_at), dt_to_db(seen_at), *wait_ids),
        )
        await self.conn.commit()
        return await self.active_waits_for_source_messages(chat_id=chat_id, source_message_ids=source_message_ids)

    async def mark_source_intermediate(
        self,
        *,
        chat_id: int,
        source_message_ids: list[int],
        intermediate_at: datetime,
        next_reminder_at: datetime,
    ) -> list[PendingWait]:
        waits = await self.active_waits_for_source_messages(chat_id=chat_id, source_message_ids=source_message_ids)
        if not waits:
            return []

        wait_ids = [wait.id for wait in waits]
        placeholders = ", ".join("?" for _ in wait_ids)
        await self.conn.execute(
            f"""
            UPDATE waits
            SET last_intermediate_at = ?,
                next_reminder_at = ?,
                group_reminders_stopped_at = NULL,
                updated_at = ?
            WHERE id IN ({placeholders}) AND status = 'active'
            """,
            (dt_to_db(intermediate_at), dt_to_db(next_reminder_at), dt_to_db(intermediate_at), *wait_ids),
        )
        await self.conn.commit()
        return await self.active_waits_for_source_messages(chat_id=chat_id, source_message_ids=source_message_ids)

    async def request_reason_for_source(
        self,
        *,
        chat_id: int,
        source_message_ids: list[int],
        requested_at: datetime,
        reason_due_at: datetime,
        reminder_message_id: int | None = None,
    ) -> list[PendingWait]:
        waits = await self.active_waits_for_source_messages(chat_id=chat_id, source_message_ids=source_message_ids)
        if not waits:
            return []

        wait_ids = [wait.id for wait in waits]
        placeholders = ", ".join("?" for _ in wait_ids)
        await self.conn.execute(
            f"""
            UPDATE waits
            SET reason_requested_at = coalesce(reason_requested_at, ?),
                reason_due_at = ?,
                next_reminder_at = ?,
                last_reminder_message_id = coalesce(?, last_reminder_message_id),
                reminder_count = reminder_count + 1,
                updated_at = ?
            WHERE id IN ({placeholders}) AND status = 'active'
            """,
            (
                dt_to_db(requested_at),
                dt_to_db(reason_due_at),
                dt_to_db(reason_due_at),
                reminder_message_id,
                dt_to_db(requested_at),
                *wait_ids,
            ),
        )
        await self.conn.commit()
        return await self.active_waits_for_source_messages(chat_id=chat_id, source_message_ids=source_message_ids)

    async def set_delay_reason_for_source(
        self,
        *,
        chat_id: int,
        source_message_ids: list[int],
        reason: str,
        reason_at: datetime,
    ) -> list[PendingWait]:
        waits = await self.active_waits_for_source_messages(chat_id=chat_id, source_message_ids=source_message_ids)
        if not waits:
            return []

        wait_ids = [wait.id for wait in waits]
        placeholders = ", ".join("?" for _ in wait_ids)
        await self.conn.execute(
            f"""
            UPDATE waits
            SET delay_reason = ?,
                delay_reason_at = ?,
                updated_at = ?
            WHERE id IN ({placeholders}) AND status = 'active'
            """,
            (reason, dt_to_db(reason_at), dt_to_db(reason_at), *wait_ids),
        )
        await self.conn.commit()
        return await self.active_waits_for_source_messages(chat_id=chat_id, source_message_ids=source_message_ids)

    async def mark_leader_request_sent_for_source(
        self,
        *,
        chat_id: int,
        source_message_ids: list[int],
        sent_at: datetime,
        leader_chat_id: int,
        leader_message_id: int,
    ) -> list[PendingWait]:
        waits = await self.active_waits_for_source_messages(chat_id=chat_id, source_message_ids=source_message_ids)
        if not waits:
            return []

        wait_ids = [wait.id for wait in waits]
        placeholders = ", ".join("?" for _ in wait_ids)
        await self.conn.execute(
            f"""
            UPDATE waits
            SET leader_request_sent_at = ?,
                leader_request_chat_id = ?,
                leader_request_message_id = ?,
                group_reminders_stopped_at = ?,
                updated_at = ?
            WHERE id IN ({placeholders}) AND status = 'active'
            """,
            (dt_to_db(sent_at), leader_chat_id, leader_message_id, dt_to_db(sent_at), dt_to_db(sent_at), *wait_ids),
        )
        await self.conn.commit()
        return await self.active_waits_for_source_messages(chat_id=chat_id, source_message_ids=source_message_ids)

    async def get_wait_by_id(self, wait_id: int) -> PendingWait | None:
        cursor = await self.conn.execute("SELECT * FROM waits WHERE id = ?", (wait_id,))
        row = await cursor.fetchone()
        return self._pending_wait(row) if row else None

    async def get_active_wait_by_reminder_message(
        self,
        *,
        chat_id: int,
        reminder_message_id: int,
    ) -> PendingWait | None:
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM waits
            WHERE status = 'active'
              AND chat_id = ?
              AND last_reminder_message_id = ?
            """,
            (chat_id, reminder_message_id),
        )
        row = await cursor.fetchone()
        return self._pending_wait(row) if row else None

    async def active_waits_for_reminder_message(
        self,
        *,
        chat_id: int,
        reminder_message_id: int,
    ) -> list[PendingWait]:
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM waits
            WHERE status = 'active'
              AND chat_id = ?
              AND last_reminder_message_id = ?
            ORDER BY id
            """,
            (chat_id, reminder_message_id),
        )
        rows = await cursor.fetchall()
        return [self._pending_wait(row) for row in rows]

    async def due_waits(self, now: datetime) -> list[PendingWait]:
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM waits
            WHERE status = 'active'
              AND ((group_reminders_stopped_at IS NULL AND next_reminder_at <= ?)
                   OR (direct_message_attempted_at IS NULL AND direct_message_due_at <= ?))
            ORDER BY min(next_reminder_at, direct_message_due_at), id
            """,
            (dt_to_db(now), dt_to_db(now)),
        )
        rows = await cursor.fetchall()
        return [self._pending_wait(row) for row in rows]

    async def mark_group_reminded(
        self,
        wait_id: int,
        *,
        reminder_message_id: int,
        next_reminder_at: datetime,
    ) -> None:
        await self.conn.execute(
            """
            UPDATE waits
            SET reminder_count = reminder_count + 1,
                last_reminder_message_id = ?,
                next_reminder_at = ?,
                updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (reminder_message_id, dt_to_db(next_reminder_at), dt_to_db(next_reminder_at), wait_id),
        )
        await self.conn.commit()

    async def stop_group_reminders(self, wait_id: int, stopped_at: datetime) -> None:
        await self.conn.execute(
            """
            UPDATE waits
            SET group_reminders_stopped_at = ?,
                updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (dt_to_db(stopped_at), dt_to_db(stopped_at), wait_id),
        )
        await self.conn.commit()

    async def reschedule_wait(self, wait_id: int, next_reminder_at: datetime) -> None:
        await self.conn.execute(
            """
            UPDATE waits
            SET next_reminder_at = ?,
                group_reminders_stopped_at = NULL,
                updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (dt_to_db(next_reminder_at), dt_to_db(next_reminder_at), wait_id),
        )
        await self.conn.commit()

    async def mark_direct_message_attempted(self, wait_id: int, attempted_at: datetime) -> None:
        await self.conn.execute(
            """
            UPDATE waits
            SET direct_message_attempted_at = ?,
                updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (dt_to_db(attempted_at), dt_to_db(attempted_at), wait_id),
        )
        await self.conn.commit()

    async def mark_direct_message_sent(self, wait_id: int, sent_at: datetime) -> None:
        await self.conn.execute(
            """
            UPDATE waits
            SET direct_message_attempted_at = ?,
                direct_message_sent_at = ?,
                updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (dt_to_db(sent_at), dt_to_db(sent_at), dt_to_db(sent_at), wait_id),
        )
        await self.conn.commit()

    async def resolve_user_id_for_wait(self, wait_id: int, user_id: int) -> None:
        await self.conn.execute(
            "UPDATE waits SET user_id = ? WHERE id = ? AND user_id IS NULL",
            (user_id, wait_id),
        )
        await self.conn.commit()

    async def record_wait_event(
        self,
        wait: PendingWait,
        *,
        event_type: str,
        created_at: datetime,
        actor_user_id: int | None = None,
        actor_label: str | None = None,
        text: str | None = None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO wait_events (
                wait_id, chat_id, username_lower, user_id, event_type,
                actor_user_id, actor_label, text, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wait.id,
                wait.chat_id,
                wait.username,
                wait.user_id,
                event_type,
                actor_user_id,
                actor_label,
                text,
                dt_to_db(created_at),
            ),
        )
        await self.conn.commit()

    async def record_wait_events(
        self,
        waits: list[PendingWait],
        *,
        event_type: str,
        created_at: datetime,
        actor_user_id: int | None = None,
        actor_label: str | None = None,
        text: str | None = None,
    ) -> None:
        for wait in waits:
            await self.record_wait_event(
                wait,
                event_type=event_type,
                created_at=created_at,
                actor_user_id=actor_user_id,
                actor_label=actor_label,
                text=text,
            )

    async def wait_events_for_source(
        self,
        *,
        chat_id: int,
        source_message_id: int,
    ) -> list[WaitEvent]:
        cursor = await self.conn.execute(
            """
            SELECT e.*
            FROM wait_events e
            JOIN waits w ON w.id = e.wait_id
            WHERE w.chat_id = ?
              AND w.source_message_id = ?
            ORDER BY e.created_at, e.id
            """,
            (chat_id, source_message_id),
        )
        rows = await cursor.fetchall()
        return [self._wait_event(row) for row in rows]

    async def employee_stats(
        self,
        *,
        username_lower: str | None,
        user_id: int | None,
        now: datetime,
    ) -> EmployeeStats:
        if username_lower is None and user_id is None:
            raise ValueError("username_lower or user_id is required")

        week_start = now - timedelta(days=7)
        month_start = datetime(now.year, now.month, 1, tzinfo=now.tzinfo)

        clauses: list[str] = []
        params: list[object] = []
        if username_lower is not None:
            clauses.append("username_lower = ?")
            params.append(username_lower)
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        user_clause = " OR ".join(clauses)

        cursor = await self.conn.execute(
            f"""
            SELECT
                coalesce(max(display_name), max(username_lower)) AS display_name,
                max(username_lower) AS username_lower,
                max(user_id) AS user_id,
                count(*) AS requests_7d,
                coalesce(sum(CASE WHEN reminder_count = 0 AND status = 'closed' THEN 1 ELSE 0 END), 0) AS closed_on_time_7d,
                coalesce(sum(CASE WHEN reminder_count > 0 THEN 1 ELSE 0 END), 0) AS overdue_7d,
                avg(CASE WHEN status = 'closed' AND closed_at IS NOT NULL THEN (julianday(closed_at) - julianday(created_at)) * 86400 END) AS avg_response_seconds
            FROM waits
            WHERE created_at >= ?
              AND ({user_clause})
            """,
            (dt_to_db(week_start), *params),
        )
        row = await cursor.fetchone()

        cursor = await self.conn.execute(
            f"""
            SELECT
                coalesce(sum(CASE WHEN decision = 'issued' THEN 1 ELSE 0 END), 0) AS fines_month,
                coalesce(sum(CASE WHEN decision = 'warning' THEN 1 ELSE 0 END), 0) AS warnings_month,
                coalesce(sum(CASE WHEN decision = 'manual_answer_confirmed' THEN 1 ELSE 0 END), 0) AS bot_missed_confirmed_month
            FROM fine_decisions
            WHERE decided_at >= ?
              AND ({user_clause})
            """,
            (dt_to_db(month_start), *params),
        )
        decisions = await cursor.fetchone()

        cursor = await self.conn.execute(
            f"""
            SELECT
                coalesce(sum(CASE WHEN event_type = 'seen' THEN 1 ELSE 0 END), 0) AS seen_count,
                coalesce(sum(CASE WHEN event_type = 'reason_answered_not_seen' THEN 1 ELSE 0 END), 0) AS answered_not_seen_count
            FROM wait_events
            WHERE created_at >= ?
              AND ({user_clause})
            """,
            (dt_to_db(week_start), *params),
        )
        events = await cursor.fetchone()

        cursor = await self.conn.execute(
            f"""
            SELECT text, count(*) AS total
            FROM wait_events
            WHERE created_at >= ?
              AND event_type = 'delay_reason'
              AND ({user_clause})
            GROUP BY text
            ORDER BY total DESC, text
            """,
            (dt_to_db(week_start), *params),
        )
        reasons = {reason_row["text"]: reason_row["total"] for reason_row in await cursor.fetchall() if reason_row["text"]}

        avg_raw = row["avg_response_seconds"] if row else None
        return EmployeeStats(
            username=(row["username_lower"] if row and row["username_lower"] else username_lower or f"user_id:{user_id}"),
            display_name=(row["display_name"] if row else None),
            user_id=(row["user_id"] if row and row["user_id"] else user_id),
            requests_7d=int(row["requests_7d"] or 0) if row else 0,
            closed_on_time_7d=int(row["closed_on_time_7d"] or 0) if row else 0,
            overdue_7d=int(row["overdue_7d"] or 0) if row else 0,
            warnings_month=int(decisions["warnings_month"] or 0),
            fines_month=int(decisions["fines_month"] or 0),
            avg_response_seconds_7d=int(avg_raw) if avg_raw is not None else None,
            seen_count_7d=int(events["seen_count"] or 0),
            answered_not_seen_count_7d=int(events["answered_not_seen_count"] or 0),
            bot_missed_confirmed_month=int(decisions["bot_missed_confirmed_month"] or 0),
            delay_reasons_7d=reasons,
        )

    async def record_metric(
        self,
        event_type: str,
        *,
        now: datetime,
        chat_id: int | None = None,
        username_lower: str | None = None,
        wait_id: int | None = None,
        value: int = 1,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO metric_events (event_type, chat_id, username_lower, wait_id, value, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_type, chat_id, username_lower, wait_id, value, dt_to_db(now)),
        )
        await self.conn.commit()

    async def metrics_summary(self) -> dict[str, int]:
        cursor = await self.conn.execute(
            """
            SELECT event_type, sum(value) AS total
            FROM metric_events
            GROUP BY event_type
            """
        )
        summary = {row["event_type"]: row["total"] for row in await cursor.fetchall()}

        cursor = await self.conn.execute("SELECT count(*) AS total FROM waits WHERE status = 'active'")
        summary["active_waits"] = (await cursor.fetchone())["total"]

        cursor = await self.conn.execute("SELECT count(*) AS total FROM waits WHERE status = 'closed'")
        summary["closed_waits_total"] = (await cursor.fetchone())["total"]
        return summary

    def _pending_wait(self, row: aiosqlite.Row) -> PendingWait:
        return PendingWait(
            id=row["id"],
            chat_id=row["chat_id"],
            chat_title=row["chat_title"],
            username=row["username_lower"],
            display_name=row["display_name"],
            user_id=row["user_id"],
            source_message_id=row["source_message_id"],
            source_message_link=row["source_message_link"],
            source_quote=row["source_quote"],
            created_at=dt_from_db(row["created_at"]),
            next_reminder_at=dt_from_db(row["next_reminder_at"]),
            direct_message_due_at=dt_from_db(row["direct_message_due_at"]),
            direct_message_attempted_at=(
                dt_from_db(row["direct_message_attempted_at"])
                if row["direct_message_attempted_at"]
                else None
            ),
            direct_message_sent_at=(
                dt_from_db(row["direct_message_sent_at"])
                if row["direct_message_sent_at"]
                else None
            ),
            last_reminder_message_id=row["last_reminder_message_id"],
            group_reminders_stopped_at=(
                dt_from_db(row["group_reminders_stopped_at"])
                if row["group_reminders_stopped_at"]
                else None
            ),
            reminder_count=row["reminder_count"],
            seen_at=dt_from_db(row["seen_at"]) if row["seen_at"] else None,
            seen_by_user_id=row["seen_by_user_id"],
            last_intermediate_at=(
                dt_from_db(row["last_intermediate_at"])
                if row["last_intermediate_at"]
                else None
            ),
            reason_requested_at=(
                dt_from_db(row["reason_requested_at"])
                if row["reason_requested_at"]
                else None
            ),
            reason_due_at=dt_from_db(row["reason_due_at"]) if row["reason_due_at"] else None,
            delay_reason=row["delay_reason"],
            delay_reason_at=dt_from_db(row["delay_reason_at"]) if row["delay_reason_at"] else None,
            leader_request_sent_at=(
                dt_from_db(row["leader_request_sent_at"])
                if row["leader_request_sent_at"]
                else None
            ),
            leader_request_chat_id=row["leader_request_chat_id"],
            leader_request_message_id=row["leader_request_message_id"],
            status=row["status"],
        )

    def _wait_event(self, row: aiosqlite.Row) -> WaitEvent:
        return WaitEvent(
            id=row["id"],
            wait_id=row["wait_id"],
            chat_id=row["chat_id"],
            username=row["username_lower"],
            user_id=row["user_id"],
            event_type=row["event_type"],
            actor_user_id=row["actor_user_id"],
            actor_label=row["actor_label"],
            text=row["text"],
            created_at=dt_from_db(row["created_at"]),
        )

    def _daily_report_item(self, row: aiosqlite.Row) -> DailyReportItem:
        return DailyReportItem(
            id=row["id"],
            chat_id=row["chat_id"],
            chat_title=row["chat_title"],
            username=row["username_lower"],
            display_name=row["display_name"],
            user_id=row["user_id"],
            source_message_link=row["source_message_link"],
            source_quote=row["source_quote"],
            created_at=dt_from_db(row["created_at"]),
            reminder_count=row["reminder_count"],
            direct_message_sent_at=(
                dt_from_db(row["direct_message_sent_at"])
                if row["direct_message_sent_at"]
                else None
            ),
            group_reminders_stopped_at=(
                dt_from_db(row["group_reminders_stopped_at"])
                if row["group_reminders_stopped_at"]
                else None
            ),
        )
