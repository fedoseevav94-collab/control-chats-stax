from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiogram.enums import MessageEntityType
from aiogram.types import MessageEntity, User

from bot.main import format_elapsed, select_waits_answered_by_message, source_text_match_score, wait_matches_sender_display, wait_matches_telegram_user, wait_target_label, wait_targets_label
from bot.storage import PendingWait
from bot.telegram_utils import extract_mention_targets, source_reference


def test_extract_targets_reads_text_mentions_and_plain_mentions() -> None:
    text = "Sergey @OtherUser"
    user = User(id=123, is_bot=False, first_name="Sergey", username="Norblacksmith")
    message = SimpleNamespace(
        text=text,
        caption=None,
        entities=[
            MessageEntity(type=MessageEntityType.TEXT_MENTION, offset=0, length=6, user=user),
            MessageEntity(type=MessageEntityType.MENTION, offset=7, length=10),
        ],
        caption_entities=None,
    )

    targets = {target.identity: target for target in extract_mention_targets(message)}

    assert targets["norblacksmith"].display_name == "Sergey"
    assert targets["norblacksmith"].user_id == 123
    assert targets["otheruser"].display_name == "@otheruser"


def test_extract_targets_reads_tg_user_text_links() -> None:
    text = "Полина"
    message = SimpleNamespace(
        text=text,
        caption=None,
        entities=[
            MessageEntity(type=MessageEntityType.TEXT_LINK, offset=0, length=6, url="tg://user?id=456"),
        ],
        caption_entities=None,
    )

    targets = extract_mention_targets(message)

    assert targets[0].identity == "user_id:456"
    assert targets[0].display_name == "Полина"
    assert targets[0].user_id == 456


def test_wait_target_label_uses_clickable_user_id_when_username_is_hidden() -> None:
    wait = _wait(username="user_id:456", display_name="Hidden User", user_id=456)

    assert wait_target_label(wait) == '<a href="tg://user?id=456">Hidden User</a>'


def test_wait_targets_label_lists_unique_addresses() -> None:
    waits = [
        _wait(username="firstuser", display_name="@firstuser", user_id=None),
        _wait(username="firstuser", display_name="@firstuser", user_id=None),
        _wait(username="user_id:456", display_name="Hidden User", user_id=456),
    ]

    assert wait_targets_label(waits) == '@firstuser, <a href="tg://user?id=456">Hidden User</a>'


def test_source_reference_embeds_message_link_in_quote_text() -> None:
    reference = source_reference("https://t.me/c/123/10", 'Need <answer> "today"')

    assert reference == '<a href="https://t.me/c/123/10">Need &lt;answer&gt; &quot;today&quot;</a>'


def test_source_reference_falls_back_to_escaped_quote_without_link() -> None:
    assert source_reference(None, "Need <answer>") == '"Need &lt;answer&gt;"'


def test_format_elapsed_minutes_and_hours() -> None:
    start = datetime(2026, 5, 25, 12, 50, tzinfo=ZoneInfo("Europe/Moscow"))

    assert format_elapsed(start, datetime(2026, 5, 25, 13, 41, tzinfo=ZoneInfo("Europe/Moscow"))) == "51 минута"
    assert format_elapsed(start, datetime(2026, 5, 25, 15, 5, tzinfo=ZoneInfo("Europe/Moscow"))) == "2 часа 15 минут"


def test_select_waits_answered_by_message_closes_single_active_wait() -> None:
    wait = _wait(username="firstuser", display_name="@firstuser", user_id=None)
    message = SimpleNamespace(text="Заказаны, сегодня привезут", caption=None)

    assert select_waits_answered_by_message([wait], message) == [wait]


def test_select_waits_answered_by_message_picks_matching_source_when_many_are_active() -> None:
    lamps_wait = _wait(
        username="firstuser",
        display_name="@firstuser",
        user_id=None,
        source_message_id=10,
        source_quote="когда будут контактные лампочки и патроны",
    )
    suspension_wait = _wait(
        username="firstuser",
        display_name="@firstuser",
        user_id=None,
        source_message_id=20,
        source_quote="нужно заменить масло и переднюю подвеску",
    )
    message = SimpleNamespace(text="По подвеске деталь заказана", caption=None)

    assert select_waits_answered_by_message([lamps_wait, suspension_wait], message) == [suspension_wait]


def test_select_waits_answered_by_message_keeps_ambiguous_many_waits_open() -> None:
    first_wait = _wait(
        username="firstuser",
        display_name="@firstuser",
        user_id=None,
        source_message_id=10,
        source_quote="когда будут контактные лампочки и патроны",
    )
    second_wait = _wait(
        username="firstuser",
        display_name="@firstuser",
        user_id=None,
        source_message_id=20,
        source_quote="нужно заменить масло и переднюю подвеску",
    )
    message = SimpleNamespace(text="Разберусь и отпишусь", caption=None)

    assert select_waits_answered_by_message([first_wait, second_wait], message) == []


def test_wait_matches_sender_display_for_hidden_username_employee() -> None:
    wait = _wait(username="user_id:456", display_name="Полина", user_id=None)
    user = User(id=456, is_bot=False, first_name="Полина")

    assert wait_matches_sender_display(wait, user)


def test_wait_does_not_match_other_user_id_by_display_name() -> None:
    wait = _wait(username="user_id:456", display_name="Полина", user_id=456)
    user = User(id=789, is_bot=False, first_name="Полина")

    assert not wait_matches_sender_display(wait, user)


def test_wait_matches_reaction_user_by_display_name_without_username() -> None:
    wait = _wait(username="user_id:456", display_name="Полина", user_id=None)
    user = User(id=456, is_bot=False, first_name="Полина")

    assert wait_matches_telegram_user(wait, user)


def test_wait_matches_reaction_user_by_username() -> None:
    wait = _wait(username="norblacksmith", display_name="@norblacksmith", user_id=None)
    user = User(id=456, is_bot=False, first_name="Nor", username="Norblacksmith")

    assert wait_matches_telegram_user(wait, user)


def test_wait_does_not_match_reaction_from_other_hidden_user() -> None:
    wait = _wait(username="user_id:456", display_name="Полина", user_id=456)
    user = User(id=789, is_bot=False, first_name="Полина")

    assert not wait_matches_telegram_user(wait, user)


def test_source_text_match_score_matches_reply_quote_text() -> None:
    wait = _wait(
        username="user_id:456",
        display_name="Полина",
        user_id=456,
        source_quote="Исаев Аслан Шамханович Полина",
    )

    assert source_text_match_score("Исаев Аслан Шамханович Полина", wait) >= 2


def _wait(
    username: str,
    display_name: str,
    user_id: int | None,
    *,
    source_message_id: int = 10,
    source_quote: str = "Question",
) -> PendingWait:
    now = datetime(2026, 5, 24, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    return PendingWait(
        id=1,
        chat_id=-100123,
        chat_title="Work chat",
        username=username,
        display_name=display_name,
        user_id=user_id,
        source_message_id=source_message_id,
        source_message_link=f"https://t.me/c/123/{source_message_id}",
        source_quote=source_quote,
        created_at=now,
        next_reminder_at=now,
        direct_message_due_at=now,
        direct_message_attempted_at=None,
        direct_message_sent_at=None,
        last_reminder_message_id=20,
        group_reminders_stopped_at=None,
        reminder_count=1,
        status="active",
    )
