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
from bot.storage import DailyReportItem, EmployeeStats, FineReportDetail, PendingWait, Storage, WaitEvent
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
                "/stats @username — статистика сотрудника.",
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


def wait_keyboard(wait: PendingWait, settings: Settings | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👀 Вижу", callback_data=f"wait:seen:{wait.id}"),
                InlineKeyboardButton(text="Закрыть", callback_data=f"wait:close:{wait.id}"),
            ]
        ]
    )


def source_link_keyboard(wait: PendingWait) -> InlineKeyboardMarkup | None:
    if not wait.source_message_link:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть сообщение", url=wait.source_message_link)]]
    )


def leader_decision_keyboard(wait: PendingWait, fine_amount: int, *, enable_warning: bool = True) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="✅ Закрыть без штрафа", callback_data=f"wait:closeok:{wait.id}")]]
    if enable_warning:
        buttons.append([InlineKeyboardButton(text="⚠️ Предупреждение", callback_data=f"wait:warning:{wait.id}")])
    buttons.append([InlineKeyboardButton(text=f"💰 Назначить штраф {fine_amount} ₽", callback_data=f"wait:fine:{wait.id}")])
    buttons.append([InlineKeyboardButton(text="🚫 Не штрафовать", callback_data=f"wait:nofine:{wait.id}")])
    if wait.source_message_link:
        buttons.append([InlineKeyboardButton(text="Открыть сообщение", url=wait.source_message_link)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def normalized_message_text(message: Message) -> str:
    text = message.text or message.caption or ""
    return re.sub(r"\s+", " ", text).strip()


def message_requires_response(message: Message, targets: list[MentionTarget]) -> bool:
    if not targets:
        return False

    text = normalized_message_text(message)
    if not text:
        return bool(message.photo or message.document or message.video)

    normalized = normalized_match_text(text)
    mentions_only = normalized
    for target in targets:
        labels = [target.display_name, target.username or "", display_username(target.username) if target.username else ""]
        for label in labels:
            if label:
                mentions_only = mentions_only.replace(normalized_match_text(label), " ")
    mentions_only = re.sub(r"@\w+", " ", mentions_only)
    mentions_only = re.sub(r"\s+", " ", mentions_only).strip(" ,.!?:;—-")

    if not mentions_only:
        return False

    always_requires_response = any(target.identity == "k_kram1" for target in targets)

    if any(phrase in normalized for phrase in NON_REQUEST_PHRASES) and not any(
        phrase in normalized for phrase in RESPONSE_REQUEST_PHRASES
    ) and not always_requires_response:
        return False

    if always_requires_response:
        return True

    if any(phrase in normalized for phrase in RESPONSE_REQUEST_PHRASES):
        return True

    # Vehicle/task-style messages often omit a question mark but still assign work.
    if re.search(r"\b[а-яa-z]\d{3,4}[а-яa-z]{2}\d{2,3}\b", normalized):
        return True

    return False


def normalized_match_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("ё", "е").casefold()).strip()


DELAY_REASONS = {
    "drive": "🚗 Был за рулём",
    "call": "📞 Был на созвоне",
    "client": "👥 Общался с клиентом",
    "urgent": "⚠️ Срочная задача",
    "not_seen": "😴 Не видел сообщение",
    "answered_not_seen": "✅ Ответил, бот не увидел",
    "other": "📝 Другое",
}

AMBIGUOUS_RESPONSE_EXACT = {
    "ок", "окей", "+", "++", "понял", "принял", "ага", "угу", "ща", "сейчас",
    "сек", "мин", "позже", "потом", "добро", "ясно", "вижу", "услышал",
    "на связи", "ладно", "хорошо",
}

INTERMEDIATE_RESPONSE_PHRASES = (
    "смотрю", "посмотрю", "сейчас посмотрю", "гляну", "сейчас гляну",
    "проверяю", "сейчас проверяю", "уточняю", "сейчас уточню", "разбираюсь",
    "сейчас разберусь", "взял в работу", "принял в работу", "сейчас займусь",
    "займусь", "после созвона отвечу", "после встречи отвечу", "после клиента отвечу",
    "после поездки отвечу", "после доставки отвечу", "после обеда отвечу",
    "чуть позже отвечу", "позже отвечу", "скоро отвечу", "вернусь с ответом",
    "дам ответ позже", "дам ответ чуть позже", "нужно время", "нужно проверить",
    "нужно уточнить", "нужно посмотреть", "нужно разобраться", "занят, но вернусь",
    "на созвоне", "на встрече", "за рулем", "за рулём", "с клиентом", "в дороге",
    "сейчас не могу", "понял, проверю", "ок, проверю", "ок, посмотрю",
    "ок, уточню", "ок, разберусь", "принял, проверю", "принял, уточню",
    "принял, посмотрю", "принял, разберусь", "секунду", "минуту",
    "щас посмотрю", "сейчас отпишусь", "сейчас дам ответ", "сейчас будет ответ",
    "пока уточняю", "пока проверяю", "уже смотрю", "уже проверяю",
    "уже занимаюсь", "в процессе", "в работе", "на контроле", "держу в голове",
    "не забыл", "не игнорю", "освобожусь", "дай 10 минут", "дай 15 минут",
    "дай 30 минут", "через 10 минут", "через 15 минут", "через 30 минут",
    "до конца дня отвечу", "сегодня отвечу", "уточню у клиента", "уточню у водителя",
    "уточню у бухгалтера", "уточню у менеджера", "уточню у руководителя",
    "жду информацию", "жду подтверждение", "жду ответ", "жду документы", "жду оплату",
    "жду фото", "жду данные", "запросил информацию", "запросил документы",
    "запросил подтверждение", "сейчас узнаю", "узнаю и отвечу",
    "проверю и отвечу", "посмотрю и отвечу", "разберусь и отвечу",
    "сначала проверю", "сначала уточню", "надо сверить", "надо проверить",
)

FULL_RESPONSE_PHRASES = (
    "сделал", "готово", "выполнил", "отправил", "передал", "проверил",
    "уточнил", "разобрался", "закрыл вопрос", "вопрос закрыт", "задача выполнена",
    "документы отправил", "документы проверил", "документы готовы", "оплату проверил",
    "оплата пришла", "оплаты нет", "деньги поступили", "деньги не поступили",
    "клиенту написал", "клиенту позвонил", "с клиентом связался", "клиент подтвердил",
    "клиент отказался", "клиент ждет", "клиент ждёт", "клиент оплатил",
    "клиент не отвечает", "водителю написал", "водителю позвонил", "водитель подтвердил",
    "водитель не отвечает", "машину проверил", "машина готова", "машина в ремонте",
    "машина на линии", "машина свободна", "машина занята", "фото отправил",
    "видео отправил", "скрин отправил", "ссылку отправил", "файл отправил",
    "договор отправил", "акт отправил", "счет отправил", "счёт отправил",
    "заявку создал", "заявку закрыл", "заявку обновил", "в базу внес", "в базу внёс",
    "в таблицу внес", "в таблицу внёс", "в crm внес", "в crm внёс",
    "исправил", "переделал", "обновил", "созвонился", "договорился",
    "согласовал", "подтвердил", "отменил", "перенес", "перенёс", "записал",
    "назначил", "добавил", "удалил", "заменил", "оплатил", "выставил счет",
    "выставил счёт", "отправил счет", "отправил счёт", "отправил реквизиты",
    "получил подтверждение", "ответил клиенту", "ответил в чат", "ответил в личку",
    "решил вопрос", "проблему решил", "ошибку исправил", "все сделал", "всё сделал",
    "все готово", "всё готово", "все отправил", "всё отправил",
    "все проверил", "всё проверил", "все подтвердил", "всё подтвердил",
    "все согласовано", "всё согласовано", "можно закрывать", "закрывай",
    "готово, закрывай", "да, сделал", "да, отправил", "да, проверил",
    "да, подтвердил", "нет, не пришло", "нет, не готово", "нет, клиент не ответил",
    "нет, оплаты нет", "да, оплата есть", "да, документы готовы", "да, машина готова",
    "да, водитель подтвердил", "подтверждаю", "не подтверждаю", "отказ",
    "согласовано", "не согласовано", "принято", "не принято", "передал в работу",
    "передал ответственному", "назначил ответственного", "решение принято",
    "решили так", "ответ:", "итог:", "по итогу:", "статус: готово",
    "статус: проверено", "статус: отправлено", "статус: оплачено",
    "статус: не оплачено", "статус: в ремонте", "статус: отменено",
)

RESPONSE_REQUEST_PHRASES = (
    "?", "когда", "где", "почему", "зачем", "как", "какой", "какая", "какие",
    "сколько", "что по", "что с", "нужен ответ", "нужна обратная связь",
    "ответьте", "ответь", "отпишитесь", "отпишись", "сообщите", "сообщи",
    "уточните", "уточни", "проверьте", "проверь", "посмотрите", "посмотри",
    "глянь", "гляньте", "разберитесь", "разберись", "подскажите", "подскажи",
    "подтвердите", "подтверди", "согласуйте", "согласуй", "свяжитесь", "свяжись",
    "позвоните", "позвони", "напишите", "напиши", "пришлите", "пришли",
    "скиньте", "скинь", "отправьте", "отправь", "передайте", "передай",
    "сделайте", "сделай", "оформите", "оформи", "оплатите", "оплати",
    "создайте", "создай", "закройте", "закрой", "обновите", "обнови",
    "исправьте", "исправь", "добавьте", "добавь", "замените", "замени",
    "пригласите", "пригласи", "подключитесь", "подключись", "возьмите", "возьми",
    "нужно", "нужен", "нужна", "нужны", "надо", "необходимо", "требуется",
    "можешь", "можете", "можно", "просьба", "прошу", "пожалуйста",
    "в работу", "на контроль", "на контроле", "ждем ответ", "ждём ответ",
    "нет д/с", "нет дс", "нет денежных средств", "нет денег",
    "ругаются", "ругается", "жалуется", "жалуются",
)

NON_REQUEST_PHRASES = (
    "спасибо", "благодарю", "доброе утро", "добрый день", "добрый вечер",
    "хорошего дня", "хорошего вечера", "привет", "здравствуйте", "извините",
)


def text_contains_standalone_name(text: str, name: str) -> bool:
    normalized_text = normalized_match_text(text)
    normalized_name = normalized_match_text(name)
    if len(normalized_name) < 3:
        return False
    pattern = rf"(?<![\w@]){re.escape(normalized_name)}(?![\w@])"
    return re.search(pattern, normalized_text) is not None


def user_display_labels(user: User | None) -> set[str]:
    if not user:
        return set()

    labels: set[str] = set()
    full_name = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
    if full_name:
        labels.add(normalized_match_text(full_name))
    if user.first_name and not user.last_name:
        labels.add(normalized_match_text(user.first_name))
    if user.username:
        labels.add(normalized_match_text(user.username))
        labels.add(normalized_match_text(f"@{user.username}"))
    return {label for label in labels if len(label) >= 3}


def wait_matches_sender_display(wait: PendingWait, user: User | None) -> bool:
    if not user or not wait.display_name:
        return False
    if wait.user_id is not None and wait.user_id != user.id:
        return False

    wait_label = normalized_match_text(wait.display_name)
    if not wait_label or wait_label.startswith("@"):
        return False
    return wait_label in user_display_labels(user)


def usernames_probably_same(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    left = left.removeprefix("@").lower()
    right = right.removeprefix("@").lower()
    if left == right:
        return True

    left_stem = re.sub(r"\d+$", "", left)
    right_stem = re.sub(r"\d+$", "", right)
    if left_stem != right_stem or len(left_stem) < 5:
        return False

    return left != left_stem or right != right_stem


def wait_matches_telegram_user(wait: PendingWait, user: User | None) -> bool:
    if not user:
        return False
    if wait.user_id is not None:
        return wait.user_id == user.id

    username = user.username.lower() if user.username else None
    if username and usernames_probably_same(wait.username, username):
        return True

    return wait_matches_sender_display(wait, user)


def can_user_control_waits(waits: list[PendingWait], user: User | None, settings: Settings) -> bool:
    return is_leader(user, settings) or any(wait_matches_telegram_user(wait, user) for wait in waits)


async def resolve_user_id_for_matching_waits(
    app_storage: Storage,
    waits: list[PendingWait],
    user: User | None,
) -> None:
    if not user:
        return
    for wait in waits:
        if wait.user_id is None and wait_matches_telegram_user(wait, user):
            await app_storage.resolve_user_id_for_wait(wait.id, user.id)


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


def response_contains_phrase(normalized: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in normalized for phrase in phrases)


def classify_employee_response(message: Message, settings: Settings | None = None) -> str:
    text = normalized_message_text(message)
    if not text:
        return "full" if (message.photo or message.document or message.video or message.voice or message.audio) else "empty"

    normalized = normalized_match_text(text)
    compact = normalized.strip(" .,!?:;—-…")
    if compact in AMBIGUOUS_RESPONSE_EXACT:
        return "ambiguous"

    if settings is None or settings.enable_smart_reply_detection:
        if response_contains_phrase(normalized, FULL_RESPONSE_PHRASES):
            return "full"
        if response_contains_phrase(normalized, INTERMEDIATE_RESPONSE_PHRASES):
            return "intermediate"

    if len(compact) <= 12 and compact in AMBIGUOUS_RESPONSE_EXACT:
        return "ambiguous"
    return "full"


def is_unclear_employee_response(message: Message) -> bool:
    return classify_employee_response(message) == "ambiguous"


def is_meaningful_employee_response(message: Message) -> bool:
    return classify_employee_response(message) == "full"


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


def source_text_match_score(text: str, wait: PendingWait) -> int:
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


def source_answer_match_score(message: Message, wait: PendingWait) -> int:
    return source_text_match_score(normalized_message_text(message), wait)


def quoted_source_match_score(reply_message: Message, wait: PendingWait) -> int:
    return source_text_match_score(normalized_message_text(reply_message), wait)


def group_waits_by_source(waits: list[PendingWait]) -> list[list[PendingWait]]:
    groups: dict[int, list[PendingWait]] = {}
    for wait in waits:
        groups.setdefault(wait.source_message_id, []).append(wait)
    return [sorted(group, key=lambda item: item.id) for group in groups.values()]


def single_source_waits(waits: list[PendingWait]) -> list[PendingWait]:
    source_groups = group_waits_by_source(waits)
    if len(source_groups) == 1:
        return source_groups[0]
    return []


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


async def active_waits_matching_source_text(
    app_storage: Storage,
    *,
    chat_id: int,
    text: str,
    min_score: int = 2,
) -> list[PendingWait]:
    if not text:
        return []

    active_waits = await app_storage.active_waits_in_chat(chat_id=chat_id)
    scored_groups: list[tuple[int, list[PendingWait]]] = []
    for group in group_waits_by_source(active_waits):
        score = max(source_text_match_score(text, wait) for wait in group)
        if score >= min_score:
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
    if reply:
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

        by_replied_text = await active_waits_matching_source_text(
            app_storage,
            chat_id=message.chat.id,
            text=normalized_message_text(reply),
        )
        if by_replied_text:
            return by_replied_text

    external_reply = getattr(message, "external_reply", None)
    external_chat = getattr(external_reply, "chat", None) if external_reply else None
    external_message_id = getattr(external_reply, "message_id", None) if external_reply else None
    external_chat_id = getattr(external_chat, "id", None) if external_chat else None
    if external_message_id and external_chat_id == message.chat.id:
        by_external_source = await app_storage.active_waits_for_source_message(
            chat_id=message.chat.id,
            source_message_id=external_message_id,
        )
        if by_external_source:
            return by_external_source

    quote = getattr(message, "quote", None)
    quote_text = getattr(quote, "text", None) if quote else None
    if quote_text:
        by_quote = await active_waits_matching_source_text(
            app_storage,
            chat_id=message.chat.id,
            text=quote_text,
        )
        if by_quote:
            return by_quote

    return []


async def active_waits_for_sender(
    app_storage: Storage,
    message: Message,
    *,
    user_id: int | None,
    username_lower: str | None,
) -> list[PendingWait]:
    waits = await app_storage.active_waits_for_user(
        chat_id=message.chat.id,
        user_id=user_id,
        username_lower=username_lower,
    )
    seen_ids = {wait.id for wait in waits}

    if not message.from_user:
        return waits

    active_waits = await app_storage.active_waits_in_chat(chat_id=message.chat.id)
    for wait in active_waits:
        if wait.id in seen_ids:
            continue
        if not wait_matches_sender_display(wait, message.from_user):
            continue
        if user_id is not None and wait.user_id is None:
            await app_storage.resolve_user_id_for_wait(wait.id, user_id)
        waits.append(wait)
        seen_ids.add(wait.id)

    return sorted(waits, key=lambda wait: wait.id)


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

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        username = parts[1].strip().removeprefix("@").lower()
        known_user = await app_storage.get_user_by_username(username)
        stats = await app_storage.employee_stats(
            username_lower=username,
            user_id=known_user["user_id"] if known_user else None,
            now=now_in_tz(settings),
        )
        display = stats.display_name or display_username(username)
        avg = "нет данных"
        if stats.avg_response_seconds_7d:
            now = now_in_tz(settings)
            avg = format_elapsed(now - timedelta(seconds=stats.avg_response_seconds_7d), now)
        reason_lines = [f"— {reason}: {count}" for reason, count in stats.delay_reasons_7d.items()] or ["— нет"]
        await message.answer(
            "\n".join(
                [
                    f"Статистика по {html.escape(display)}:",
                    "",
                    "За 7 дней:",
                    f"— обращений: {stats.requests_7d}",
                    f"— закрыто вовремя: {stats.closed_on_time_7d}",
                    f"— просрочек: {stats.overdue_7d}",
                    f"— предупреждений за месяц: {stats.warnings_month}",
                    f"— штрафов за месяц: {stats.fines_month}",
                    f"— среднее время ответа: {avg}",
                    f"— нажимал «Вижу»: {stats.seen_count_7d}",
                    f"— выбирал «Ответил, бот не увидел»: {stats.answered_not_seen_count_7d}",
                    f"— подтверждённых ошибок бота за месяц: {stats.bot_missed_confirmed_month}",
                    "",
                    "Причины задержек:",
                    *reason_lines,
                ]
            ),
            parse_mode="HTML",
        )
        return

    summary = await app_storage.metrics_summary()
    await message.answer(
        "\n".join(
            [
                "Статистика бота:",
                f"Активные ожидания: {summary.get('active_waits', 0)}",
                f"Создано/обновлено ожиданий: {summary.get('wait_upserted', 0)}",
                f"Закрыто reply-ом: {summary.get('wait_closed_by_reply', 0)}",
                f"Закрыто реакцией: {summary.get('wait_closed_by_reaction', 0)}",
                f"Закрыто кнопкой: {summary.get('wait_closed_by_button', 0)}",
                f"Нажали «Вижу»: {summary.get('wait_seen', 0)}",
                f"Запрошено решений руководителя: {summary.get('leader_decision_requested', 0)}",
                f"Предупреждений: {summary.get('warning_issued', 0)}",
                f"Штрафов назначено: {summary.get('fine_issued', 0)}",
                f"Штрафов отклонено: {summary.get('fine_declined', 0)}",
                f"Оставлено под контролем: {summary.get('wait_kept_by_confirm_button', 0)}",
                f"Переадресовано: {summary.get('wait_delegated', 0)}",
                f"Групповых напоминаний: {summary.get('group_reminder_sent', 0)}",
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
                f"Отсрочка по кнопке «Вижу»: {settings.seen_delay_minutes} мин.",
                "Закрытие ответом: только reply на исходное обращение или напоминание бота",
                "Кнопки в напоминании: 👀 Вижу и Закрыть",
                f"Предупреждения: {'включено' if settings.enable_warning_decision else 'выключено'}",
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

    if command_text == "отложить":
        await message.reply("Отложить можно кнопкой «👀 Вижу» у адресата обращения.")
        return

    source_waits = await app_storage.active_waits_for_source_message(
        chat_id=wait.chat_id,
        source_message_id=wait.source_message_id,
    )
    source_waits = source_waits or [wait]
    if not can_user_control_waits(source_waits, message.from_user, settings):
        await message.reply("Закрыть обращение может руководитель или адресат обращения.")
        return

    now = now_in_tz(settings)
    await resolve_user_id_for_matching_waits(app_storage, source_waits, message.from_user)
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


async def record_employee_message_events(
    app_storage: Storage,
    waits: list[PendingWait],
    message: Message,
    *,
    event_type: str,
    now: datetime,
) -> None:
    if not waits:
        return
    await app_storage.record_wait_events(
        waits,
        event_type=event_type,
        created_at=now,
        actor_user_id=message.from_user.id if message.from_user else None,
        actor_label=actor_label(message.from_user),
        text=normalized_message_text(message),
    )


async def postpone_source_waits(
    app_storage: Storage,
    *,
    chat_id: int,
    source_message_ids: list[int],
    now: datetime,
    delay_minutes: int,
    settings: Settings,
    mode: str,
    actor_user_id: int | None = None,
) -> list[PendingWait]:
    next_at = add_working_minutes(
        now,
        delay_minutes,
        settings.workday_start,
        settings.workday_end,
        settings.timezone,
    )
    if mode == "seen" and actor_user_id is not None:
        return await app_storage.mark_source_seen(
            chat_id=chat_id,
            source_message_ids=source_message_ids,
            seen_by_user_id=actor_user_id,
            seen_at=now,
            next_reminder_at=next_at,
        )
    if mode == "intermediate":
        return await app_storage.mark_source_intermediate(
            chat_id=chat_id,
            source_message_ids=source_message_ids,
            intermediate_at=now,
            next_reminder_at=next_at,
        )
    return await app_storage.reschedule_waits_for_source_messages(
        chat_id=chat_id,
        source_message_ids=source_message_ids,
        next_reminder_at=next_at,
    )


async def handle_intermediate_response(
    bot: Bot,
    app_storage: Storage,
    message: Message,
    waits: list[PendingWait],
    now: datetime,
    settings: Settings,
) -> None:
    source_message_ids = [wait.source_message_id for wait in waits]
    updated_waits = await postpone_source_waits(
        app_storage,
        chat_id=message.chat.id,
        source_message_ids=source_message_ids,
        now=now,
        delay_minutes=settings.smart_reply_delay_minutes,
        settings=settings,
        mode="intermediate",
    )
    await record_employee_message_events(app_storage, updated_waits or waits, message, event_type="intermediate_answer", now=now)
    await app_storage.record_metric(
        "wait_intermediate_answer",
        now=now,
        chat_id=message.chat.id,
        username_lower=waits[0].username if waits else None,
        value=max(len(updated_waits), 1),
    )
    await bot.send_message(
        chat_id=message.chat.id,
        text=(
            f"{actor_label(message.from_user)} подтвердил, что занимается вопросом.\n"
            "Ждём полноценный ответ."
        ),
        parse_mode="HTML",
        reply_parameters=ReplyParameters(message_id=waits[0].source_message_id, allow_sending_without_reply=True),
    )


def answer_found_in_events(events: list[WaitEvent], wait: PendingWait) -> bool:
    for event in events:
        if event.event_type in {"full_answer", "reaction", "closed_by_reply", "closed_by_message"}:
            return True
        if event.event_type == "employee_message" and event.text:
            if classify_text_as_response(event.text) == "full" or source_text_match_score(event.text, wait) >= 2:
                return True
    return False


def classify_text_as_response(text: str) -> str:
    fake_message = type("FakeMessage", (), {
        "text": text,
        "caption": None,
        "photo": None,
        "document": None,
        "video": None,
        "voice": None,
        "audio": None,
    })()
    return classify_employee_response(fake_message)


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def handle_group_message(message: Message, bot: Bot, app_storage: Storage, settings: Settings) -> None:
    await register_user_from_message(app_storage, message, settings)

    now = now_in_tz(settings)
    sender_username = message.from_user.username.lower() if message.from_user and message.from_user.username else None
    sender_id = message.from_user.id if message.from_user else None

    # New waits must start only from explicit Telegram mentions: @username,
    # text_mention, or tg://user links. Plain names in normal text are used only
    # to match already active waits, not to create new control tasks.
    explicit_targets = extract_mention_targets(message)
    mention_targets = [
        target
        for target in explicit_targets
        if not (
            (sender_id is not None and target.user_id == sender_id)
            or (sender_username is not None and target.identity == sender_username)
        )
    ]
    actionable_mention_targets = mention_targets if message_requires_response(message, mention_targets) else []

    sender_waits = await active_waits_for_sender(
        app_storage,
        message,
        user_id=sender_id,
        username_lower=sender_username,
    )
    reply_source_waits: list[PendingWait] = []
    if message.reply_to_message or getattr(message, "quote", None) or getattr(message, "external_reply", None):
        reply_source_waits = await active_waits_for_reply(app_storage, message)

    tracked_waits = reply_source_waits or sender_waits
    if tracked_waits and normalized_message_text(message):
        await record_employee_message_events(app_storage, tracked_waits, message, event_type="employee_message", now=now)

    if reply_source_waits and actionable_mention_targets:
        if not can_user_control_waits(reply_source_waits, message.from_user, settings):
            return
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
            actionable_mention_targets,
            now,
            source_waits_count=len(reply_source_waits),
        )
        return

    if sender_waits and actionable_mention_targets:
        delegated_waits = single_source_waits(sender_waits)
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
                actionable_mention_targets,
                now,
                source_waits_count=len(delegated_waits),
            )
            return

        for target in actionable_mention_targets:
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

    if reply_source_waits:
        if not can_user_control_waits(reply_source_waits, message.from_user, settings):
            return
        await record_employee_message_events(app_storage, reply_source_waits, message, event_type="full_answer", now=now)
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

    if sender_waits:
        answered_waits = single_source_waits(sender_waits)
        if answered_waits:
            await record_employee_message_events(app_storage, answered_waits, message, event_type="full_answer", now=now)
            await close_waits_after_employee_message(
                bot,
                app_storage,
                message,
                answered_waits,
                now,
                metric_name="wait_closed_by_single_active_message",
                reason="ответил",
            )
        return

    if not actionable_mention_targets:
        return

    for target in actionable_mention_targets:
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
        reminder_waits = await app_storage.active_waits_for_reminder_message(
            chat_id=event.chat.id,
            reminder_message_id=event.message_id,
        )
        if reminder_waits:
            waits = await app_storage.active_waits_for_source_messages(
                chat_id=event.chat.id,
                source_message_ids=[wait.source_message_id for wait in reminder_waits],
            ) or reminder_waits

    if not waits:
        return

    if not any(wait_matches_telegram_user(wait, event.user) for wait in waits):
        return

    await resolve_user_id_for_matching_waits(app_storage, waits, event.user)

    source_message_ids = [wait.source_message_id for wait in waits]
    closed_waits = await app_storage.close_waits_for_source_messages(
        chat_id=event.chat.id,
        source_message_ids=source_message_ids,
        closed_by_user_id=event.user.id,
        now=now,
    )
    if not closed_waits:
        return

    await app_storage.record_wait_events(
        closed_waits,
        event_type="reaction",
        created_at=now,
        actor_user_id=event.user.id,
        actor_label=actor_label(event.user),
        text="reaction",
    )
    username = event.user.username.lower() if event.user.username else None
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

    if action == "seen":
        source_waits = await app_storage.active_waits_for_source_message(
            chat_id=wait.chat_id,
            source_message_id=wait.source_message_id,
        )
        if not any(wait_matches_telegram_user(item, callback.from_user) for item in source_waits):
            await safe_callback_answer(callback, "Кнопка «Вижу» доступна адресату обращения.", show_alert=True)
            return
        await resolve_user_id_for_matching_waits(app_storage, source_waits, callback.from_user)
        updated_waits = await postpone_source_waits(
            app_storage,
            chat_id=wait.chat_id,
            source_message_ids=[wait.source_message_id],
            now=now,
            delay_minutes=settings.seen_delay_minutes,
            settings=settings,
            mode="seen",
            actor_user_id=callback.from_user.id,
        )
        await app_storage.record_wait_events(
            updated_waits or source_waits,
            event_type="seen",
            created_at=now,
            actor_user_id=callback.from_user.id,
            actor_label=actor_label(callback.from_user),
            text=source_reference(wait.source_message_link, wait.source_quote),
        )
        await app_storage.record_metric(
            "wait_seen",
            now=now,
            chat_id=wait.chat_id,
            username_lower=wait.username,
            wait_id=wait.id,
            value=max(len(updated_waits), 1),
        )
        await bot.send_message(
            chat_id=wait.chat_id,
            text=(
                f"{actor_label(callback.from_user)} увидел обращение.\n"
                f"Даём дополнительные {settings.seen_delay_minutes} минут на ответ."
            ),
            parse_mode="HTML",
            reply_parameters=ReplyParameters(message_id=wait.source_message_id, allow_sending_without_reply=True),
        )
        await safe_callback_answer(callback, "Зафиксировал: увидел.")
        return

    if action.startswith("reason_"):
        reason_key = action.removeprefix("reason_")
        source_waits = await app_storage.active_waits_for_source_message(
            chat_id=wait.chat_id,
            source_message_id=wait.source_message_id,
        )
        if not any(wait_matches_telegram_user(item, callback.from_user) for item in source_waits):
            await safe_callback_answer(callback, "Причину может указать адресат обращения.", show_alert=True)
            return
        await resolve_user_id_for_matching_waits(app_storage, source_waits, callback.from_user)
        updated_waits = await app_storage.set_delay_reason_for_source(
            chat_id=wait.chat_id,
            source_message_ids=[wait.source_message_id],
            reason=reason_key,
            reason_at=now,
        )
        await app_storage.record_wait_events(
            updated_waits or source_waits,
            event_type="reason_answered_not_seen" if reason_key == "answered_not_seen" else "delay_reason",
            created_at=now,
            actor_user_id=callback.from_user.id,
            actor_label=actor_label(callback.from_user),
            text=reason_label(reason_key),
        )
        await app_storage.record_metric(
            "delay_reason_selected",
            now=now,
            chat_id=wait.chat_id,
            username_lower=wait.username,
            wait_id=wait.id,
            value=max(len(updated_waits), 1),
        )
        if reason_key == "answered_not_seen":
            events = await app_storage.wait_events_for_source(chat_id=wait.chat_id, source_message_id=wait.source_message_id)
            if answer_found_in_events(events, wait):
                closed_waits = await app_storage.close_waits_for_source_messages(
                    chat_id=wait.chat_id,
                    source_message_ids=[wait.source_message_id],
                    closed_by_user_id=callback.from_user.id,
                    now=now,
                )
                if closed_waits:
                    await mark_waits_closed(
                        bot,
                        closed_waits,
                        actor_label(callback.from_user),
                        "ответ найден после проверки",
                        now,
                        settings,
                        time_label="Время проверки",
                    )
                    await bot.send_message(
                        chat_id=wait.chat_id,
                        text="Ответ сотрудника найден.\nОбращение закрыто.",
                        reply_parameters=ReplyParameters(message_id=wait.source_message_id, allow_sending_without_reply=True),
                    )
                await safe_callback_answer(callback, "Ответ найден, обращение закрыто.")
                return
            await bot.send_message(
                chat_id=wait.chat_id,
                text=(
                    "Бот не смог автоматически найти ответ.\n"
                    "Запрос отправлен руководителю на ручную проверку."
                ),
                reply_parameters=ReplyParameters(message_id=wait.source_message_id, allow_sending_without_reply=True),
            )
            await send_leader_decision_request(bot, app_storage, settings, updated_waits or source_waits, now, manual_review=True)
            await safe_callback_answer(callback, "Передал руководителю на проверку.")
            return

        await bot.send_message(
            chat_id=wait.chat_id,
            text=f"Причина задержки принята: {html.escape(reason_label(reason_key))}.",
            parse_mode="HTML",
            reply_parameters=ReplyParameters(message_id=wait.source_message_id, allow_sending_without_reply=True),
        )
        await safe_callback_answer(callback, "Причина сохранена.")
        return

    if action in {"fine", "nofine", "warning", "closeok"}:
        if not is_leader(callback.from_user, settings):
            await safe_callback_answer(
                callback,
                f"Решение может принять только {display_username(settings.leader_username)}.",
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

        decision_map = {
            "fine": "issued",
            "warning": "warning",
            "nofine": "declined",
            "closeok": "manual_answer_confirmed",
        }
        decision = decision_map[action]
        amount = settings.fine_amount_rubles if action == "fine" else 0
        await app_storage.record_fine_decisions(
            waits=closed_waits,
            decision=decision,
            amount_rubles=amount,
            decided_by_user_id=callback.from_user.id,
            decided_at=now,
        )
        await app_storage.record_wait_events(
            closed_waits,
            event_type=f"leader_decision_{decision}",
            created_at=now,
            actor_user_id=callback.from_user.id,
            actor_label=actor_label(callback.from_user),
            text=reason_label(wait.delay_reason),
        )
        await app_storage.record_metric(
            {
                "fine": "fine_issued",
                "warning": "warning_issued",
                "nofine": "fine_declined",
                "closeok": "manual_answer_confirmed",
            }[action],
            now=now,
            chat_id=wait.chat_id,
            username_lower=wait.username,
            wait_id=wait.id,
            value=len(closed_waits),
        )

        reference = source_reference(wait.source_message_link, wait.source_quote)
        common = (
            f"Решение принял: {actor_label(callback.from_user)}\n"
            f"Время решения: {format_event_dt(now, settings)}\n\n"
            f"Адресаты: {wait_targets_label(closed_waits)}\n\n"
            f"Что было:\n" + "\n".join(source_history_lines(closed_waits)) + "\n\n"
            f"Исходное обращение:\n{reference}"
        )
        if action == "fine":
            header = (
                "Обращение закрыто с нарушением регламента ответа.\n\n"
                f"Руководитель зафиксировал штраф: {settings.fine_amount_rubles} ₽."
            )
        elif action == "warning":
            header = (
                "Обращение закрыто с нарушением регламента ответа.\n\n"
                "Руководитель зафиксировал предупреждение."
            )
        elif action == "closeok":
            header = "Обращение закрыто: руководитель подтвердил, что ответ был."
        else:
            header = "Обращение закрыто без штрафа."
        group_text = f"{header}\n{common}"
        private_text = (
            f"Решение сохранено.\n{header}\n\n"
            f"Адресаты: {wait_targets_label(closed_waits)}\n"
            f"Исходное обращение: {reference}"
        )
        await edit_reminder_closed(bot, closed_waits[0], group_text)
        await edit_callback_message(callback, private_text)
        await safe_callback_answer(callback, "Решение сохранено, сообщение в группе обновлено.")
        return

    if action in {"close", "confirmclose"}:
        source_waits = await app_storage.active_waits_for_source_message(
            chat_id=wait.chat_id,
            source_message_id=wait.source_message_id,
        )
        source_waits = source_waits or [wait]
        if not can_user_control_waits(source_waits, callback.from_user, settings):
            await safe_callback_answer(
                callback,
                "Кнопка «Закрыть» доступна руководителю и адресатам обращения.",
                show_alert=True,
            )
            return

        await resolve_user_id_for_matching_waits(app_storage, source_waits, callback.from_user)
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
            await app_storage.record_wait_events(
                closed_waits,
                event_type="manual_close" if action == "close" else "confirm_close",
                created_at=now,
                actor_user_id=callback.from_user.id,
                actor_label=actor_label(callback.from_user),
                text=None,
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

    if action in {"keep", "snooze"}:
        await safe_callback_answer(
            callback,
            "Эта кнопка больше не используется. Нажмите «👀 Вижу» или «Закрыть».",
            show_alert=True,
        )
        return

    await safe_callback_answer(callback, "Неизвестное действие.", show_alert=True)


def reason_label(reason_key: str | None) -> str:
    if not reason_key:
        return "Причина задержки не указана"
    return DELAY_REASONS.get(reason_key, reason_key)


def source_history_lines(waits: list[PendingWait]) -> list[str]:
    if not waits:
        return ["— данных нет"]
    reminder_count = max(wait.reminder_count for wait in waits)
    lines = [f"— {reminder_count} {plural_ru(reminder_count, ('напоминание', 'напоминания', 'напоминаний'))} в чат"]
    if any(wait.seen_at for wait in waits):
        lines.append("— сотрудник нажимал «Вижу»")
    if any(wait.last_intermediate_at for wait in waits):
        lines.append("— были промежуточные ответы")
    lines.append("— полноценного ответа нет")
    return lines


def employee_stats_text(stats: EmployeeStats) -> str:
    avg = format_elapsed(datetime.now().astimezone() - timedelta(seconds=stats.avg_response_seconds_7d), datetime.now().astimezone()) if stats.avg_response_seconds_7d else "нет данных"
    lines = [
        f"— просрочек за 7 дней: {stats.overdue_7d}",
        f"— штрафов за месяц: {stats.fines_month}",
        f"— предупреждений за месяц: {stats.warnings_month}",
        f"— среднее время ответа: {avg}",
        f"— нажимал «Вижу»: {stats.seen_count_7d}",
        f"— выбирал «Ответил, бот не увидел»: {stats.answered_not_seen_count_7d}",
    ]
    if stats.bot_missed_confirmed_month:
        lines.append(f"— бот реально ошибся: {stats.bot_missed_confirmed_month}")
    return "\n".join(lines)


async def leader_request_text(
    app_storage: Storage,
    settings: Settings,
    waits: list[PendingWait],
    now: datetime,
    *,
    manual_review: bool = False,
) -> str:
    wait = waits[0]
    chat_title = html.escape(wait.chat_title or str(wait.chat_id))
    reference = source_reference(wait.source_message_link, wait.source_quote)
    stats = await app_storage.employee_stats(username_lower=wait.username, user_id=wait.user_id, now=now)
    intro = "Нужно решение по обращению."
    if manual_review:
        intro = (
            "Сотрудник указал: «Ответил, бот не увидел».\n"
            "Автоматически подтвердить ответ не удалось. Проверьте обращение вручную."
        )
    return (
        f"{intro}\n\n"
        f"Сотрудник: {wait_targets_label(waits)}\n"
        f"Чат: {chat_title}\n"
        f"Нет ответа: {format_elapsed(wait.created_at, now)}\n\n"
        f"Что было:\n" + "\n".join(source_history_lines(waits)) + "\n\n"
        f"История сотрудника:\n{employee_stats_text(stats)}\n\n"
        f"Исходное обращение:\n{reference}"
    )


async def send_leader_decision_request(
    bot: Bot,
    app_storage: Storage,
    settings: Settings,
    waits: list[PendingWait],
    now: datetime,
    *,
    manual_review: bool = False,
) -> bool:
    if not waits:
        return False
    wait = waits[0]
    text = await leader_request_text(app_storage, settings, waits, now, manual_review=manual_review)
    sent = await notify_leader(
        bot,
        app_storage,
        settings,
        text,
        now,
        wait,
        reply_markup=leader_decision_keyboard(wait, settings.fine_amount_rubles, enable_warning=settings.enable_warning_decision),
    )
    if not sent:
        return False

    await app_storage.mark_leader_request_sent_for_source(
        chat_id=wait.chat_id,
        source_message_ids=[item.source_message_id for item in waits],
        sent_at=now,
    )
    for item in waits:
        await app_storage.record_metric(
            "leader_decision_requested",
            now=now,
            chat_id=item.chat_id,
            username_lower=item.username,
            wait_id=item.id,
        )
    return True


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
    should_request_leader = (
        next_reminder_number >= settings.escalate_after_reminders
        and not wait.leader_request_sent_at
    )
    is_final_after_dm_failure = limit_applies and next_reminder_number >= dm_failure_limit

    reminder_prefix = f"Напоминание {next_reminder_number}."
    if should_request_leader:
        text = (
            f"{reminder_prefix} {target_labels}, ответа нет по сообщению: {reference}\n"
            "Запрос на решение отправлен руководителю."
        )
    elif next_reminder_number == 2:
        text = (
            f"{reminder_prefix} {target_labels}, нужен ответ по сообщению: {reference}\n"
            f"Если ответа не будет до следующего напоминания, руководителю уйдёт запрос на решение. "
            f"Возможен штраф {settings.fine_amount_rubles} ₽."
        )
    elif is_final_after_dm_failure:
        text = (
            f"{reminder_prefix} {target_labels}, финальное напоминание по сообщению: {reference}\n"
            "Пожалуйста, не оставляйте обращения коллег без ответа: "
            "в рабочих чатах это недопустимо и задерживает работу команды."
        )
    elif wait.seen_at:
        text = f"{reminder_prefix} {target_labels} видел обращение, но ответа пока нет: {reference}"
    else:
        text = f"{reminder_prefix} {target_labels}, нужен ответ по сообщению: {reference}"

    await delete_previous_reminders(bot, source_waits)

    sent_message = await bot.send_message(
        chat_id=wait.chat_id,
        text=text,
        disable_web_page_preview=True,
        reply_markup=wait_keyboard(wait, settings),
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

    if should_request_leader:
        leader_notified = await send_leader_decision_request(bot, app_storage, settings, source_waits, now)
        if leader_notified:
            for item in source_waits:
                await app_storage.stop_group_reminders(item.id, now)
                await app_storage.record_metric(
                    "group_reminders_stopped_after_leader_request",
                    now=now,
                    chat_id=item.chat_id,
                    username_lower=item.username,
                    wait_id=item.id,
                )
        return

    if is_final_after_dm_failure:
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
        if not items:
            await app_storage.mark_daily_report_sent(chat_id=chat_id, report_date=report_date, sent_at=now)
            await app_storage.record_metric(
                "daily_report_skipped_empty",
                now=now,
                chat_id=chat_id,
                value=1,
            )
            continue

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
