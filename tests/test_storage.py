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
                ("seconduser", 100),
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

            assert {wait.username for wait in closed} == {"firstuser", "seconduser"}
            assert already_closed == []
            assert [wait.username for wait in remaining] == ["thirduser"]
        finally:
            await storage.close()

    asyncio.run(scenario())
