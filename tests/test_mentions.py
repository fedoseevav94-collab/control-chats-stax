from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from aiogram.enums import MessageEntityType
from aiogram.types import MessageEntity, User

from bot.main import can_user_control_waits, format_elapsed, message_requires_response, select_waits_answered_by_message, single_source_waits, source_text_match_score, wait_keyboard, wait_matches_sender_display, wait_matches_telegram_user, wait_target_label, wait_targets_label
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


def test_message_requires_response_for_direct_task_with_mention() -> None:
    text = "Оплатите заявку пожалуйста: 10009 @PolinaUser"
    message = SimpleNamespace(text=text, caption=None, photo=None, document=None, video=None)
    targets = [SimpleNamespace(identity="polinauser", display_name="@polinauser", username="polinauser")]

    assert message_requires_response(message, targets)


def test_message_requires_response_for_question_with_mention() -> None:
    text = "@Norblacksmith когда будут лампочки?"
    message = SimpleNamespace(text=text, caption=None, photo=None, document=None, video=None)
    targets = [SimpleNamespace(identity="norblacksmith", display_name="@norblacksmith", username="norblacksmith")]

    assert message_requires_response(message, targets)


def test_message_without_mention_does_not_start_control_even_if_it_has_request() -> None:
    message = SimpleNamespace(text="Сергей Николаевич, вы можете уточнить?", caption=None, photo=None, document=None, video=None)

    assert not message_requires_response(message, [])


def test_message_with_plain_greeting_mention_does_not_start_control() -> None:
    text = "@Norblacksmith хорошего вечера!"
    message = SimpleNamespace(text=text, caption=None, photo=None, document=None, video=None)
    targets = [SimpleNamespace(identity="norblacksmith", display_name="@norblacksmith", username="norblacksmith")]

    assert not message_requires_response(message, targets)


def test_message_with_mention_without_request_does_not_start_control() -> None:
    text = "@Norblacksmith спасибо за помощь"
    message = SimpleNamespace(text=text, caption=None, photo=None, document=None, video=None)
    targets = [SimpleNamespace(identity="norblacksmith", display_name="@norblacksmith", username="norblacksmith")]

    assert not message_requires_response(message, targets)


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


def test_wait_keyboard_has_only_seen_and_close_buttons() -> None:
    wait = _wait(username="firstuser", display_name="@firstuser", user_id=None)

    keyboard = wait_keyboard(wait)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert labels == ["👀 Вижу", "Закрыть"]


def test_control_buttons_are_limited_to_leader_or_target() -> None:
    wait = _wait(username="firstuser", display_name="@firstuser", user_id=None)
    target = User(id=456, is_bot=False, first_name="First", username="firstuser")
    leader = User(id=999, is_bot=False, first_name="Alex", username="Fedos_AV")
    other = User(id=111, is_bot=False, first_name="Other", username="otheruser")
    settings = SimpleNamespace(leader_username="fedos_av")

    assert can_user_control_waits([wait], target, settings)
    assert can_user_control_waits([wait], leader, settings)
    assert not can_user_control_waits([wait], other, settings)


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


def test_single_source_waits_returns_source_group_only_when_one_question_is_active() -> None:
    first_wait = _wait(username="firstuser", display_name="@firstuser", user_id=None, source_message_id=10)
    second_wait = _wait(username="seconduser", display_name="@seconduser", user_id=None, source_message_id=10)

    assert single_source_waits([first_wait, second_wait]) == [first_wait, second_wait]


def test_single_source_waits_keeps_multiple_questions_open() -> None:
    first_wait = _wait(username="firstuser", display_name="@firstuser", user_id=None, source_message_id=10)
    second_wait = _wait(username="firstuser", display_name="@firstuser", user_id=None, source_message_id=20)

    assert single_source_waits([first_wait, second_wait]) == []


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


def test_wait_matches_username_when_only_trailing_digits_changed() -> None:
    wait = _wait(username="lalalas19", display_name="@lalalas19", user_id=None)
    user = User(id=456, is_bot=False, first_name="Rafael", username="Lalalas")

    assert wait_matches_telegram_user(wait, user)


def test_wait_does_not_match_short_username_stem_with_different_digits() -> None:
    wait = _wait(username="alex1", display_name="@alex1", user_id=None)
    user = User(id=456, is_bot=False, first_name="Alex", username="alex2")

    assert not wait_matches_telegram_user(wait, user)


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
        seen_at=None,
        seen_by_user_id=None,
        last_intermediate_at=None,
        reason_requested_at=None,
        reason_due_at=None,
        delay_reason=None,
        delay_reason_at=None,
        leader_request_sent_at=None,
        status="active",
    )
