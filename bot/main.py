from __future__ import annotations

import asyncio
import csv
import io
import html
import logging
import re
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageReactionUpdated,
    ReplyParameters,
    User,
)

from bot.config import Settings, load_settings
from bot.storage import DailyReportItem, FineReportDetail, PendingWait, Storage
from bot.telegram_utils import (
    MentionTarget,
    build_message_link,
    display_username,
    extract_mention_targets,
    short_quote,
    source_reference,
)
from bot.worktime import add_working_minutes, is_work_time, next_work_time

logger = logging.getLogger(__name__)
router = Router()
ALLOWED_UPDATES = ["message", "callback_query", "message_reaction"]

UNCLEAR_RESPONSE_PATTERNS = (
    r"^ок(?:ей)?[.!]?$",
    r"^ща[.!]?$",
    r"^сейчас[.!]?$",
    r"^позже[.!]?$",
    r"^потом[.!]?$",
    r"^посмотрю[.!]?$",
    r"^уточню[.!]?$",
    r"^разберусь[.!]?$",
    r"^принял[.!]?$",
    r"^понял[.!]?$",
    r"^не знаю[.!]?$",
    r"^не в курсе[.!]?$",
)

UNCLEAR_RESPONSE_RE = re.compile("|".join(f"(?:{pattern})" for pattern in UNCLEAR_RESPONSE_PATTERNS), re.IGNORECASE)


def now_in_tz(settings: Settings) -> datetime:
    return datetime.now(settings.timezone)


def is_leader(user: User | None, settings: Settings) -> bool:
    if not user or not user.username:
        return False
    return user.username.lower() == settings.leader_username


def actor_label(user: User | None) -> str:
    if not user:
        return "Пользователь"
    if user.username:
        return html.escape(f"@{user.username}")
    full_name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    return html.escape(full_name or f"user_id:{user.id}")


def commands_help_text(settings: Settings, user: User | None) -> str:
    lines = [
        "Доступные команды:",
        "/start — активировать личные сообщения с ботом.",
        "/help — показать эту подсказку.",
        "/chatid — показать ID текущего чата.",
        "",
        "В рабочем чате также можно ответить на напоминание бота текстом:",
        "закрыть — закрыть обращение.",
        f"отложить — перенести следующее напоминание на {settings.reminder_interval_minutes} минут.",
    ]

    if is_leader(user, settings):
        lines.extend(
            [
                "",
                "Команды руководителя:",
                "/stats — статистика работы бота.",
                "/settings — текущие настройки, которые бот видит на сервере.",
                "/fines — штрафы за текущий месяц с детализацией.",
                "/fines 2026-05 — штрафы за выбранный месяц.",
                "",
                "Запросы на штраф приходят руководителю в личку. После решения бот обновляет третье уведомление в рабочем чате.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Команды /stats, /settings и /fines доступны только руководителю.",
            ]
        )

    return "\n".join(lines)


async def setup_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Активировать личные сообщения"),
            BotCommand(command="help", description="Показать команды бота"),
            BotCommand(command="chatid", description="Показать ID текущего чата"),
            BotCommand(command="stats", description="Статистика для руководителя"),
            BotCommand(command="settings", description="Настройки для руководителя"),
            BotCommand(command="fines", description="Отчёт по штрафам для руководителя"),
        ]
    )


def wait_target_label(wait: PendingWait) -> str:
    if wait.user_id and wait.display_name and wait.display_name != display_username(wait.username):
        name = html.escape(wait.display_name)
        return f'<a href="tg://user?id={wait.user_id}">{name}</a>'
    if wait.username.startswith("user_id:") and wait.user_id:
        name = html.escape(wait.display_name or f"user_id:{wait.user_id}")
        return f'<a href="tg://user?id={wait.user_id}">{name}</a>'
    if wait.username.startswith("user_id:"):
        return html.escape(wait.display_name or wait.username)
    return display_username(wait.username)


def item_target_label(item: DailyReportItem) -> str:
    if item.user_id and item.display_name and item.display_name != display_username(item.username):
        name = html.escape(item.display_name)
        return f'<a href="tg://user?id={item.user_id}">{name}</a>'
    if item.username.startswith("user_id:") and item.user_id:
        name = html.escape(item.display_name or f"user_id:{item.user_id}")
        return f'<a href="tg://user?id={item.user_id}">{name}</a>'
    if item.username.startswith("user_id:"):
        return html.escape(item.display_name or item.username)
    return display_username(item.username)


def wait_targets_label(waits: list[PendingWait]) -> str:
    seen: set[str] = set()
    labels: list[str] = []
    for wait in waits:
        key = wait.username
        if key in seen:
            continue
        seen.add(key)
        labels.append(wait_target_label(wait))
    return ", ".join(labels)


def wait_keyboard(wait: PendingWait) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="Закрыть", callback_data=f"wait:close:{wait.id}"),
            InlineKeyboardButton(text="Отложить", callback_data=f"wait:snooze:{wait.id}"),
        ]
    ]
    if wait.source_message_link:
        buttons.append([InlineKeyboardButton(text="Открыть сообщение", url=wait.source_message_link)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def source_link_keyboard(wait: PendingWait) -> InlineKeyboardMarkup | None:
    if not wait.source_message_link:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть сообщение", url=wait.source_message_link)]]
    )


def answer_confirmation_keyboard(wait: PendingWait) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Считать ответом", callback_data=f"wait:confirmclose:{wait.id}"),
                InlineKeyboardButton(text="Продолжить контроль", callback_data=f"wait:keep:{wait.id}"),
            ]
        ]
    )


def fine_decision_keyboard(wait: PendingWait, fine_amount: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text=f"Выписать штраф {fine_amount} ₽", callback_data=f"wait:fine:{wait.id}"),
            InlineKeyboardButton(text="Не штрафовать", callback_data=f"wait:nofine:{wait.id}"),
        ]
    ]
    if wait.source_message_link:
        buttons.append([InlineKeyboardButton(text="Открыть сообщение", url=wait.source_message_link)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def normalized_message_text(message: Message) -> str:
    text = message.text or message.caption or ""
    return re.sub(r"\s+", " ", text).strip()


def normalized_match_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("ё", "е").casefold()).strip()


def text_contains_standalone_name(text: str, name: str) -> bool:
    normalized_text = normalized_match_text(text)
    normalized_name = normalized_match_text(name)
    if len(normalized_name) < 3:
        return False
    pattern = rf"(?<![\w@]){re.escape(normalized_name)}(?![\w@])"
    return re.search(pattern, normalized_text) is not None


def mention_target_from_known_user(user: object, display_name: str) -> MentionTarget:
    username = user["username_lower"] if user["username_lower"] else None
    return MentionTarget(
        identity=username or f"user_id:{user['user_id']}",
        display_name=display_name,
        username=username,
        user_id=user["user_id"],
        first_name=user["first_name"],
        last_name=user["last_name"],
    )


def add_mention_target(
    targets: dict[str, MentionTarget],
    target: MentionTarget,
    *,
    sender_id: int | None,
    sender_username: str | None,
) -> None:
    if sender_id is not None and target.user_id == sender_id:
        return
    if sender_username is not None and target.identity == sender_username:
        return
    targets.setdefault(target.identity, target)


async def extend_targets_from_known_names(
    app_storage: Storage,
    message: Message,
    targets: list[MentionTarget],
) -> list[MentionTarget]:
    text = normalized_message_text(message)
    if not text:
        return targets

    sender_id = message.from_user.id if message.from_user else None
    sender_username = message.from_user.username.lower() if message.from_user and message.from_user.username else None
    targets_by_identity = {target.identity: target for target in targets}

    known_users = await app_storage.known_users_for_matching()
    first_name_counts: dict[str, int] = {}
    for user in known_users:
        first_name = user["first_name"]
        if not first_name:
            continue
        normalized_first_name = normalized_match_text(first_name)
        if len(normalized_first_name) >= 3:
            first_name_counts[normalized_first_name] = first_name_counts.get(normalized_first_name, 0) + 1

    for user in known_users:
        first_name = user["first_name"]
        last_name = user["last_name"]
        full_name = " ".join(part for part in (first_name, last_name) if part).strip()
        matched_label: str | None = None

        if full_name and text_contains_standalone_name(text, full_name):
            matched_label = full_name
        elif first_name:
            normalized_first_name = normalized_match_text(first_name)
            if first_name_counts.get(normalized_first_name) == 1 and text_contains_standalone_name(text, first_name):
                matched_label = first_name

        if not matched_label and user["username_lower"] and text_contains_standalone_name(text, user["username_lower"]):
            matched_label = display_username(user["username_lower"])

        if matched_label:
            add_mention_target(
                targets_by_identity,
                mention_target_from_known_user(user, matched_label),
                sender_id=sender_id,
                sender_username=sender_username,
            )

    active_waits = await app_storage.active_waits_in_chat(chat_id=message.chat.id)
    for wait in active_waits:
        labels = [wait.display_name] if wait.display_name else []
        if not wait.username.startswith("user_id:"):
            labels.append(wait.username)

        matched_label = next(
            (
                label
                for label in labels
                if label and not label.startswith("@") and text_contains_standalone_name(text, label)
            ),
            None,
        )
        if not matched_label:
            continue

        username = wait.username if not wait.username.startswith("user_id:") else None
        add_mention_target(
            targets_by_identity,
            MentionTarget(
                identity=wait.username,
                display_name=matched_label,
                username=username,
                user_id=wait.user_id,
            ),
            sender_id=sender_id,
            sender_username=sender_username,
        )

    return sorted(targets_by_identity.values(), key=lambda target: target.display_name.lower())


def is_unclear_employee_response(message: Message) -> bool:
    text = normalized_message_text(message)
    if not text:
        return False

    normalized = text.lower().replace("ё", "е")
    if UNCLEAR_RESPONSE_RE.match(normalized):
        return True

    unclear_fragments = (
        "позже",
        "потом",
        "посмотрю",
        "уточню",
        "разберусь",
        "отпишусь",
        "не мой вопрос",
        "не ко мне",
        "не знаю",
        "не в курсе",
    )
    return any(fragment in normalized for fragment in unclear_fragments)


def is_meaningful_employee_response(message: Message) -> bool:
    text = normalized_message_text(message)
    if not text:
        return bool(message.photo or message.document or message.video or message.voice or message.audio)
    return not is_unclear_employee_response(message)


SOURCE_MATCH_STOPWORDS = {
    "если",
    "или",
    "как",
    "когда",
    "который",
    "которая",
    "которые",
    "нужен",
    "нужна",
    "нужно",
    "нужны",
    "ответ",
    "пожалуйста",
    "сегодня",
    "сообщение",
    "сообщению",
    "через",
    "что",
    "это",
    "этот",
    "этому",
    "есть",
    "будет",
    "будут",
    "надо",
    "еще",
    "ещё",
}


RUSSIAN_MATCH_ENDINGS = (
    "ами",
    "ями",
    "ого",
    "ему",
    "ыми",
    "ими",
    "ых",
    "их",
    "ую",
    "юю",
    "ая",
    "яя",
    "ое",
    "ее",
    "ые",
    "ие",
    "ой",
    "ей",
    "ом",
    "ем",
    "ам",
    "ям",
    "ах",
    "ях",
    "ку",
    "ке",
    "ка",
    "ки",
    "ый",
    "ий",
)


def token_match_forms(token: str) -> set[str]:
    forms = {token}
    if not re.fullmatch(r"[а-я]+", token) or len(token) < 6:
        return forms

    forms.add(token[:-1])
    for ending in RUSSIAN_MATCH_ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            forms.add(token[: -len(ending)])
    return forms


def important_tokens(value: str) -> set[str]:
    normalized = normalized_match_text(value)
    tokens = re.findall(r"[0-9a-zа-я_]+", normalized)
    result: set[str] = set()
    for token in tokens:
        if token in SOURCE_MATCH_STOPWORDS:
            continue
        if token.isdigit() and len(token) >= 3:
            result.add(token)
        elif len(token) >= 4:
            result.update(token_match_forms(token))
    return result


def source_answer_match_score(message: Message, wait: PendingWait) -> int:
    text = normalized_message_text(message)
    if not text:
        return 0

    response_tokens = important_tokens(text)
    source_tokens = important_tokens(wait.source_quote)
    shared_tokens = response_tokens & source_tokens
    if not shared_tokens:
        return 0

    score = len(shared_tokens)
    score += sum(2 for token in shared_tokens if any(char.isdigit() for char in token))
    return score


def quoted_source_match_score(reply_message: Message, wait: PendingWait) -> int:
    replied_text = normalized_message_text(reply_message)
    if not replied_text:
        return 0

    replied_tokens = important_tokens(replied_text)
    source_tokens = important_tokens(wait.source_quote)
    shared_tokens = replied_tokens & source_tokens
    if not shared_tokens:
        return 0

    score = len(shared_tokens)
    score += sum(2 for token in shared_tokens if any(char.isdigit() for char in token))
    return score


def group_waits_by_source(waits: list[PendingWait]) -> list[list[PendingWait]]:
    groups: dict[int, list[PendingWait]] = {}
    for wait in waits:
        groups.setdefault(wait.source_message_id, []).append(wait)
    return [sorted(group, key=lambda item: item.id) for group in groups.values()]


def select_waits_answered_by_message(waits: list[PendingWait], message: Message) -> list[PendingWait]:
    source_groups = group_waits_by_source(waits)
    if len(source_groups) == 1:
        return source_groups[0]

    scored_groups: list[tuple[int, list[PendingWait]]] = []
    for group in source_groups:
        score = max(source_answer_match_score(message, wait) for wait in group)
        if score > 0:
            scored_groups.append((score, group))

    if not scored_groups:
        return []

    best_score = max(score for score, _ in scored_groups)
    best_groups = [group for score, group in scored_groups if score == best_score]
    if len(best_groups) == 1:
        return best_groups[0]
    return []


async def active_waits_for_reply(
    app_storage: Storage,
    message: Message,
) -> list[PendingWait]:
    reply = message.reply_to_message
    if not reply:
        return []

    by_source = await app_storage.active_waits_for_source_message(
        chat_id=message.chat.id,
        source_message_id=reply.message_id,
    )
    if by_source:
        return by_source

    by_reminder = await app_storage.active_waits_for_reminder_message(
        chat_id=message.chat.id,
        reminder_message_id=reply.message_id,
    )
    if by_reminder:
        source_message_ids = [wait.source_message_id for wait in by_reminder]
        source_waits = await app_storage.active_waits_for_source_messages(
            chat_id=message.chat.id,
            source_message_ids=source_message_ids,
        )
        return source_waits or by_reminder

    active_waits = await app_storage.active_waits_in_chat(chat_id=message.chat.id)
    scored_groups: list[tuple[int, list[PendingWait]]] = []
    for group in group_waits_by_source(active_waits):
        score = max(quoted_source_match_score(reply, wait) for wait in group)
        if score >= 2:
            scored_groups.append((score, group))

    if not scored_groups:
        return []

    best_score = max(score for score, _ in scored_groups)
    best_groups = [group for score, group in scored_groups if score == best_score]
    if len(best_groups) == 1:
        return best_groups[0]
    return []


def is_direct_message_unreachable(wait: PendingWait) -> bool:
    return wait.direct_message_attempted_at is not None and wait.direct_message_sent_at is None


async def safe_callback_answer(callback: CallbackQuery, text: str, *, show_alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as error:
        logger.warning("Cannot answer callback %s: %s", callback.id, error)


def format_local_dt(value: datetime, settings: Settings) -> str:
    return value.astimezone(settings.timezone).strftime("%d.%m %H:%M")


def timezone_label(settings: Settings) -> str:
    if settings.timezone_name == "Europe/Moscow":
        return "МСК"
    return settings.timezone_name


def format_event_dt(value: datetime, settings: Settings) -> str:
    return f"{format_local_dt(value, settings)} {timezone_label(settings)}"


def plural_ru(value: int, forms: tuple[str, str, str]) -> str:
    remainder_100 = value % 100
    remainder_10 = value % 10
    if 11 <= remainder_100 <= 14:
        form = forms[2]
    elif remainder_10 == 1:
        form = forms[0]
    elif 2 <= remainder_10 <= 4:
        form = forms[1]
    else:
        form = forms[2]
    return f"{value} {form}"


def format_elapsed(start_at: datetime, end_at: datetime) -> str:
    elapsed_seconds = max(0, int((end_at - start_at).total_seconds()))
    elapsed_minutes = elapsed_seconds // 60
    if elapsed_minutes < 1:
        return "меньше минуты"

    elapsed_hours = elapsed_minutes // 60
    minutes = elapsed_minutes % 60
    elapsed_days = elapsed_hours // 24
    hours = elapsed_hours % 24

    if elapsed_days:
        parts = [plural_ru(elapsed_days, ("день", "дня", "дней"))]
        if hours:
            parts.append(plural_ru(hours, ("час", "часа", "часов")))
        if not hours and minutes:
            parts.append(plural_ru(minutes, ("минута", "минуты", "минут")))
        return " ".join(parts)

    if elapsed_hours:
        parts = [plural_ru(elapsed_hours, ("час", "часа", "часов"))]
        if minutes:
            parts.append(plural_ru(minutes, ("минута", "минуты", "минут")))
        return " ".join(parts)

    return plural_ru(elapsed_minutes, ("минута", "минуты", "минут"))


def report_period(now: datetime, settings: Settings) -> tuple[date, datetime, datetime]:
    local_now = now.astimezone(settings.timezone)
    report_date = local_now.date() - timedelta(days=1)
    start_at = datetime.combine(report_date, datetime.min.time(), tzinfo=settings.timezone)
    end_at = start_at + timedelta(days=1)
    return report_date, start_at, end_at


def daily_report_is_due(now: datetime, settings: Settings) -> bool:
    local_now = now.astimezone(settings.timezone)
    report_at = datetime.combine(local_now.date(), settings.daily_report_time, tzinfo=settings.timezone)
    return local_now >= report_at


def daily_report_text(report_date: date, items: list[DailyReportItem]) -> str:
    title = f"Отчёт за {report_date.strftime('%d.%m.%Y')}"
    if not items:
        return f"{title}\nНезакрытых обращений за прошедший день нет."

    lines = [title, "Незакрытые обращения:"]
    max_items = 20
    for index, item in enumerate(items[:max_items], start=1):
        reference = source_reference(item.source_message_link, item.source_quote)
        status_parts = [f"напоминаний: {item.reminder_count}"]
        if item.direct_message_sent_at:
            status_parts.append("личка отправлена")
        if item.group_reminders_stopped_at:
            status_parts.append("групповые напоминания остановлены")
        lines.append(f"{index}. {item_target_label(item)} не ответил по сообщению: {reference} ({', '.join(status_parts)})")

    if len(items) > max_items:
        lines.append(f"Ещё {len(items) - max_items} незакрытых обращений не показано в этом сообщении.")

    return "\n".join(lines)



def month_period(now: datetime, settings: Settings, month_value: str | None = None) -> tuple[datetime, datetime]:
    if month_value:
        year_raw, month_raw = month_value.split("-", maxsplit=1)
        year = int(year_raw)
        month = int(month_raw)
        if month < 1 or month > 12:
            raise ValueError
        start_at = datetime(year, month, 1, tzinfo=settings.timezone)
    else:
        local_now = now.astimezone(settings.timezone)
        start_at = datetime(local_now.year, local_now.month, 1, tzinfo=settings.timezone)

    if start_at.month == 12:
        end_at = datetime(start_at.year + 1, 1, 1, tzinfo=settings.timezone)
    else:
        end_at = datetime(start_at.year, start_at.month + 1, 1, tzinfo=settings.timezone)
    return start_at, end_at


def fine_target_label(item: FineReportDetail) -> str:
    if item.user_id and item.display_name and item.display_name != display_username(item.username):
        name = html.escape(item.display_name)
        return f'<a href="tg://user?id={item.user_id}">{name}</a>'
    if item.username.startswith("user_id:") and item.user_id:
        name = html.escape(item.display_name or f"user_id:{item.user_id}")
        return f'<a href="tg://user?id={item.user_id}">{name}</a>'
    if item.username.startswith("user_id:"):
        return html.escape(item.display_name or item.username)
    return display_username(item.username)


def fine_report_text(month_start: datetime, items: list[FineReportDetail], settings: Settings) -> str:
    title = f"Отчёт по штрафам за {month_start.strftime('%m.%Y')}"
    if not items:
        return f"{title}\nНачисленных штрафов нет."

    total_amount = sum(item.amount_rubles for item in items)
    total_count = len(items)
    lines = [
        title,
        f"Всего: {total_amount} ₽ ({plural_ru(total_count, ('штраф', 'штрафа', 'штрафов'))})",
        "Детализация:",
    ]
    max_items = 25
    for index, item in enumerate(items[:max_items], start=1):
        chat_title = html.escape(item.chat_title or f"chat_id:{item.chat_id}")
        reference = source_reference(item.source_message_link, item.source_quote)
        lines.extend(
            [
                f"{index}. {fine_target_label(item)} — {item.amount_rubles} ₽",
                f"Когда: {format_event_dt(item.decided_at, settings)}",
                f"Чат: {chat_title}",
                f"За что: {reference}",
            ]
        )
    if len(items) > max_items:
        lines.append(f"Ещё {len(items) - max_items} штрафов не показано в сообщении. Полная детализация есть в CSV-файле.")
    return "\n".join(lines)


def fine_report_csv(month_start: datetime, items: list[FineReportDetail]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "month",
        "decided_at",
        "username",
        "display_name",
        "user_id",
        "chat_id",
        "chat_title",
        "amount_rubles",
        "source_quote",
        "source_message_link",
    ])
    for item in items:
        writer.writerow([
            month_start.strftime("%Y-%m"),
            item.decided_at.isoformat(),
            item.username,
            item.display_name or "",
            item.user_id or "",
            item.chat_id,
            item.chat_title or "",
            item.amount_rubles,
            item.source_quote,
            item.source_message_link or "",
        ])
    return buffer.getvalue().encode("utf-8-sig")

def leader_daily_report_text(report_date: date, items_by_chat: dict[int, list[DailyReportItem]]) -> str:
    title = f"Общий отчёт за {report_date.strftime('%d.%m.%Y')}"
    all_items = [item for items in items_by_chat.values() for item in items]
    if not all_items:
        return f"{title}\nНезакрытых обращений за прошедший день нет."

    lines = [
        title,
        f"Незакрытых обращений: {len(all_items)}",
    ]
    max_items_per_chat = 10
    for chat_id, items in items_by_chat.items():
        if not items:
            continue
        chat_title = items[0].chat_title or str(chat_id)
        lines.append("")
        lines.append(f"Чат: {html.escape(chat_title)}")
        for index, item in enumerate(items[:max_items_per_chat], start=1):
            reference = source_reference(item.source_message_link, item.source_quote)
            lines.append(f"{index}. {item_target_label(item)} не ответил: {reference}")
        if len(items) > max_items_per_chat:
            lines.append(f"Ещё {len(items) - max_items_per_chat} обращений не показано.")

    return "\n".join(lines)


async def edit_reminder_closed(bot: Bot, wait: PendingWait, text: str) -> None:
    if not wait.last_reminder_message_id:
        return

    try:
        await bot.edit_message_text(
            chat_id=wait.chat_id,
            message_id=wait.last_reminder_message_id,
            text=text,
            disable_web_page_preview=True,
            parse_mode="HTML",
        )
    except TelegramBadRequest as error:
        logger.warning("Cannot edit closed reminder %s: %s", wait.last_reminder_message_id, error)
        try:
            await bot.edit_message_reply_markup(
                chat_id=wait.chat_id,
                message_id=wait.last_reminder_message_id,
                reply_markup=None,
            )
        except TelegramAPIError:
            logger.exception("Cannot remove keyboard from reminder %s", wait.last_reminder_message_id)
    except TelegramAPIError:
        logger.exception("Cannot mark reminder %s as closed", wait.last_reminder_message_id)


async def mark_waits_closed(
    bot: Bot,
    waits: list[PendingWait],
    closed_by: str,
    reason: str,
    action_at: datetime,
    settings: Settings,
    *,
    time_label: str = "Время реакции",
) -> None:
    edited_message_ids: set[int] = set()
    for wait in waits:
        if not wait.last_reminder_message_id or wait.last_reminder_message_id in edited_message_ids:
            continue
        edited_message_ids.add(wait.last_reminder_message_id)
        same_message_waits = [
            item for item in waits if item.last_reminder_message_id == wait.last_reminder_message_id
        ]
        if not same_message_waits:
            same_message_waits = [wait]
        source_created_at = min(item.created_at for item in same_message_waits)
        reference = source_reference(wait.source_message_link, wait.source_quote)
        text = (
            f"Закрыто: {closed_by} {reason}.\n"
            f"{time_label}: {format_event_dt(action_at, settings)}\n"
            f"Прошло с обращения: {format_elapsed(source_created_at, action_at)}\n"
            f"Адресаты: {wait_targets_label(same_message_waits)}\n"
            f"Исходное сообщение: {reference}"
        )
        await edit_reminder_closed(bot, wait, text)


async def mark_wait_already_closed(bot: Bot, wait: PendingWait) -> None:
    reference = source_reference(wait.source_message_link, wait.source_quote)
    text = (
        "Закрыто: обращение уже закрыто.\n"
        f"Адресаты: {wait_target_label(wait)}\n"
        f"Исходное сообщение: {reference}"
    )
    await edit_reminder_closed(bot, wait, text)


async def mark_last_reminder_snoozed(bot: Bot, wait: PendingWait, next_at: datetime, settings: Settings) -> None:
    if not wait.last_reminder_message_id:
        return

    reference = source_reference(wait.source_message_link, wait.source_quote)
    text = (
        f"Отложено до {format_local_dt(next_at, settings)}: {wait_targets_label([wait])}.\n"
        f"Исходное сообщение: {reference}"
    )
    try:
        await bot.edit_message_text(
            chat_id=wait.chat_id,
            message_id=wait.last_reminder_message_id,
            text=text,
            disable_web_page_preview=True,
            parse_mode="HTML",
        )
    except TelegramBadRequest as error:
        logger.warning("Cannot edit snoozed reminder %s: %s", wait.last_reminder_message_id, error)
        try:
            await bot.edit_message_reply_markup(
                chat_id=wait.chat_id,
                message_id=wait.last_reminder_message_id,
                reply_markup=None,
            )
        except TelegramAPIError:
            logger.exception("Cannot remove keyboard from snoozed reminder %s", wait.last_reminder_message_id)
    except TelegramAPIError:
        logger.exception("Cannot mark reminder %s as snoozed", wait.last_reminder_message_id)


async def mark_waits_snoozed(bot: Bot, waits: list[PendingWait], next_at: datetime, settings: Settings) -> None:
    edited_message_ids: set[int] = set()
    for wait in waits:
        if not wait.last_reminder_message_id or wait.last_reminder_message_id in edited_message_ids:
            continue
        edited_message_ids.add(wait.last_reminder_message_id)
        same_message_waits = [
            item for item in waits if item.last_reminder_message_id == wait.last_reminder_message_id
        ]
        reference = source_reference(wait.source_message_link, wait.source_quote)
        text = (
            f"Отложено до {format_local_dt(next_at, settings)}: {wait_targets_label(same_message_waits or [wait])}.\n"
            f"Исходное сообщение: {reference}"
        )
        try:
            await bot.edit_message_text(
                chat_id=wait.chat_id,
                message_id=wait.last_reminder_message_id,
                text=text,
                disable_web_page_preview=True,
                parse_mode="HTML",
            )
        except TelegramBadRequest as error:
            logger.warning("Cannot edit snoozed reminder %s: %s", wait.last_reminder_message_id, error)
            try:
                await bot.edit_message_reply_markup(
                    chat_id=wait.chat_id,
                    message_id=wait.last_reminder_message_id,
                    reply_markup=None,
                )
            except TelegramAPIError:
                logger.exception("Cannot remove keyboard from snoozed reminder %s", wait.last_reminder_message_id)
        except TelegramAPIError:
            logger.exception("Cannot mark reminder %s as snoozed", wait.last_reminder_message_id)


async def delete_previous_reminders(bot: Bot, waits: list[PendingWait]) -> None:
    reminder_message_ids = {
        wait.last_reminder_message_id
        for wait in waits
        if wait.last_reminder_message_id is not None
    }
    for reminder_message_id in reminder_message_ids:
        wait = next(item for item in waits if item.last_reminder_message_id == reminder_message_id)
        try:
            await bot.delete_message(chat_id=wait.chat_id, message_id=reminder_message_id)
        except TelegramBadRequest as error:
            logger.warning("Cannot delete previous reminder %s: %s", reminder_message_id, error)
            try:
                await bot.edit_message_reply_markup(
                    chat_id=wait.chat_id,
                    message_id=reminder_message_id,
                    reply_markup=None,
                )
            except TelegramAPIError:
                logger.exception("Cannot remove keyboard from previous reminder %s", reminder_message_id)
        except TelegramAPIError:
            logger.exception("Cannot delete previous reminder %s", reminder_message_id)


async def edit_callback_message(callback: CallbackQuery, text: str) -> None:
    if not callback.message:
        return

    try:
        await callback.message.edit_text(
            text,
            disable_web_page_preview=True,
            reply_markup=None,
            parse_mode="HTML",
        )
    except TelegramBadRequest as error:
        logger.warning("Cannot edit callback message %s: %s", callback.message.message_id, error)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            logger.exception("Cannot remove callback message keyboard %s", callback.message.message_id)
    except TelegramAPIError:
        logger.exception("Cannot edit callback message %s", callback.message.message_id)


async def create_wait_for_target(
    app_storage: Storage,
    message: Message,
    settings: Settings,
    *,
    target: MentionTarget,
    now: datetime,
    source_prefix: str | None = None,
) -> None:
    quote = short_quote(message)
    if source_prefix:
        quote = f"{source_prefix}: {quote}"

    if target.user_id is not None:
        await app_storage.upsert_user(
            user_id=target.user_id,
            username=target.username,
            first_name=target.first_name,
            last_name=target.last_name,
            private_chat_started=False,
            now=now,
        )

    known_user = None
    if target.user_id is not None:
        known_user = await app_storage.get_user_by_id(target.user_id)
    elif target.username:
        known_user = await app_storage.get_user_by_username(target.username)

    await app_storage.upsert_wait(
        chat_id=message.chat.id,
        chat_title=message.chat.title,
        username_lower=target.identity,
        display_name=target.display_name,
        user_id=target.user_id or (known_user["user_id"] if known_user else None),
        source_message_id=message.message_id,
        source_message_link=build_message_link(message),
        source_quote=quote,
        mentioned_by_user_id=message.from_user.id if message.from_user else None,
        now=now,
        next_reminder_at=add_working_minutes(
            now,
            settings.reminder_interval_minutes,
            settings.workday_start,
            settings.workday_end,
            settings.timezone,
        ),
        direct_message_due_at=add_working_minutes(
            now,
            settings.direct_message_after_minutes,
            settings.workday_start,
            settings.workday_end,
            settings.timezone,
        ),
    )
    await app_storage.record_metric(
        "wait_upserted",
        now=now,
        chat_id=message.chat.id,
        username_lower=target.identity,
    )


async def register_user_from_message(app_storage: Storage, message: Message, settings: Settings) -> None:
    if not message.from_user:
        return

    await app_storage.upsert_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        private_chat_started=message.chat.type == ChatType.PRIVATE,
        now=now_in_tz(settings),
    )


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start_private(message: Message, app_storage: Storage, settings: Settings) -> None:
    await register_user_from_message(app_storage, message, settings)
    await message.answer(
        "Готово. Теперь бот знает ваш Telegram user_id и сможет отправить личное "
        "напоминание, если вас ждут в рабочем чате.\n\n"
        "В группе я буду закрывать ожидания автоматически, когда ответ понятен или привязан к исходному сообщению. "
        "Кнопками в напоминаниях может пользоваться любой участник чата, а я зафиксирую, кто нажал.\n\n"
        f"{commands_help_text(settings, message.from_user)}"
    )


@router.message(Command("help"))
async def help_command(message: Message, app_storage: Storage, settings: Settings) -> None:
    await register_user_from_message(app_storage, message, settings)
    await message.answer(commands_help_text(settings, message.from_user))


@router.message(Command("stats"))
async def stats_command(message: Message, app_storage: Storage, settings: Settings) -> None:
    await register_user_from_message(app_storage, message, settings)
    if not is_leader(message.from_user, settings):
        await message.answer("Статистика доступна только руководителю.")
        return

    summary = await app_storage.metrics_summary()
    await message.answer(
        "\n".join(
            [
                "Статистика бота:",
                f"Активные ожидания: {summary.get('active_waits', 0)}",
                f"Создано/обновлено ожиданий: {summary.get('wait_upserted', 0)}",
                f"Закрыто ответом: {summary.get('wait_closed_by_message', 0)}",
                f"Закрыто reply-ом: {summary.get('wait_closed_by_reply', 0)}",
                f"Закрыто реакцией: {summary.get('wait_closed_by_reaction', 0)}",
                f"Закрыто кнопкой: {summary.get('wait_closed_by_button', 0)}",
                f"Закрыто подтверждением: {summary.get('wait_closed_by_confirm_button', 0)}",
                f"Отложено кнопкой: {summary.get('wait_snoozed_by_button', 0)}",
                f"Спорных ответов: {summary.get('wait_response_unclear', 0)}",
                f"Неоднозначных ответов: {summary.get('wait_response_ambiguous', 0)}",
                f"Оставлено под контролем: {summary.get('wait_kept_by_confirm_button', 0)}",
                f"Переадресовано: {summary.get('wait_delegated', 0)}",
                f"Групповых напоминаний: {summary.get('group_reminder_sent', 0)}",
                f"Запрошено решений по штрафу: {summary.get('fine_decision_requested', 0)}",
                f"Штрафов назначено: {summary.get('fine_issued', 0)}",
                f"Штрафов отклонено: {summary.get('fine_declined', 0)}",
                f"Запросов по штрафу отправлено в личку: {summary.get('fine_request_dm_sent', 0)}",
                f"Остановлено после лимита: {summary.get('group_reminders_stopped_after_dm_failure', 0)}",
                f"Эскалаций руководителю: {summary.get('leader_escalation_sent', 0)}",
                f"Личных сообщений отправлено: {summary.get('direct_message_sent', 0)}",
                f"Личных сообщений не отправлено: {summary.get('direct_message_failed', 0)}",
                f"Всего закрытых ожиданий в базе: {summary.get('closed_waits_total', 0)}",
            ]
        )
    )


@router.message(Command("settings"))
async def settings_command(message: Message, app_storage: Storage, settings: Settings) -> None:
    await register_user_from_message(app_storage, message, settings)
    if not is_leader(message.from_user, settings):
        await message.answer("Настройки доступны только руководителю.")
        return

    await message.answer(
        "\n".join(
            [
                "Текущие настройки бота:",
                f"Интервал напоминаний: {settings.reminder_interval_minutes} мин.",
                f"Личное сообщение через: {settings.direct_message_after_minutes} мин.",
                f"Рабочее время: {settings.workday_start.strftime('%H:%M')} - {settings.workday_end.strftime('%H:%M')}",
                f"Часовой пояс: {settings.timezone_name}",
                f"Отчёт каждый день: {settings.daily_report_time.strftime('%H:%M')}",
                f"Руководитель: {display_username(settings.leader_username)}",
                f"Штраф: {settings.fine_amount_rubles} ₽",
                f"База данных: {settings.database_path}",
            ]
        )
    )


@router.message(Command("fines"))
async def fines_command(message: Message, app_storage: Storage, settings: Settings) -> None:
    await register_user_from_message(app_storage, message, settings)
    if not is_leader(message.from_user, settings):
        await message.answer("Отчёт по штрафам доступен только руководителю.")
        return

    parts = (message.text or "").split(maxsplit=1)
    month_value = parts[1].strip() if len(parts) > 1 else None
    try:
        start_at, end_at = month_period(now_in_tz(settings), settings, month_value)
    except ValueError:
        await message.answer("Укажите месяц в формате /fines 2026-05 или просто /fines для текущего месяца.")
        return

    items = await app_storage.fine_details_for_month(start_at=start_at, end_at=end_at)
    await message.answer(
        fine_report_text(start_at, items, settings),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    if items:
        document = BufferedInputFile(
            fine_report_csv(start_at, items),
            filename=f"fines_{start_at.strftime('%Y_%m')}.csv",
        )
        await message.answer_document(document, caption="CSV-выгрузка штрафов за месяц")


@router.message(Command("chatid"))
async def chat_id_command(message: Message, app_storage: Storage, settings: Settings) -> None:
    await register_user_from_message(app_storage, message, settings)
    await message.answer(
        f"Chat ID: `{message.chat.id}`\n"
        f"Тип чата: {message.chat.type}",
        parse_mode="Markdown",
    )


@router.message(F.text.regexp(r"(?i)^\s*(закрыть|отложить)\s*$"))
async def text_action_command(message: Message, bot: Bot, app_storage: Storage, settings: Settings) -> None:
    await register_user_from_message(app_storage, message, settings)
    command_text = (message.text or "").strip().lower()
    reply = message.reply_to_message
    if not reply:
        await message.reply("Ответьте этой командой на сообщение-напоминание бота.")
        return

    wait = await app_storage.get_active_wait_by_reminder_message(
        chat_id=message.chat.id,
        reminder_message_id=reply.message_id,
    )
    if not wait:
        await message.reply("Активное ожидание для этого напоминания уже не найдено.")
        return

    now = now_in_tz(settings)
    if command_text == "закрыть":
        closed_waits = await app_storage.close_waits_for_source_messages(
            chat_id=wait.chat_id,
            source_message_ids=[wait.source_message_id],
            closed_by_user_id=message.from_user.id,
            now=now,
        )
        if closed_waits:
            await app_storage.record_metric(
                "wait_closed_by_text_command",
                now=now,
                chat_id=wait.chat_id,
                username_lower=wait.username,
                wait_id=wait.id,
                value=len(closed_waits),
            )
            await mark_waits_closed(
                bot,
                closed_waits,
                actor_label(message.from_user),
                "закрыл обращение",
                message.date,
                settings,
                time_label="Время закрытия",
            )
        return

    next_at = add_working_minutes(
        now,
        settings.reminder_interval_minutes,
        settings.workday_start,
        settings.workday_end,
        settings.timezone,
    )
    snoozed_waits = await app_storage.reschedule_waits_for_source_messages(
        chat_id=wait.chat_id,
        source_message_ids=[wait.source_message_id],
        next_reminder_at=next_at,
    )
    await app_storage.record_metric(
        "wait_snoozed_by_text_command",
        now=now,
        chat_id=wait.chat_id,
        username_lower=wait.username,
        wait_id=wait.id,
        value=max(len(snoozed_waits), 1),
    )
    await mark_waits_snoozed(bot, snoozed_waits or [wait], next_at, settings)


async def close_waits_after_employee_message(
    bot: Bot,
    app_storage: Storage,
    message: Message,
    waits: list[PendingWait],
    now: datetime,
    *,
    metric_name: str,
    reason: str,
) -> None:
    sender_username = message.from_user.username.lower() if message.from_user and message.from_user.username else None
    sender_id = message.from_user.id if message.from_user else None
    source_message_ids = [wait.source_message_id for wait in waits]
    closed_waits = await app_storage.close_waits_for_source_messages(
        chat_id=message.chat.id,
        source_message_ids=source_message_ids,
        closed_by_user_id=sender_id,
        now=now,
    )
    if not closed_waits:
        return

    await app_storage.record_metric(
        metric_name,
        now=now,
        chat_id=message.chat.id,
        username_lower=sender_username,
        value=len(closed_waits),
    )
    time_label = "Время переадресации" if "переадрес" in reason else "Время ответа"
    await mark_waits_closed(
        bot,
        closed_waits,
        actor_label(message.from_user),
        reason,
        message.date,
        settings,
        time_label=time_label,
    )
    logger.info("Closed %s waits in chat %s for user %s", len(closed_waits), message.chat.id, sender_id)


async def create_delegated_waits(
    app_storage: Storage,
    message: Message,
    settings: Settings,
    targets: list[MentionTarget],
    now: datetime,
    *,
    source_waits_count: int,
) -> None:
    sender_id = message.from_user.id if message.from_user else None
    for target in targets:
        await create_wait_for_target(
            app_storage,
            message,
            settings,
            target=target,
            now=now,
            source_prefix="Переадресовано",
        )
        await app_storage.record_metric(
            "wait_delegated",
            now=now,
            chat_id=message.chat.id,
            username_lower=target.identity,
            value=source_waits_count,
        )
        logger.info("Delegated wait from user %s to %s in chat %s", sender_id, target.identity, message.chat.id)


async def ask_to_confirm_response(
    app_storage: Storage,
    message: Message,
    waits: list[PendingWait],
    now: datetime,
    *,
    metric_name: str,
) -> None:
    for source_waits in group_waits_by_source(waits):
        wait = source_waits[0]
        await message.reply(
            (
                f"{wait_targets_label(source_waits)}, считать это ответом по сообщению: "
                f"{source_reference(wait.source_message_link, wait.source_quote)}"
            ),
            disable_web_page_preview=True,
            parse_mode="HTML",
            reply_markup=answer_confirmation_keyboard(wait),
        )
        for source_wait in source_waits:
            await app_storage.record_metric(
                metric_name,
                now=now,
                chat_id=message.chat.id,
                username_lower=source_wait.username,
                wait_id=source_wait.id,
            )


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def handle_group_message(message: Message, bot: Bot, app_storage: Storage, settings: Settings) -> None:
    await register_user_from_message(app_storage, message, settings)

    now = now_in_tz(settings)
    sender_username = message.from_user.username.lower() if message.from_user and message.from_user.username else None
    sender_id = message.from_user.id if message.from_user else None

    detected_targets = await extend_targets_from_known_names(
        app_storage,
        message,
        extract_mention_targets(message),
    )
    mention_targets = [
        target
        for target in detected_targets
        if not (
            (sender_id is not None and target.user_id == sender_id)
            or (sender_username is not None and target.identity == sender_username)
        )
    ]

    sender_waits = await app_storage.active_waits_for_user(
        chat_id=message.chat.id,
        user_id=sender_id,
        username_lower=sender_username,
    )
    reply_source_waits: list[PendingWait] = []
    if message.reply_to_message:
        reply_source_waits = await active_waits_for_reply(app_storage, message)

    if reply_source_waits and mention_targets:
        await close_waits_after_employee_message(
            bot,
            app_storage,
            message,
            reply_source_waits,
            now,
            metric_name="wait_delegated_from_reply",
            reason="переадресовал вопрос",
        )
        await create_delegated_waits(
            app_storage,
            message,
            settings,
            mention_targets,
            now,
            source_waits_count=len(reply_source_waits),
        )
        return

    if sender_waits and mention_targets:
        delegated_waits = select_waits_answered_by_message(sender_waits, message)
        if delegated_waits:
            await close_waits_after_employee_message(
                bot,
                app_storage,
                message,
                delegated_waits,
                now,
                metric_name="wait_delegated_from_message",
                reason="переадресовал вопрос",
            )
            await create_delegated_waits(
                app_storage,
                message,
                settings,
                mention_targets,
                now,
                source_waits_count=len(delegated_waits),
            )
            return

        for target in mention_targets:
            await create_wait_for_target(
                app_storage,
                message,
                settings,
                target=target,
                now=now,
                source_prefix=None,
            )
            logger.info("Created wait for %s in chat %s; sender waits left active", target.identity, message.chat.id)
        return

    if reply_source_waits and is_meaningful_employee_response(message):
        await close_waits_after_employee_message(
            bot,
            app_storage,
            message,
            reply_source_waits,
            now,
            metric_name="wait_closed_by_reply",
            reason="ответил reply-ом",
        )
        return

    if sender_waits and is_meaningful_employee_response(message):
        answered_waits = select_waits_answered_by_message(sender_waits, message)
        if answered_waits:
            await close_waits_after_employee_message(
                bot,
                app_storage,
                message,
                answered_waits,
                now,
                metric_name="wait_closed_by_message",
                reason="ответил",
            )
            return

        await ask_to_confirm_response(
            app_storage,
            message,
            sender_waits,
            now,
            metric_name="wait_response_ambiguous",
        )
        return

    if sender_waits:
        await ask_to_confirm_response(
            app_storage,
            message,
            sender_waits,
            now,
            metric_name="wait_response_unclear",
        )
        return

    if not mention_targets:
        return

    for target in mention_targets:
        await create_wait_for_target(
            app_storage,
            message,
            settings,
            target=target,
            now=now,
            source_prefix=None,
        )
        logger.info("Created/updated wait for %s in chat %s", target.identity, message.chat.id)


@router.message_reaction()
async def handle_message_reaction(
    event: MessageReactionUpdated,
    bot: Bot,
    app_storage: Storage,
    settings: Settings,
) -> None:
    if not event.user or not event.new_reaction:
        return

    now = now_in_tz(settings)
    await app_storage.upsert_user(
        user_id=event.user.id,
        username=event.user.username,
        first_name=event.user.first_name,
        last_name=event.user.last_name,
        private_chat_started=False,
        now=now,
    )

    waits = await app_storage.active_waits_for_source_message(
        chat_id=event.chat.id,
        source_message_id=event.message_id,
    )
    if not waits:
        return

    username = event.user.username.lower() if event.user.username else None
    is_addressee = any(
        (wait.user_id is not None and wait.user_id == event.user.id)
        or (username is not None and wait.username == username)
        for wait in waits
    )
    if not is_addressee:
        return

    closed_waits = await app_storage.close_waits_for_source_messages(
        chat_id=event.chat.id,
        source_message_ids=[event.message_id],
        closed_by_user_id=event.user.id,
        now=now,
    )
    if not closed_waits:
        return

    await app_storage.record_metric(
        "wait_closed_by_reaction",
        now=now,
        chat_id=event.chat.id,
        username_lower=username,
        value=len(closed_waits),
    )
    await mark_waits_closed(bot, closed_waits, actor_label(event.user), "поставил реакцию", event.date, settings)


@router.callback_query(F.data.startswith("wait:"))
async def wait_callback(callback: CallbackQuery, bot: Bot, app_storage: Storage, settings: Settings) -> None:
    if not callback.data:
        return

    _, action, wait_id_raw = callback.data.split(":", maxsplit=2)
    logger.info("Callback %s for wait %s from user %s", action, wait_id_raw, callback.from_user.id)
    wait = await app_storage.get_wait_by_id(int(wait_id_raw))
    if not wait or wait.status != "active":
        await safe_callback_answer(callback, "Это ожидание уже закрыто.", show_alert=False)
        return

    now = now_in_tz(settings)
    await app_storage.upsert_user(
        user_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
        private_chat_started=False,
        now=now,
    )

    if action in {"fine", "nofine"}:
        if not is_leader(callback.from_user, settings):
            await safe_callback_answer(
                callback,
                f"Решение по штрафу может принять только {display_username(settings.leader_username)}.",
                show_alert=True,
            )
            return

        closed_waits = await app_storage.close_waits_for_source_messages(
            chat_id=wait.chat_id,
            source_message_ids=[wait.source_message_id],
            closed_by_user_id=callback.from_user.id,
            now=now,
        )
        if not closed_waits:
            await safe_callback_answer(callback, "Ожидание уже закрыто.", show_alert=False)
            return

        decision = "issued" if action == "fine" else "declined"
        amount = settings.fine_amount_rubles if action == "fine" else 0
        await app_storage.record_fine_decisions(
            waits=closed_waits,
            decision=decision,
            amount_rubles=amount,
            decided_by_user_id=callback.from_user.id,
            decided_at=now,
        )
        await app_storage.record_metric(
            "fine_issued" if action == "fine" else "fine_declined",
            now=now,
            chat_id=wait.chat_id,
            username_lower=wait.username,
            wait_id=wait.id,
            value=len(closed_waits),
        )

        reference = source_reference(wait.source_message_link, wait.source_quote)
        if action == "fine":
            status_line = f"Штраф назначен: {settings.fine_amount_rubles} ₽"
            if len(closed_waits) > 1:
                status_line += " каждому адресату"
        else:
            status_line = "Штраф не назначен"
        group_text = (
            f"{status_line}.\n"
            f"Решение принял: {actor_label(callback.from_user)}\n"
            f"Время решения: {format_event_dt(now, settings)}\n"
            f"Адресаты: {wait_targets_label(closed_waits)}\n"
            f"Исходное сообщение: {reference}"
        )
        private_text = (
            f"Решение сохранено.\n"
            f"{status_line}.\n"
            f"Я обновил третье уведомление в рабочей группе.\n"
            f"Время решения: {format_event_dt(now, settings)}\n"
            f"Адресаты: {wait_targets_label(closed_waits)}\n"
            f"Исходное сообщение: {reference}\n"
            f"Сумму и детализацию штрафов за месяц можно посмотреть командой /fines."
        )
        await edit_reminder_closed(bot, closed_waits[0], group_text)
        await edit_callback_message(callback, private_text)
        await safe_callback_answer(callback, "Решение сохранено, сообщение в группе обновлено.")
        return

    if action in {"close", "confirmclose"}:
        closed_waits = await app_storage.close_waits_for_source_messages(
            chat_id=wait.chat_id,
            source_message_ids=[wait.source_message_id],
            closed_by_user_id=callback.from_user.id,
            now=now,
        )
        if closed_waits:
            await app_storage.record_metric(
                "wait_closed_by_confirm_button" if action == "confirmclose" else "wait_closed_by_button",
                now=now,
                chat_id=wait.chat_id,
                username_lower=wait.username,
                wait_id=wait.id,
                value=len(closed_waits),
            )
            await safe_callback_answer(callback, "Ожидание закрыто.")
            await mark_waits_closed(
                bot,
                closed_waits,
                actor_label(callback.from_user),
                "подтвердил ответ" if action == "confirmclose" else "закрыл обращение",
                now,
                settings,
                time_label="Время подтверждения" if action == "confirmclose" else "Время закрытия",
            )
            if action == "confirmclose":
                await edit_callback_message(
                    callback,
                    (
                        f"Закрыто: {actor_label(callback.from_user)} подтвердил, что это ответ.\n"
                        f"Время реакции: {format_event_dt(now, settings)}"
                    ),
                )
        else:
            await safe_callback_answer(callback, "Ожидание уже закрыто.", show_alert=False)
        return

    if action == "keep":
        await app_storage.record_metric(
            "wait_kept_by_confirm_button",
            now=now,
            chat_id=wait.chat_id,
            username_lower=wait.username,
            wait_id=wait.id,
        )
        await edit_callback_message(callback, f"Ок, продолжаю контроль: {wait_target_label(wait)}.")
        await safe_callback_answer(callback, "Продолжаю контроль.")
        return

    if action == "snooze":
        next_at = add_working_minutes(
            now,
            settings.reminder_interval_minutes,
            settings.workday_start,
            settings.workday_end,
            settings.timezone,
        )
        snoozed_waits = await app_storage.reschedule_waits_for_source_messages(
            chat_id=wait.chat_id,
            source_message_ids=[wait.source_message_id],
            next_reminder_at=next_at,
        )
        await app_storage.record_metric(
            "wait_snoozed_by_button",
            now=now,
            chat_id=wait.chat_id,
            username_lower=wait.username,
            wait_id=wait.id,
            value=max(len(snoozed_waits), 1),
        )
        await mark_waits_snoozed(bot, snoozed_waits or [wait], next_at, settings)
        await safe_callback_answer(callback, "Отложено до следующего окна напоминания.")
        return

    await safe_callback_answer(callback, "Неизвестное действие.", show_alert=True)


async def notify_leader(
    bot: Bot,
    app_storage: Storage,
    settings: Settings,
    text: str,
    now: datetime,
    wait: PendingWait | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    leader = await app_storage.get_user_by_username(settings.leader_username)
    if not leader or not leader["private_chat_started"]:
        logger.warning("Cannot notify leader @%s: private chat is not activated", settings.leader_username)
        await app_storage.record_metric("leader_notification_failed", now=now, wait_id=wait.id if wait else None)
        return False

    try:
        await bot.send_message(
            chat_id=leader["user_id"],
            text=text,
            disable_web_page_preview=True,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except TelegramAPIError:
        logger.exception("Cannot notify leader @%s", settings.leader_username)
        await app_storage.record_metric("leader_notification_failed", now=now, wait_id=wait.id if wait else None)
        return False

    await app_storage.record_metric("leader_notification_sent", now=now, wait_id=wait.id if wait else None)
    return True


async def send_group_reminder(
    bot: Bot,
    app_storage: Storage,
    settings: Settings,
    waits: list[PendingWait],
    now: datetime,
) -> None:
    if not waits:
        return

    source_waits = sorted(waits, key=lambda item: item.id)
    wait = source_waits[0]
    target_labels = wait_targets_label(source_waits)
    reminder_count = max(item.reminder_count for item in source_waits)
    dm_unreachable = any(is_direct_message_unreachable(item) for item in source_waits)
    dm_failure_limit = settings.max_group_reminders_if_dm_unreachable
    limit_applies = dm_unreachable and dm_failure_limit > 0

    if limit_applies and reminder_count >= dm_failure_limit:
        for item in source_waits:
            await app_storage.stop_group_reminders(item.id, now)
            await app_storage.record_metric(
                "group_reminders_stopped_after_dm_failure",
                now=now,
                chat_id=item.chat_id,
                username_lower=item.username,
                wait_id=item.id,
            )
        logger.info("Stopped group reminders for source message %s after DM failure limit", wait.source_message_id)
        return

    reference = source_reference(wait.source_message_link, wait.source_quote)
    next_reminder_number = reminder_count + 1
    is_fine_warning = next_reminder_number == 2
    is_fine_decision = next_reminder_number >= 3
    is_final_after_dm_failure = limit_applies and next_reminder_number >= dm_failure_limit

    if is_fine_decision:
        text = (
            f"{target_labels}, третье напоминание по сообщению: {reference}\n"
            f"Запрос на решение по штрафу отправлен руководителю {display_username(settings.leader_username)} в личку."
        )
        reply_markup = source_link_keyboard(wait)
    elif is_fine_warning:
        text = (
            f"{target_labels}, второе напоминание: нужен ответ по сообщению: {reference}\n"
            f"Если ответа не будет в течение {settings.reminder_interval_minutes} минут, "
            f"я отправлю руководителю запрос на штраф {settings.fine_amount_rubles} ₽."
        )
        reply_markup = wait_keyboard(wait)
    elif is_final_after_dm_failure:
        text = (
            f"{target_labels}, финальное напоминание по сообщению: {reference}\n"
            "Пожалуйста, не оставляйте обращения коллег без ответа: "
            "в рабочих чатах это недопустимо и задерживает работу команды."
        )
        reply_markup = wait_keyboard(wait)
    else:
        text = f"{target_labels}, нужен ответ по сообщению: {reference}"
        reply_markup = wait_keyboard(wait)

    if not is_fine_decision and not is_final_after_dm_failure and reminder_count >= settings.escalate_after_reminders:
        text += f"\n{display_username(settings.leader_username)}, подключитесь, пожалуйста."
        for item in source_waits:
            await app_storage.record_metric(
                "leader_escalation_sent",
                now=now,
                chat_id=item.chat_id,
                username_lower=item.username,
                wait_id=item.id,
            )

    await delete_previous_reminders(bot, source_waits)

    sent_message = await bot.send_message(
        chat_id=wait.chat_id,
        text=text,
        disable_web_page_preview=True,
        reply_markup=reply_markup,
        parse_mode="HTML",
        reply_parameters=ReplyParameters(
            message_id=wait.source_message_id,
            allow_sending_without_reply=True,
        ),
    )

    next_at = add_working_minutes(
        now,
        settings.reminder_interval_minutes,
        settings.workday_start,
        settings.workday_end,
        settings.timezone,
    )
    for item in source_waits:
        await app_storage.mark_group_reminded(
            item.id,
            reminder_message_id=sent_message.message_id,
            next_reminder_at=next_at,
        )
        await app_storage.record_metric(
            "group_reminder_sent",
            now=now,
            chat_id=item.chat_id,
            username_lower=item.username,
            wait_id=item.id,
        )
    if is_fine_decision:
        chat_title = html.escape(wait.chat_title or str(wait.chat_id))
        leader_request_text = (
            f"Нужно решение по штрафу.\n"
            f"Чат: {chat_title}\n"
            f"Кто не отвечает: {target_labels}\n"
            f"Сумма штрафа: {settings.fine_amount_rubles} ₽\n"
            f"Исходное сообщение: {reference}"
        )
        leader_request_sent = await notify_leader(
            bot,
            app_storage,
            settings,
            leader_request_text,
            now,
            wait,
            reply_markup=fine_decision_keyboard(wait, settings.fine_amount_rubles),
        )
        if not leader_request_sent:
            try:
                await bot.edit_message_text(
                    chat_id=wait.chat_id,
                    message_id=sent_message.message_id,
                    text=(
                        f"{target_labels}, третье напоминание по сообщению: {reference}\n"
                        f"Не удалось отправить запрос на решение по штрафу руководителю в личку. "
                        f"{display_username(settings.leader_username)}, проверьте, что вы нажали /start в личке с ботом."
                    ),
                    disable_web_page_preview=True,
                    parse_mode="HTML",
                    reply_markup=source_link_keyboard(wait),
                )
            except TelegramAPIError:
                logger.exception("Cannot edit fine request failure message %s", sent_message.message_id)
        for item in source_waits:
            await app_storage.stop_group_reminders(item.id, now)
            await app_storage.record_metric(
                "fine_decision_requested",
                now=now,
                chat_id=item.chat_id,
                username_lower=item.username,
                wait_id=item.id,
            )
            if leader_request_sent:
                await app_storage.record_metric(
                    "fine_request_dm_sent",
                    now=now,
                    chat_id=item.chat_id,
                    username_lower=item.username,
                    wait_id=item.id,
                )
    elif is_final_after_dm_failure:
        for item in source_waits:
            await app_storage.stop_group_reminders(item.id, now)
            await app_storage.record_metric(
                "group_reminders_stopped_after_dm_failure",
                now=now,
                chat_id=item.chat_id,
                username_lower=item.username,
                wait_id=item.id,
            )


async def send_direct_message_if_needed(
    bot: Bot,
    app_storage: Storage,
    settings: Settings,
    wait: PendingWait,
    now: datetime,
) -> None:
    if wait.direct_message_attempted_at is not None:
        return

    user_id = wait.user_id
    known_user = await app_storage.get_user_by_id(user_id) if user_id is not None else None
    if known_user is None and not wait.username.startswith("user_id:"):
        known_user = await app_storage.get_user_by_username(wait.username)
        if user_id is None and known_user:
            user_id = known_user["user_id"]
            await app_storage.resolve_user_id_for_wait(wait.id, user_id)

    fail_reason: str | None = None
    if user_id is None:
        fail_reason = "бот пока не знает Telegram user_id сотрудника"
    elif not known_user or not known_user["private_chat_started"]:
        fail_reason = "сотрудник не активировал личные сообщения через /start"
    elif not wait.source_message_link:
        fail_reason = "у исходного сообщения нет ссылки"

    if fail_reason:
        logger.info("Cannot DM %s: %s", wait.username, fail_reason)
        await app_storage.mark_direct_message_attempted(wait.id, now)
        await app_storage.record_metric(
            "direct_message_failed",
            now=now,
            chat_id=wait.chat_id,
            username_lower=wait.username,
            wait_id=wait.id,
        )
        await notify_leader(
            bot,
            app_storage,
            settings,
            (
                f"Не удалось отправить личное напоминание {wait_target_label(wait)}: "
                f"{fail_reason}.\nИсточник: {source_reference(wait.source_message_link, wait.source_quote)}"
            ),
            now,
            wait,
        )
        return

    if not wait.source_message_link:
        return

    try:
        await bot.send_message(
            chat_id=user_id,
            text=f"В рабочем чате ждут ваш ответ: {wait.source_message_link}",
            disable_web_page_preview=True,
        )
    except TelegramForbiddenError as error:
        logger.warning("Cannot DM %s (%s): %s", wait.username, user_id, error)
        await app_storage.mark_direct_message_attempted(wait.id, now)
        await app_storage.record_metric(
            "direct_message_failed",
            now=now,
            chat_id=wait.chat_id,
            username_lower=wait.username,
            wait_id=wait.id,
        )
        await notify_leader(
            bot,
            app_storage,
            settings,
            (
                f"Telegram запретил личное напоминание {wait_target_label(wait)}. "
                "Скорее всего, сотрудник не нажал /start в личке с ботом.\n"
                f"Источник: {source_reference(wait.source_message_link, wait.source_quote)}"
            ),
            now,
            wait,
        )
        return
    except TelegramAPIError:
        logger.exception("Telegram API error while sending DM to %s", wait.username)
        await app_storage.mark_direct_message_attempted(wait.id, now)
        await app_storage.record_metric(
            "direct_message_failed",
            now=now,
            chat_id=wait.chat_id,
            username_lower=wait.username,
            wait_id=wait.id,
        )
        return

    await app_storage.mark_direct_message_sent(wait.id, now)
    await app_storage.record_metric(
        "direct_message_sent",
        now=now,
        chat_id=wait.chat_id,
        username_lower=wait.username,
        wait_id=wait.id,
    )


async def send_daily_reports_if_due(bot: Bot, app_storage: Storage, settings: Settings, now: datetime) -> None:
    if not daily_report_is_due(now, settings):
        return

    report_date, start_at, end_at = report_period(now, settings)
    chat_ids = await app_storage.chats_with_waits_created_between(start_at=start_at, end_at=end_at)

    if not await app_storage.leader_daily_report_was_sent(report_date=report_date):
        items_by_chat: dict[int, list[DailyReportItem]] = {}
        for chat_id in chat_ids:
            items_by_chat[chat_id] = await app_storage.unanswered_waits_created_between(
                chat_id=chat_id,
                start_at=start_at,
                end_at=end_at,
            )
        leader_sent = await notify_leader(
            bot,
            app_storage,
            settings,
            leader_daily_report_text(report_date, items_by_chat),
            now,
        )
        if leader_sent:
            await app_storage.mark_leader_daily_report_sent(report_date=report_date, sent_at=now)
            await app_storage.record_metric(
                "leader_daily_report_sent",
                now=now,
                value=1,
            )

    for chat_id in chat_ids:
        if await app_storage.daily_report_was_sent(chat_id=chat_id, report_date=report_date):
            continue

        items = await app_storage.unanswered_waits_created_between(
            chat_id=chat_id,
            start_at=start_at,
            end_at=end_at,
        )
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=daily_report_text(report_date, items),
                disable_web_page_preview=True,
                parse_mode="HTML",
            )
        except TelegramAPIError:
            logger.exception("Cannot send daily report to chat %s", chat_id)
            continue

        await app_storage.mark_daily_report_sent(chat_id=chat_id, report_date=report_date, sent_at=now)
        await app_storage.record_metric(
            "daily_report_sent",
            now=now,
            chat_id=chat_id,
            value=1,
        )


async def scheduler_loop(bot: Bot, app_storage: Storage, settings: Settings) -> None:
    if settings.scheduler_startup_grace_seconds > 0:
        logger.info(
            "Scheduler startup grace: waiting %s seconds before reminders",
            settings.scheduler_startup_grace_seconds,
        )
        await asyncio.sleep(settings.scheduler_startup_grace_seconds)

    while True:
        now = now_in_tz(settings)
        await send_daily_reports_if_due(bot, app_storage, settings, now)
        due_waits = await app_storage.due_waits(now)

        if due_waits and not is_work_time(now, settings.workday_start, settings.workday_end, settings.timezone):
            next_at = next_work_time(now, settings.workday_start, settings.workday_end, settings.timezone)
            for wait in due_waits:
                await app_storage.reschedule_wait(wait.id, next_at)
            await asyncio.sleep(settings.scheduler_tick_seconds)
            continue

        for wait in due_waits:
            now = now_in_tz(settings)
            fresh_wait = await app_storage.get_wait_by_id(wait.id)
            if not fresh_wait or fresh_wait.status != "active":
                continue

            if fresh_wait.direct_message_attempted_at is None and fresh_wait.direct_message_due_at <= now:
                await send_direct_message_if_needed(bot, app_storage, settings, fresh_wait, now)

        processed_sources: set[tuple[int, int]] = set()
        for wait in due_waits:
            key = (wait.chat_id, wait.source_message_id)
            if key in processed_sources:
                continue
            processed_sources.add(key)

            now = now_in_tz(settings)
            source_waits = await app_storage.active_waits_for_source_message(
                chat_id=wait.chat_id,
                source_message_id=wait.source_message_id,
            )
            source_waits = [item for item in source_waits if item.group_reminders_stopped_at is None]
            if not source_waits or not any(item.next_reminder_at <= now for item in source_waits):
                continue

            try:
                await send_group_reminder(bot, app_storage, settings, source_waits, now)
            except TelegramAPIError:
                logger.exception(
                    "Telegram API error while sending group reminder for source message %s in chat %s",
                    wait.source_message_id,
                    wait.chat_id,
                )

        await asyncio.sleep(settings.scheduler_tick_seconds)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    logger.info(
        "Settings loaded: reminder_interval=%s min, direct_message_after=%s min, workday=%s-%s, timezone=%s",
        settings.reminder_interval_minutes,
        settings.direct_message_after_minutes,
        settings.workday_start.strftime("%H:%M"),
        settings.workday_end.strftime("%H:%M"),
        settings.timezone_name,
    )
    storage = Storage(str(settings.database_path))
    await storage.connect()

    bot = Bot(settings.bot_token)
    await setup_bot_commands(bot)
    dispatcher = Dispatcher(app_storage=storage, settings=settings)
    dispatcher.include_router(router)

    scheduler_task = asyncio.create_task(scheduler_loop(bot, storage, settings))
    try:
        logger.info("Allowed updates: %s", ", ".join(ALLOWED_UPDATES))
        await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES)
    finally:
        scheduler_task.cancel()
        await bot.session.close()
        await storage.close()


if __name__ == "__main__":
    asyncio.run(main())
