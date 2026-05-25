from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass

from aiogram.types import Message, MessageEntity
from aiogram.enums import MessageEntityType

USERNAME_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{5,32})")


@dataclass(frozen=True)
class MentionTarget:
    identity: str
    display_name: str
    username: str | None = None
    user_id: int | None = None
    first_name: str | None = None
    last_name: str | None = None


def normalize_username(username: str) -> str:
    return username.removeprefix("@").lower()


def display_username(username: str) -> str:
    return "@" + normalize_username(username)


def _user_full_name(first_name: str | None, last_name: str | None) -> str:
    return " ".join(part for part in (first_name, last_name) if part).strip()


def extract_mention_targets(message: Message) -> list[MentionTarget]:
    text = message.text or message.caption or ""
    targets: dict[str, MentionTarget] = {}

    entities: Iterable[MessageEntity] = message.entities or message.caption_entities or []
    for entity in entities:
        if entity.type == MessageEntityType.MENTION:
            mention = entity.extract_from(text)
            username = normalize_username(mention)
            targets[username] = MentionTarget(
                identity=username,
                display_name=display_username(username),
                username=username,
            )
        elif entity.type == MessageEntityType.TEXT_MENTION and entity.user:
            user = entity.user
            username = normalize_username(user.username) if user.username else None
            display_name = entity.extract_from(text) or _user_full_name(user.first_name, user.last_name)
            identity = username or f"user_id:{user.id}"
            targets[identity] = MentionTarget(
                identity=identity,
                display_name=display_name,
                username=username,
                user_id=user.id,
                first_name=user.first_name,
                last_name=user.last_name,
            )

    for match in USERNAME_RE.finditer(text):
        username = normalize_username(match.group(1))
        targets.setdefault(
            username,
            MentionTarget(
                identity=username,
                display_name=display_username(username),
                username=username,
            ),
        )

    return sorted(targets.values(), key=lambda target: target.display_name.lower())


def extract_mentioned_usernames(message: Message) -> list[str]:
    return [target.identity for target in extract_mention_targets(message)]


def short_quote(message: Message, limit: int = 120) -> str:
    text = (message.text or message.caption or "").strip()
    if not text:
        return "сообщение без текста"

    compact = re.sub(r"\s+", " ", text)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."


def build_message_link(message: Message) -> str | None:
    if message.chat.username:
        return f"https://t.me/{message.chat.username}/{message.message_id}"

    chat_id = message.chat.id
    if str(chat_id).startswith("-100"):
        internal_id = str(chat_id)[4:]
        return f"https://t.me/c/{internal_id}/{message.message_id}"

    return None


def source_reference(link: str | None, quote: str) -> str:
    escaped_quote = html.escape(quote)
    if link:
        escaped_link = html.escape(link, quote=True)
        return f'<a href="{escaped_link}">{escaped_quote}</a>'
    return f"\"{escaped_quote}\""
