import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.storage import Storage


def test_close_waits_for_source_message_closes_all_addresses(tmp_path) -> None:
    async def scenario() -> None:
        storage = Storage(str(tmp_path / "bot.sqlite3"))
        await storage.connect()
        try:
            now = datetime(2026, 5, 24, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
            for username, message_id in (
                ("firstuser", 100),
                ("firstuser", 100),
                ("seconduser", 100),
                ("firstuser", 200),
                ("thirduser", 200),
            ):
                await storage.upsert_wait(
                    chat_id=-100123,
                    chat_title="Work chat",
                    username_lower=username,
                    display_name=f"@{username}",
                    user_id=None,
                    source_message_id=message_id,
                    source_message_link=f"https://t.me/c/123/{message_id}",
                    source_quote="Question",
                    mentioned_by_user_id=1,
                    now=now,
                    next_reminder_at=now + timedelta(minutes=20),
                    direct_message_due_at=now + timedelta(hours=1),
                )

            source_waits = await storage.active_waits_for_source_message(chat_id=-100123, source_message_id=100)
            for wait in source_waits:
                await storage.mark_group_reminded(
                    wait.id,
                    reminder_message_id=555,
                    next_reminder_at=now + timedelta(minutes=40),
                )
            waits_by_reminder = await storage.active_waits_for_reminder_message(
                chat_id=-100123,
                reminder_message_id=555,
            )
            closed = await storage.close_waits_for_source_messages(
                chat_id=-100123,
                source_message_ids=[100],
                closed_by_user_id=999,
                now=now,
            )
            already_closed = await storage.close_waits_for_source_messages(
                chat_id=-100123,
                source_message_ids=[100],
                closed_by_user_id=888,
                now=now,
            )
            remaining = await storage.active_waits_for_source_message(chat_id=-100123, source_message_id=200)

            assert {wait.username for wait in waits_by_reminder} == {"firstuser", "seconduser"}
            assert {wait.username for wait in closed} == {"firstuser", "seconduser"}
            assert already_closed == []
            assert [wait.username for wait in remaining] == ["firstuser", "thirduser"]
        finally:
            await storage.close()

    asyncio.run(scenario())


def test_fine_report_groups_monthly_totals(tmp_path) -> None:
    async def scenario() -> None:
        storage = Storage(str(tmp_path / "bot.sqlite3"))
        await storage.connect()
        try:
            now = datetime(2026, 5, 24, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
            for username, message_id in (("firstuser", 100), ("seconduser", 100)):
                await storage.upsert_wait(
                    chat_id=-100123,
                    chat_title="Work chat",
                    username_lower=username,
                    display_name=f"@{username}",
                    user_id=None,
                    source_message_id=message_id,
                    source_message_link=f"https://t.me/c/123/{message_id}",
                    source_quote="Question",
                    mentioned_by_user_id=1,
                    now=now,
                    next_reminder_at=now + timedelta(minutes=15),
                    direct_message_due_at=now + timedelta(hours=1),
                )

            waits = await storage.active_waits_for_source_message(chat_id=-100123, source_message_id=100)
            await storage.record_fine_decisions(
                waits=waits,
                decision="issued",
                amount_rubles=500,
                decided_by_user_id=999,
                decided_at=now,
            )
            start_at = datetime(2026, 5, 1, tzinfo=ZoneInfo("Europe/Moscow"))
            end_at = datetime(2026, 6, 1, tzinfo=ZoneInfo("Europe/Moscow"))

            total = await storage.fine_total_for_month(start_at=start_at, end_at=end_at)
            items = await storage.fine_report_for_month(start_at=start_at, end_at=end_at)
            details = await storage.fine_details_for_month(start_at=start_at, end_at=end_at)

            assert total == 1000
            assert {item.username: item.total_amount for item in items} == {"firstuser": 500, "seconduser": 500}
            assert len(details) == 2
            assert {item.username for item in details} == {"firstuser", "seconduser"}
            assert all(item.amount_rubles == 500 for item in details)
            assert all(item.chat_title == "Work chat" for item in details)
            assert all(item.source_quote == "Question" for item in details)
        finally:
            await storage.close()

    asyncio.run(scenario())



def test_seen_intermediate_reason_and_warning_history(tmp_path) -> None:
    async def scenario() -> None:
        storage = Storage(str(tmp_path / "bot.sqlite3"))
        await storage.connect()
        try:
            now = datetime(2026, 5, 24, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
            await storage.upsert_wait(
                chat_id=-100123,
                chat_title="Work chat",
                username_lower="polina",
                display_name="Полина",
                user_id=456,
                source_message_id=100,
                source_message_link="https://t.me/c/123/100",
                source_quote="Оплатите заявку 10009 Полина",
                mentioned_by_user_id=1,
                now=now,
                next_reminder_at=now + timedelta(minutes=15),
                direct_message_due_at=now + timedelta(hours=1),
            )
            waits = await storage.active_waits_for_source_message(chat_id=-100123, source_message_id=100)

            seen_waits = await storage.mark_source_seen(
                chat_id=-100123,
                source_message_ids=[100],
                seen_by_user_id=456,
                seen_at=now + timedelta(minutes=15),
                next_reminder_at=now + timedelta(minutes=45),
            )
            await storage.record_wait_events(
                seen_waits,
                event_type="seen",
                created_at=now + timedelta(minutes=15),
                actor_user_id=456,
                actor_label="Полина",
                text="seen",
            )
            active_after_seen = await storage.active_waits_for_source_message(chat_id=-100123, source_message_id=100)

            intermediate_waits = await storage.mark_source_intermediate(
                chat_id=-100123,
                source_message_ids=[100],
                intermediate_at=now + timedelta(minutes=30),
                next_reminder_at=now + timedelta(minutes=60),
            )
            await storage.record_wait_events(
                intermediate_waits,
                event_type="intermediate_answer",
                created_at=now + timedelta(minutes=30),
                actor_user_id=456,
                actor_label="Полина",
                text="Сейчас посмотрю",
            )
            active_after_intermediate = await storage.active_waits_for_source_message(chat_id=-100123, source_message_id=100)

            requested = await storage.request_reason_for_source(
                chat_id=-100123,
                source_message_ids=[100],
                requested_at=now + timedelta(minutes=60),
                reason_due_at=now + timedelta(minutes=75),
                reminder_message_id=777,
            )
            reasoned = await storage.set_delay_reason_for_source(
                chat_id=-100123,
                source_message_ids=[100],
                reason="call",
                reason_at=now + timedelta(minutes=61),
            )
            await storage.record_wait_events(
                reasoned,
                event_type="delay_reason",
                created_at=now + timedelta(minutes=61),
                actor_user_id=456,
                actor_label="Полина",
                text="📞 Был на созвоне",
            )
            closed = await storage.close_waits_for_source_messages(
                chat_id=-100123,
                source_message_ids=[100],
                closed_by_user_id=999,
                now=now + timedelta(minutes=80),
            )
            await storage.record_fine_decisions(
                waits=closed,
                decision="warning",
                amount_rubles=0,
                decided_by_user_id=999,
                decided_at=now + timedelta(minutes=80),
            )
            stats = await storage.employee_stats(username_lower="polina", user_id=456, now=now + timedelta(minutes=80))

            assert active_after_seen[0].seen_by_user_id == 456
            assert active_after_seen[0].status == "active"
            assert active_after_intermediate[0].last_intermediate_at is not None
            assert active_after_intermediate[0].status == "active"
            assert requested[0].reason_due_at == now + timedelta(minutes=75)
            assert reasoned[0].delay_reason == "call"
            assert stats.warnings_month == 1
            assert stats.seen_count_7d == 1
            assert stats.delay_reasons_7d == {"📞 Был на созвоне": 1}
        finally:
            await storage.close()

    asyncio.run(scenario())
