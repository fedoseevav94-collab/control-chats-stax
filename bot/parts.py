"""Private procurement workflows. Separate state and permissions from response reminders."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup

LOG = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Moscow")
MANAGERS = {"pagorodu": "Арман", "lalalas19": "Рафаэль", "serb_98": "Александр Петрович"}
STATUS = {
    "new": "Требуется ответ", "order": "Нужно заказать", "partial": "Заказано частично",
    "payment": "Заказано — ждём оплату", "disassembly": "Ждём разбор",
    "blocked": "Не можем заказать", "approval": "Ждём подтверждение руководителя",
    "closed": "Закрыто",
}
CHOICES = {"order": "Нужно заказать", "partial": "Заказано частично", "payment": "Всё заказано — ждём оплату",
           "paid": "Всё заказано и оплачено", "disassembly": "Ждём разбор", "blocked": "Не можем заказать",
           "repaired": "Уже отремонтирована", "unneeded": "Ремонт не требуется"}
CLOSING = ("paid", "repaired", "unneeded")  # закрывают заявку сразу; руководитель может вернуть в работу
NEED_PARTS = ("order", "partial", "payment", "paid")
COMMENT_REQUIRED = ("partial", "blocked", "repaired", "unneeded", "keep")
MEDIA = ("document", "photo", "video", "voice", "video_note", "audio", "animation")
TRANS = str.maketrans("ABEKMHOPCTYX", "АВЕКМНОРСТУХ")


def now():
    return datetime.now(TZ)


def normalize(value):
    return re.sub(r"[\s_-]+", "", value.upper()).translate(TRANS)


SEP = r"[\s_\-./|\\]*"  # разделители внутри номера: пробел, дефис, подчёркивание, точка, слэш, черта
PLATE = re.compile(
    r"(?<![А-ЯA-Z0-9])([АВЕКМНОРСТУХ0])" + SEP + r"([\dО]{3})" + SEP + r"([АВЕКМНОРСТУХ0]{2})"
    r"(?:" + SEP + r"(\d{2,3})(?!\d)|(?![А-ЯA-Z\d]))")


def plates(text, fleet):
    """Госномера из текста. Латиница и кириллица, любой регистр, разделители внутри номера,
    0 вместо О (и наоборот) — если такой номер есть в справочнике, номер без региона — если он
    однозначно находится в справочнике."""
    text = text.upper().translate(TRANS)
    result = set()
    for letter, digits, letters, region in PLATE.findall(text):
        fixed = (letter.replace("0", "О") + digits.replace("О", "0") + letters.replace("0", "О"))
        guessed = fixed != letter + digits + letters  # пришлось исправить 0/О
        if region:
            plate = fixed + region
            if not guessed or plate in fleet:
                result.add(plate)
        else:
            matches = [p for p in fleet if p.startswith(fixed)]
            if len(matches) == 1:
                result.add(matches[0])
    # Нестандартные номера из справочника (например ЕС53674 / ЕС 53674) — только точное совпадение.
    for plate in fleet:
        if PLATE.fullmatch(plate):
            continue
        pattern = SEP.join(re.escape(ch) for ch in plate)
        if re.search(r"(?<![А-ЯA-Z0-9])" + pattern + r"(?![А-ЯA-Z0-9])", text):
            result.add(plate)
    return sorted(result)


def working(value):
    return value.weekday() < 5 and 9 <= value.hour < 18


def deadline(value, minutes=120):
    value = value.replace(second=0, microsecond=0)
    while not working(value):
        value += timedelta(minutes=1)
    while minutes:
        value += timedelta(minutes=1)
        if working(value - timedelta(minutes=1)):
            minutes -= 1
    return value


def parse_date(text, current):
    value = datetime.strptime(text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
    if value <= current or not working(value):
        raise ValueError("Нужна будущая дата: Пн–Пт, 09:00–17:59 МСК.")
    return value


MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7,
          "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
ACCIDENT = r"(?:дтп|д\.\s*т\.\s*п\.?|авари\w*|столкнов\w*|происшеств\w*)"
NUM_DATE = r"(\d{1,2})[.,/\-\s](\d{1,2})(?:[.,/\-\s](\d{4}|\d{2}))?(?!\d)"
WORD_DATE = r"(\d{1,2})\s*(" + "|".join(MONTHS) + r")[а-я]*\.?(?:\s*(\d{4}))?"
RELATIVE = {"позавчера": 2, "вчера": 1, "сегодня": 0}


def _make_date(day, month, year, today):
    try:
        value = date(int(year) + (2000 if len(year) == 2 else 0) if year else today.year, int(month), int(day))
    except ValueError:
        return None
    if not year and value > today + timedelta(days=1):
        try:
            value = value.replace(year=value.year - 1)
        except ValueError:
            return None
    if value > today + timedelta(days=1) or value < today - timedelta(days=730):
        return None
    return value


def accident_date(text, today=None):
    """Дата ДТП. Понимает «ДТП от 24.11.2026», «ДТП 24.11», «24,11», «24 11 2026», «24/11/26»,
    «24 ноября», «ДТП вчера», дату перед словом ДТП и дату в имени архива."""
    today = today or now().date()
    low = text.lower().replace("ё", "е")
    near = r"[\W_]{0,3}(?:от|был[оа]?|произошл[оа]?)?[\W_]{0,3}"
    # (шаблон, вид, только недавняя дата) — от самых надёжных к менее надёжным
    candidates = [
        (ACCIDENT + near + r"(позавчера|вчера|сегодня)", "rel", False),
        (ACCIDENT + near + WORD_DATE, "word", False),
        (ACCIDENT + near + NUM_DATE, "num", False),
        (WORD_DATE + r"[^\n]{0,15}?" + ACCIDENT, "word", False),
        (NUM_DATE + r"[^\n\d]{0,15}?" + ACCIDENT, "num", False),
        (r"(?<![\d.])(\d{1,2})[./\-](\d{1,2})[./\-](\d{4}|\d{2})(?!\d)", "num", False),  # полная дата где угодно
        (r"(?<!\d)" + WORD_DATE, "word", False),
        (r"(?<![\d.,/])(\d{1,2})[./](\d{1,2})(?![\d.,/])()", "num", True),  # «24.11» где угодно — только недавняя
    ]
    for pattern, kind, recent_only in candidates:
        for match in re.finditer(pattern, low):
            if kind == "rel":
                return (today - timedelta(days=RELATIVE[match[1]])).isoformat()
            day, month, year = match.groups()[-3:]
            value = _make_date(day, MONTHS[month] if kind == "word" else month, year, today)
            if value and (not recent_only or value >= today - timedelta(days=60)):
                return value.isoformat()
    return None


def repair_notice(text):
    """«Можно в ремонт» во всех частых формулировках; вопросы и отрицания — нет."""
    value = text.lower().replace("ё", "е")
    if "?" in value or re.search(r"нельзя|не\s+можно|не\s+разреш|не\s+осмотр|запрещ|пока\s+не\b|еще\s+не\b", value):
        return False
    return bool(re.search(
        r"можно\s+(?:\w+\s+){0,2}в\s+ремонт|в\s+ремонт\s+(?:\w+\s+)?можно|можно\s+ремонтир"
        r"|разреш\w*\s+(?:\w+\s+){0,2}ремонт|осмотр\w*\s+(?:\w+\s+){0,2}(?:пройден|прошл|закончен|заверш|состоял)"
        r"|страхов\w*\s+(?:\w+\s+){0,2}осмотрел", value))


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data="parts:" + data) for label, data in row
    ] for row in rows])


class Parts:
    def __init__(self, path, fleet, leader="fedos_av"):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS parts_state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        row = self.db.execute("SELECT data FROM parts_state WHERE id=1").fetchone()
        self.data = json.loads(row[0]) if row else {
            "jobs": {}, "users": {}, "sessions": {}, "events": {}, "outbox": {}, "history": [],
            "source": None, "target": None, "leader_id": None,
        }
        self.fleet = {normalize(r["plate"]): r for r in fleet}
        self.leader = leader.lower()
        self.lock = asyncio.Lock()
        self.username = ""
        self.defer_save = False
        self.app_storage = None
        self.settings = None
        for job in self.data["jobs"].values():  # прежнее правило «ждёт подтверждения» отменено — закрываем
            if job["status"] == "approval":
                job.update(status="closed", closed_at=now().isoformat(), version=job["version"] + 1)
                self.audit(None, "auto-close (approval removed)", job)
                self.publish(job)
        self.save()

    def save(self):
        if self.defer_save:
            return
        with self.db:
            self.db.execute("INSERT INTO parts_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                            (json.dumps(self.data, ensure_ascii=False),))

    def role(self, user):
        if not user:
            return None
        uid = user.id
        if self.data["leader_id"] == uid:
            return "leader"
        for name, known_id in self.data["users"].items():
            if known_id == uid:
                return name
        return None

    def register(self, user):
        name = (user.username or "").lower()
        if name == self.leader and self.data["leader_id"] is None:
            self.data["leader_id"] = user.id
        if name in MANAGERS and name not in self.data["users"]:
            self.data["users"][name] = user.id
        self.save()
        return self.role(user)

    def access(self, job, role):
        # Любой менеджер может открыть любую заявку и выполнить действие — тогда она закрепится за ним.
        return role == "leader" or role in MANAGERS

    def owns(self, job, role):
        # Списки «Мои заявки» / «Просроченные»: менеджеру — только закреплённые за ним.
        return role == "leader" or (role in MANAGERS and job["manager"] == role)

    def take_over(self, job, role, actor):
        """Менеджер выбрал действие в чужой заявке — закрепляем заявку за ним."""
        if role not in MANAGERS or job["manager"] == role:
            return False
        previous = job["manager"]
        job["manager"] = role
        job["version"] += 1
        self.audit(actor, "take over from " + (previous or "—"), job)
        self.publish(job)
        self.alert(job, f"🔁 Заявку взял @{role}" + (f" (ранее @{previous})" if previous else "") + ".",
                   dm_to=previous, dm_text=f"🔁 Вашу заявку взял {MANAGERS.get(role, role)}.")
        return True

    def audit(self, actor, action, job=None):
        self.data["history"].append({"at": now().isoformat(), "actor": actor, "action": action,
                                     "job": dict(job) if job else None})

    def enqueue(self, key, text, job=None, chat=None, **extra):
        if key not in self.data["outbox"]:
            self.data["outbox"][key] = {"chat": chat, "text": text, "job": job, "sent": False, **extra}

    def dm(self, key, username, text, job=None, rows=None):
        """Личное сообщение сотруднику (если он нажимал /start). Не доставлено — не повторяем бесконечно."""
        uid = self.data["leader_id"] if username == self.leader else self.data["users"].get(username)
        if uid:
            self.enqueue("dm:" + key, text, job, chat=uid, kind="dm", rows=rows)

    def short(self, job):
        return f"№{job['id']} {job['plate']} — {job['model']}"

    def alert(self, job, text, dm_to=None, dm_text=None):
        """Короткий сигнал в группу (там, где нужна реакция) + дубль в личку."""
        key = f"alert:{job['id']}:{len(self.data['history'])}"
        self.enqueue(key, text + "\n" + self.short(job), job["id"])
        if dm_to:
            self.dm(key, dm_to, (dm_text or text) + "\n" + self.short(job), job["id"])

    def unclear(self, key, event):
        text = ("❓ Нужно уточнить сообщение из группы ДТП: " + ", ".join(event["pending"])
                + "\nОткройте в боте «Требуют уточнения».\n" + event["link"])
        if self.data["leader_id"]:
            self.enqueue("unclear:" + key, text, chat=self.data["leader_id"], kind="dm",
                         rows=[[["Требуют уточнения", "inbox:0"]]])
        else:
            self.enqueue("unclear:" + key, f"@{self.leader}, " + text)

    def card(self, job):
        manager = "@" + job["manager"] if job["manager"] else "не определён"
        due = datetime.fromisoformat(job["due"]).strftime("%d.%m.%Y %H:%M")
        lines = [f"Заявка №{job['id']}", f"{job['plate']} — {job['model']}", f"Менеджер: {manager}",
                 f"Статус: {STATUS[job['status']]}",
                 (f"Закрыта: {datetime.fromisoformat(job['closed_at']).strftime('%d.%m.%Y %H:%M')} МСК"
                  if job["status"] == "closed" and job.get("closed_at") else f"Контроль: {due} МСК"),
                 f"Запчасти: {job['parts'] or 'список ещё не указан'}", f"Комментарий: {job['comment'] or '—'}"]
        if job.get("request"):
            lines.append("Основание закрытия: " + CHOICES.get(job["request"], job["request"]))
        return "\n".join(lines)

    def link(self, job):
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="Открыть заявку в боте", url=f"https://t.me/{self.username}?start=parts_{job}")]])

    def render(self, job):
        """Текст единственной карточки заявки в группе — всегда актуальный."""
        icon = {"closed": "✅", "approval": "🟡"}.get(job["status"], "🚨")
        text = f"{icon} ДТП — " + self.card(job).replace("Заявка №", "заявка №", 1)
        if job.get("date"):
            text += "\nДата ДТП: " + date.fromisoformat(job["date"]).strftime("%d.%m.%Y")
        link = self.data["events"].get(job["sources"][0], {}).get("link") if job["sources"] else ""
        if link:
            text += "\nИсточник: " + link
        if not job["manager"] and job["status"] != "closed":
            text += f"\n\n@{self.leader}, определите менеджера."
        return text + "\n\nОбновлено: " + now().strftime("%d.%m %H:%M")

    def publish(self, job, message=None):
        """Карточка заявки: первая публикация — новое сообщение, дальше — правка того же сообщения."""
        if f"card:{job['id']}" not in self.data["outbox"]:
            self.enqueue(f"card:{job['id']}", "", job["id"], kind="card")
        else:
            self.data["outbox"][f"edit:{job['id']}"] = {"chat": None, "text": "", "job": job["id"],
                                                        "sent": False, "kind": "edit"}
        self.save()

    def new_job(self, plate, event, date=None):
        jid = str(len(self.data["jobs"]) + 1)
        vehicle = self.fleet.get(plate, {})
        job = {"id": jid, "plate": plate, "model": vehicle.get("model", "уточнить"),
               "manager": vehicle.get("manager"), "date": date, "status": "new", "parts": "", "comment": "",
               "due": deadline(now()).isoformat(), "sources": [event], "request": None, "version": 0}
        self.data["jobs"][jid] = job
        self.audit("bot", "created", job)
        self.publish(job)
        if job["manager"]:
            self.dm(f"new:{jid}", job["manager"], "🚨 Новая заявка на вас: проверьте заказ запчастей.\n" + self.short(job), jid)
        return job

    async def show(self, bot, chat, job, role):
        if not self.access(job, role):
            await bot.send_message(chat, "Эта заявка вам недоступна.")
            return
        jid, version = job["id"], job["version"]
        rows = []
        foreign = role in MANAGERS and job["manager"] != role and job["status"] not in ("closed", "approval")
        if job["status"] == "approval":
            if role == "leader":
                rows = [[("Подтвердить закрытие", f"approve:{jid}:{version}"), ("Вернуть в работу", f"reject:{jid}:{version}")]]
        elif job["status"] == "closed":
            if role == "leader":
                rows = [[("↩️ Вернуть в работу", f"reopen:{jid}:{version}")]]
        else:
            rows = [[(label, f"state:{jid}:{version}:{code}")] for code, label in CHOICES.items()]
            rows += [[("Машина не моя", f"reassign:{jid}:{version}")], [("Комментарий / перенос срока", f"state:{jid}:{version}:keep")]]
        rows.append([("📋 К списку заявок", "list:0"), ("🏠 Меню", "home:main")])
        source = job["sources"][-1]
        msg = self.data["events"].get(source, {})
        text = self.card(job)
        if msg.get("link"):
            text += "\nИсточник: " + msg["link"]
        if foreign:
            text += ("\n\n⚠️ Заявка закреплена за " + ("@" + job["manager"] if job["manager"] else "никем")
                     + ". Если выберете действие, она перейдёт к вам.")
        await bot.send_message(chat, text, reply_markup=keyboard(rows))

    async def menu(self, bot, chat, role):
        if role == "leader":
            # Руководитель управляет двумя разделами: запчасти и контроль чатов.
            rows = [[("🚗 Контроль запчастей", "home:parts")], [("💬 Контроль чатов", "home:chats")]]
            await bot.send_message(chat, "Главное меню. Выберите раздел:", reply_markup=keyboard(rows))
            return
        await self.parts_menu(bot, chat, role)

    async def parts_menu(self, bot, chat, role):
        rows = [[("Все заявки" if role == "leader" else "Мои заявки", "list:0")], [("Просроченные", "overdue:0")]]
        if role == "leader":
            rows.append([("Требуют уточнения", "inbox:0")])
            rows.append([("⬅️ Главное меню", "home:main")])
        await bot.send_message(chat, "🚗 Контроль запчастей", reply_markup=keyboard(rows))

    async def chats_menu(self, bot, chat):
        rows = [[("📊 Статистика", "chats:stats")], [("💸 Штрафы за месяц", "chats:fines")],
                [("⚙️ Настройки", "chats:settings")], [("⬅️ Главное меню", "home:main")]]
        await bot.send_message(chat, "💬 Контроль чатов\n\nНапоминания об ответах работают в рабочих чатах "
                               "автоматически. Здесь — отчёты руководителя.\n"
                               "Статистика сотрудника: /stats @username, штрафы за месяц: /fines 2026-05.",
                               reply_markup=keyboard(rows))

    async def run_chat_report(self, bot, cb, name):
        """Вызывает существующие отчёты контроля чатов (/stats, /settings, /fines) из кнопки."""
        from types import SimpleNamespace
        from bot import main as chats
        handler = {"stats": chats.stats_command, "settings": chats.settings_command, "fines": chats.fines_command}[name]
        chat = cb.message.chat.id

        async def answer(text, **kwargs):
            return await bot.send_message(chat, text, **kwargs)

        async def answer_document(document, **kwargs):
            return await bot.send_document(chat, document, **kwargs)

        shim = SimpleNamespace(from_user=cb.from_user, chat=cb.message.chat, text="/" + name,
                               answer=answer, answer_document=answer_document)
        await handler(shim, self.app_storage, self.settings)

    async def ingest(self, message):
        before = copy.deepcopy(self.data)
        self.defer_save = True
        try:
            await self._ingest(message)
        except Exception:
            self.data = before
            raise
        finally:
            self.defer_save = False
        self.save()

    async def _ingest(self, message):
        key = f"{message.chat.id}:{message.message_id}"
        if key in self.data["events"]:
            return
        files = [getattr(getattr(message, kind, None), "file_name", None) for kind in ("document", "video", "audio")]
        text = (message.text or message.caption or "") + "\n" + "\n".join(f for f in files if f)
        found = plates(text, self.fleet)
        if not found and message.reply_to_message:
            ref = f"{message.chat.id}:{message.reply_to_message.message_id}"
            found = self.data["events"].get(ref, {}).get("plates", [])
        event = {"text": text[:3000], "plates": found, "pending": [], "done": [], "date": accident_date(text),
                 "link": f"https://t.me/c/{str(message.chat.id)[4:]}/{message.message_id}" if str(message.chat.id).startswith("-100") else ""}
        self.data["events"][key] = event
        album_id = getattr(message, "media_group_id", None)
        if album_id:
            # Альбом: подпись с номером обычно только у одного фото. Остальные фото берут номер из неё.
            album = self.data.setdefault("albums", {}).setdefault(str(album_id), {
                "keys": [], "plates": [], "date": None, "jobs": [], "flagged": False, "at": None})
            album["keys"].append(key)
            album["at"] = now().isoformat()
            event["album"] = str(album_id)
            if not found:
                if album["plates"]:
                    event["plates"] = list(album["plates"])
                    for jid in album["jobs"]:
                        self.data["jobs"][jid]["sources"].append(key)
                self.save()
                return  # без номера: ждём подпись; если её не будет — одно уточнение на весь альбом (см. tick)
            if album["plates"]:
                self.save()
                return  # номер в альбоме уже обработан
            album["plates"], album["date"] = found, event["date"]
        if not found:
            if any(getattr(message, kind, None) for kind in MEDIA) or re.search(r"дтп|авар|осмотр|ремонт|страхов", text, re.I):
                event["pending"] = ["?"]
        elif repair_notice(text):
            for plate in found:
                jobs = [j for j in self.data["jobs"].values() if j["plate"] == plate]
                job = jobs[-1] if jobs else None
                manager = (job or self.fleet.get(plate, {})).get("manager")
                tag = "@" + (manager or self.leader)
                body = (f"{plate}: осмотрен, можно брать в ремонт.\n"
                        + ("Статус заказа: " + STATUS[job["status"]] if job else "Заявки на запчасти нет — проверьте заказ.")
                        + "\n" + event["link"])
                self.enqueue(f"repair:{key}:{plate}", f"🔧 {tag}, " + body, job["id"] if job else None)
                if manager:
                    self.dm(f"repair:{key}:{plate}", manager, "🔧 " + body, job["id"] if job else None)
        else:
            for plate in found:
                jobs = [j for j in self.data["jobs"].values() if j["plate"] == plate]
                exact = [j for j in jobs if event["date"] and j["date"] == event["date"]]
                if exact:
                    exact[-1]["sources"].append(key)
                    self.audit("bot", "additional source", exact[-1])
                    self.publish(exact[-1])
                elif jobs and (not event["date"] or any(not j["date"] for j in jobs)):
                    event["pending"].append(plate)
                else:
                    self.new_job(plate, key, event["date"])
        if album_id:
            album["jobs"] = [j["id"] for j in self.data["jobs"].values() if key in j["sources"]]
            for other in album["keys"][:-1]:  # фото альбома, пришедшие раньше подписи
                self.data["events"][other]["plates"] = list(found)
                for jid in album["jobs"]:
                    self.data["jobs"][jid]["sources"].insert(0, other)
        if event["pending"]:
            self.unclear(key, event)
        self.save()

    async def edited(self, message, bot=None, **_):
        """Исправленное сообщение в группе ДТП: если раньше номер не нашли — разбираем заново."""
        async with self.lock:
            if message.chat.id != self.data["source"] or message.chat.id is None:
                raise SkipHandler
            key = f"{message.chat.id}:{message.message_id}"
            event = self.data["events"].get(key)
            if event and event["plates"]:
                return  # уже обработано — правка не создаёт дублей
            if event:
                del self.data["events"][key]
                self.data["outbox"].get("unclear:" + key, {})["sent"] = True
            await self.ingest(message)

    async def drop(self, message):
        """Удалить служебную команду из группы (если у бота есть право удалять сообщения)."""
        try:
            await message.delete()
        except (TelegramAPIError, AttributeError):
            pass

    async def tell_leader(self, bot, text):
        if self.data["leader_id"]:
            try:
                await bot.send_message(self.data["leader_id"], text)
            except TelegramAPIError:
                LOG.warning("Leader private chat is not available")

    async def message(self, message, bot, app_storage=None, settings=None):
        async with self.lock:
            if message.chat.type == "private" and (message.text or "").startswith("/start") and app_storage:
                from bot.main import register_user_from_message
                await register_user_from_message(app_storage, message, settings)
            return await self._message(message, bot)

    async def _message(self, message, bot):
        text = (message.text or "").strip()
        if message.chat.type != "private":
            if text.startswith("/parts_bind"):
                # В группах бот молчит: ответ — руководителю в личку, саму команду убираем из чата.
                await self.drop(message)
                if not message.from_user or (message.from_user.username or "").lower() != self.leader:
                    return
                self.register(message.from_user)
                if self.role(message.from_user) != "leader":
                    return
                title = f"«{getattr(message.chat, 'title', None) or message.chat.id}»"
                arg = text.split()[-1]
                if arg not in ("source", "target"):
                    await self.tell_leader(bot, f"Группа {title}: укажите /parts_bind source (группа ДТП) "
                                                "или /parts_bind target (группа заказа запчастей).")
                    return
                other = "target" if arg == "source" else "source"
                if message.chat.id == self.data[other]:
                    await self.tell_leader(bot, f"Группа {title} уже закреплена как "
                                                + ("группа заказа" if other == "target" else "группа ДТП")
                                                + ". Для ДТП и заказа нужны разные группы.")
                    return
                self.data[arg] = message.chat.id
                self.audit(message.from_user.id, "bind " + arg)
                self.save()
                await self.tell_leader(bot, f"✅ Группа {title} закреплена: "
                                            + ("источник ДТП" if arg == "source" else "заказ запчастей") + ".")
                return
            if text.startswith("/") and message.chat.id in (self.data["source"], self.data["target"]):
                return  # служебные команды в рабочих группах игнорируем молча
            if message.chat.id == self.data["source"]:
                # Анонимный админ пишет от имени группы (from_user — служебный бот, есть sender_chat) — тоже читаем.
                if not message.from_user or not message.from_user.is_bot or getattr(message, "sender_chat", None):
                    await self.ingest(message)
                return
            if message.chat.id == self.data["target"]:
                return
            raise SkipHandler
        if not message.from_user:
            raise SkipHandler
        role = self.register(message.from_user)
        if text.startswith("/start"):
            if not role:
                raise SkipHandler
            self.data["sessions"].pop(str(message.from_user.id), None)
            self.save()
            commands = [BotCommand(command="start", description="Открыть меню")]
            if role == "leader":
                commands += [BotCommand(command=c, description=d) for c, d in
                             [("stats", "Статистика чатов"), ("settings", "Настройки чатов"), ("fines", "Отчёт по штрафам")]]
            await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=message.chat.id))
            match = re.fullmatch(r"/start(?:@\w+)? parts_(\d+)", text)
            if match and match[1] in self.data["jobs"]:
                await self.show(bot, message.chat.id, self.data["jobs"][match[1]], role)
            else:
                await self.menu(bot, message.chat.id, role)
            return
        session = self.data["sessions"].get(str(message.from_user.id))
        if not session:
            if role in MANAGERS:
                await self.menu(bot, message.chat.id, role)
                return
            raise SkipHandler
        if text.startswith("/"):
            self.data["sessions"].pop(str(message.from_user.id), None)
            self.save()
            raise SkipHandler
        if not text or len(text) > 1200:
            await message.answer("Пришлите текст до 1200 символов. /start — отмена.")
            return
        if session.get("step") == "plate":
            if role != "leader":
                return
            plate = normalize(text)
            if not plates(plate, self.fleet):
                await message.answer("Не удалось распознать госномер.")
                return
            event = self.data["events"][session["event"]]
            event["pending"] = [plate]
            event.setdefault("done", []).append("number clarified")
            self.data["sessions"].pop(str(message.from_user.id))
            self.save()
            await message.answer("Номер уточнён. Откройте «Требуют уточнения» для выбора ДТП.")
            return
        job = self.data["jobs"][session["job"]]
        if not self.access(job, role) or session["version"] != job["version"] or job["status"] in ("closed", "approval"):
            self.data["sessions"].pop(str(message.from_user.id), None)
            self.save()
            await message.answer("Заявка изменена. Откройте её заново.")
            return
        step = session["step"]
        chat = message.chat.id
        if step == "parts":
            session["parts"] = text
            session["step"] = "comment"
            self.save()
            await self.prompt(bot, chat, session, job)
        elif step == "comment":
            if text.lower() == "нет" and session["action"] in COMMENT_REQUIRED:
                await message.answer("Для этого действия нужна причина или пояснение.")
                return
            await self.after_comment(bot, chat, message.from_user.id, session, job, role,
                                     "" if text.lower() == "нет" else text)
        elif step == "date":
            try:
                due = parse_date(text, now())
            except ValueError:
                await message.answer("Нужна будущая дата в формате ДД.ММ.ГГГГ ЧЧ:ММ, Пн–Пт, 09:00–17:59 МСК. "
                                     "Или выберите дату кнопкой выше.")
                return
            self.finish(message.from_user.id, session, job, due)
            await self.show(bot, chat, job, role)

    async def after_comment(self, bot, chat, actor, session, job, role, comment):
        session["comment"] = comment
        if session["action"] in ("paid", "repaired", "unneeded"):
            self.finish(actor, session, job, None)
            await bot.send_message(chat, "✅ Заявка закрыта. Руководитель получил уведомление.")
            await self.show(bot, chat, job, role)
        else:
            session["step"] = "date"
            self.save()
            await self.prompt(bot, chat, session, job)

    @staticmethod
    def quick_dates():
        """Ближайшие рабочие слоты для кнопок выбора срока."""
        current = now()
        result = []
        if current.weekday() < 5 and current.hour < 15:
            result.append(current.replace(hour=16, minute=0, second=0, microsecond=0))
        day = current.date()
        while len(result) < 5:
            day += timedelta(days=1)
            if day.weekday() < 5:
                for hour in (10, 16):
                    if len(result) < 5:
                        result.append(datetime(day.year, day.month, day.day, hour, tzinfo=TZ))
        return result

    async def prompt(self, bot, chat, session, job):
        """Сообщение текущего шага с кнопками «Назад» и «Отмена»."""
        label = CHOICES.get(session["action"], "Комментарий / перенос срока")
        head = f"Заявка №{job['id']} {job['plate']} — {label}\n\n"
        step = session["step"]
        rows = []
        if step == "parts":
            text = "Шаг 1. Напишите полный список запчастей одним сообщением."
            if job["parts"]:
                text += "\n\nСейчас указано: " + job["parts"]
                rows.append([("Оставить текущий список", "flow:keepparts")])
        elif step == "comment":
            hints = {"partial": "Что осталось заказать?", "blocked": "Почему не можем заказать?",
                     "repaired": "Подтвердите, что ремонт по этому ДТП завершён (кратко).",
                     "unneeded": "Почему ремонт не требуется?", "keep": "Комментарий или причина переноса срока.",
                     "disassembly": "Комментарий по разбору (если нужен).", "payment": "Комментарий по оплате (если нужен).",
                     "paid": "Комментарий (если нужен).", "order": "Комментарий (если нужен)."}
            text = "Комментарий. " + hints.get(session["action"], "")
            if session["action"] not in COMMENT_REQUIRED:
                rows.append([("Без комментария", "flow:skip")])
        else:
            text = "Когда проверить результат? Выберите кнопку или напишите дату: 27.11.2026 16:00 (Пн–Пт, 09:00–17:59 МСК)."
            dates = self.quick_dates()
            for i in range(0, len(dates), 2):
                rows.append([(d.strftime("%a %d.%m %H:%M").replace("Mon", "Пн").replace("Tue", "Вт")
                              .replace("Wed", "Ср").replace("Thu", "Чт").replace("Fri", "Пт"),
                              "flow:due:" + d.strftime("%Y%m%d%H%M")) for d in dates[i:i + 2]])
        rows.append([("⬅️ Назад", "flow:back"), ("✖️ Отмена", "flow:cancel")])
        await bot.send_message(chat, head + text, reply_markup=keyboard(rows))

    async def flow(self, bot, cb, role, args):
        """Кнопки внутри пошагового ввода: назад, отмена, без комментария, быстрый срок."""
        uid = str(cb.from_user.id)
        chat = cb.message.chat.id
        session = self.data["sessions"].get(uid)
        if not session:
            await cb.answer("Это действие уже завершено.", show_alert=True)
            return
        if session.get("step") == "plate":
            self.data["sessions"].pop(uid, None)
            self.save()
            await cb.answer("Отменено.")
            await self.menu(bot, chat, role)
            return
        job = self.data["jobs"][session["job"]]
        if session["version"] != job["version"] or job["status"] in ("closed", "approval"):
            self.data["sessions"].pop(uid, None)
            self.save()
            await cb.answer("Заявка изменилась. Откройте её заново.", show_alert=True)
            return
        await cb.answer()
        what = args[1]
        need_parts = session["action"] in NEED_PARTS
        if what == "cancel":
            self.data["sessions"].pop(uid, None)
            self.save()
            await bot.send_message(chat, "Действие отменено, заявка не изменилась.")
            await self.show(bot, chat, job, role)
        elif what == "back":
            if session["step"] == "date":
                session["step"] = "comment"
            elif session["step"] == "comment" and need_parts:
                session["step"] = "parts"
            else:
                self.data["sessions"].pop(uid, None)
                self.save()
                await self.show(bot, chat, job, role)
                return
            self.save()
            await self.prompt(bot, chat, session, job)
        elif what == "keepparts" and session["step"] == "parts":
            session["parts"] = job["parts"]
            session["step"] = "comment"
            self.save()
            await self.prompt(bot, chat, session, job)
        elif what == "skip" and session["step"] == "comment" and session["action"] not in COMMENT_REQUIRED:
            await self.after_comment(bot, chat, cb.from_user.id, session, job, role, "")
        elif what == "due" and session["step"] == "date":
            due = datetime.strptime(args[2], "%Y%m%d%H%M").replace(tzinfo=TZ)
            if due <= now():
                await bot.send_message(chat, "Это время уже прошло, выберите другое.")
                await self.prompt(bot, chat, session, job)
                return
            self.finish(cb.from_user.id, session, job, due)
            await self.show(bot, chat, job, role)

    def finish(self, actor, session, job, due):
        action = session["action"]
        job.update(parts=session.get("parts", job["parts"]), comment=session["comment"], version=job["version"] + 1)
        if action in CLOSING:
            job.update(status="closed", request=action, closed_at=now().isoformat())
        else:
            job.update(status=job["status"] if action == "keep" else action, due=due.isoformat(), request=None)
        self.data["sessions"].pop(str(actor), None)
        self.audit(actor, action, job)
        self.publish(job)
        if action in CLOSING:
            who = MANAGERS.get(job["manager"], "менеджер")
            self.dm(f"closed:{job['id']}:{job['version']}", self.leader,
                    f"✅ {who} закрыл заявку: {CHOICES[action]}.\n{self.short(job)}"
                    + (f"\nЗапчасти: {job['parts']}" if job["parts"] else "")
                    + (f"\nКомментарий: {job['comment']}" if job["comment"] else ""),
                    job["id"], rows=[[["Открыть", f"open:{job['id']}"],
                                      ["↩️ Вернуть в работу", f"reopen:{job['id']}:{job['version']}"]]])

    async def callback(self, callback, bot):
        async with self.lock:
            try:
                await self._callback(callback, bot)
            except (KeyError, ValueError, IndexError):
                await callback.answer("Кнопка устарела. Откройте меню заново.", show_alert=True)

    async def _callback(self, cb, bot):
        if not cb.message or cb.message.chat.type != "private":
            await cb.answer("Действия доступны в личной переписке с ботом.", show_alert=True)
            return
        role = self.role(cb.from_user)
        if not role:
            await cb.answer("Нет доступа.", show_alert=True)
            return
        args = cb.data.split(":")[1:]
        action = args[0]
        chat = cb.message.chat.id
        if action == "flow":
            await self.flow(bot, cb, role, args)
            return
        if action == "home":
            await cb.answer()
            if args[1] == "parts":
                await self.parts_menu(bot, chat, role)
            elif args[1] == "chats" and role == "leader":
                await self.chats_menu(bot, chat)
            else:
                await self.menu(bot, chat, role)
            return
        if action == "chats":
            if role != "leader" or self.app_storage is None:
                await cb.answer("Нет доступа.", show_alert=True)
                return
            await cb.answer()
            await self.run_chat_report(bot, cb, args[1])
            return
        if action in ("list", "overdue"):
            offset = int(args[1])
            jobs = [j for j in self.data["jobs"].values() if self.owns(j, role) and j["status"] != "closed"
                    and (action != "overdue" or datetime.fromisoformat(j["due"]) < now())]
            rows = [[(f"№{j['id']} {j['plate']} — {STATUS[j['status']]}", f"open:{j['id']}")] for j in jobs[offset:offset + 15]]
            nav = []
            if offset:
                nav.append(("⬅️ Предыдущие", f"{action}:{max(offset - 15, 0)}"))
            if offset + 15 < len(jobs):
                nav.append(("Далее ➡️", f"{action}:{offset + 15}"))
            if nav:
                rows.append(nav)
            rows.append([("🏠 Меню", "home:main" if role == "leader" else "home:parts")])
            await cb.answer()
            title = "Просроченные заявки" if action == "overdue" else ("Все заявки" if role == "leader" else "Мои заявки")
            await bot.send_message(chat, title if jobs else "Активных заявок нет.", reply_markup=keyboard(rows))
            return
        if action == "inbox" and role == "leader":
            entries = [(key, ev) for key, ev in self.data["events"].items() if ev["pending"]]
            offset = int(args[1])
            await cb.answer()
            if offset >= len(entries):
                await bot.send_message(chat, "Нет сообщений для уточнения.", reply_markup=keyboard([[("🏠 Меню", "home:main")]]))
                return
            key, event = entries[offset]
            plate = event["pending"][0]
            # Compact event index instead of potentially long chat/message key in callback data.
            eid = list(self.data["events"]).index(key)
            token = len(event.get("done", []))
            rows = [[("Указать госномер", f"resolve:{eid}:plate:{token}")]] if plate == "?" else [[("Это новое ДТП", f"resolve:{eid}:new:{token}")]]
            if plate != "?":
                matches = [j for j in self.data["jobs"].values() if j["plate"] == plate]
                rows += [[(f"Дополнить №{j['id']} ({j['date'] or 'дата неизвестна'})", f"resolve:{eid}:{j['id']}:{token}")] for j in matches[-8:]]
            rows.append([("Не относится к контролю", f"resolve:{eid}:ignore:{token}")])
            if offset + 1 < len(entries):
                rows.append([("Следующее сообщение", f"inbox:{offset + 1}")])
            rows.append([("🏠 Меню", "home:main")])
            await bot.send_message(chat, f"Уточнение: {plate}\n{event['text']}\n{event['link']}", reply_markup=keyboard(rows))
            return
        if action == "resolve" and role == "leader":
            key = list(self.data["events"])[int(args[1])]
            event = self.data["events"][key]
            if not event["pending"] or int(args[3]) != len(event.get("done", [])):
                await cb.answer("Уже обработано.")
                return
            plate, choice = event["pending"][0], args[2]
            if choice == "plate":
                self.data["sessions"][str(cb.from_user.id)] = {"step": "plate", "event": key}
                self.save()
                await cb.answer()
                await bot.send_message(chat, "Введите госномер. /start — отмена.")
                return
            if choice == "new" and plate != "?":
                self.new_job(plate, key, event["date"])
            elif choice != "ignore":
                job = self.data["jobs"][choice]
                if job["plate"] != plate:
                    raise ValueError()
                job["sources"].append(key)
                self.audit(cb.from_user.id, "source linked", job)
                self.publish(job, "Дополнительная информация")
            event.setdefault("done", []).append(event["pending"].pop(0))
            self.audit(cb.from_user.id, "resolve " + choice)
            self.save()
            await cb.answer("Сохранено.")
            return
        job = self.data["jobs"][args[1]]
        if not self.access(job, role):
            await cb.answer("Это чужая заявка.", show_alert=True)
            return
        if action == "open":
            await cb.answer()
            await self.show(bot, chat, job, role)
            return
        if int(args[2]) != job["version"] or (job["status"] == "closed" and action != "reopen"):
            await cb.answer("Карточка изменилась. Откройте её заново.", show_alert=True)
            return
        if action in ("approve", "reject", "reopen"):
            if role != "leader" or job["status"] != ("closed" if action == "reopen" else "approval"):
                await cb.answer("Решение принимает руководитель." if role != "leader" else "Заявка уже изменилась.",
                                show_alert=True)
                return
            if action == "reopen":
                action = "reject"
            job["status"] = "closed" if action == "approve" else "new"
            if action == "approve":
                job["closed_at"] = now().isoformat()
            if action == "reject":
                job.update(due=deadline(now()).isoformat(), request=None, closed_at=None)
            job["version"] += 1
            self.audit(cb.from_user.id, action, job)
            self.publish(job)
            if action == "reject" and job["manager"]:
                self.alert(job, f"↩️ @{job['manager']}, руководитель вернул заявку в работу.", dm_to=job["manager"],
                           dm_text="↩️ Руководитель вернул заявку в работу.")
            await bot.send_message(chat, "✅ Заявка закрыта." if action == "approve" else "↩️ Заявка возвращена в работу.")
            await self.show(bot, chat, job, role)
        elif job["status"] == "approval":
            await cb.answer("Ожидается решение руководителя.", show_alert=True)
            return
        elif action == "reassign":
            rows = [[(name, f"assign:{job['id']}:{job['version']}:{username}")]
                    for username, name in MANAGERS.items() if username != job["manager"]]
            rows.append([("⬅️ Назад к заявке", f"open:{job['id']}")])
            await bot.send_message(chat, f"Заявка №{job['id']} {job['plate']}. Кому передать?", reply_markup=keyboard(rows))
        elif action == "assign" and args[3] in MANAGERS:
            previous = job["manager"]
            job["manager"] = args[3]
            job["version"] += 1
            self.audit(cb.from_user.id, "reassign", job)
            self.publish(job)
            self.alert(job, f"➡️ Заявка передана @{args[3]}" + (f" (ранее @{previous})" if previous else "") + ".",
                       dm_to=args[3], dm_text="➡️ Вам передали заявку.")
            await bot.send_message(chat, f"✅ Заявка №{job['id']} передана: {MANAGERS[args[3]]}.")
            await self.show(bot, chat, job, role)
        elif action == "state" and args[3] in {*CHOICES, "keep"}:
            state = args[3]
            if self.take_over(job, role, cb.from_user.id):
                await bot.send_message(chat, f"Заявка №{job['id']} теперь закреплена за вами.")
            need_parts = state in NEED_PARTS
            session = {"job": job["id"], "version": job["version"], "action": state,
                       "step": "parts" if need_parts else "comment"}
            self.data["sessions"][str(cb.from_user.id)] = session
            self.save()
            await self.prompt(bot, chat, session, job)
        await cb.answer()

    def flush_albums(self, current):
        """Альбом без номера ни в одной подписи — одно уточнение руководителю на весь альбом."""
        for aid, album in list(self.data.get("albums", {}).items()):
            at = datetime.fromisoformat(album["at"])
            if not album["plates"] and not album["flagged"] and current - at > timedelta(seconds=20):
                album["flagged"] = True
                key = album["keys"][0]
                event = self.data["events"][key]
                event["pending"] = ["?"]
                event["text"] = (event["text"] + f"\n(альбом: {len(album['keys'])} файлов)").strip()
                self.unclear(key, event)
            if current - at > timedelta(days=2):
                del self.data["albums"][aid]

    def overdue_reminders(self, current):
        slot = "16" if current.hour >= 16 else "10" if current.hour >= 10 else None
        for job in self.data["jobs"].values():
            if job["status"] == "closed" or datetime.fromisoformat(job["due"]) > current:
                continue
            if job["status"] != "new" and not slot:
                continue
            if job["status"] != "new" and current.replace(hour=int(slot), minute=0, second=0, microsecond=0) < datetime.fromisoformat(job["due"]):
                continue
            # Первый срок (2 рабочих часа) — сразу; дальше — дважды в день, 10:00 и 16:00.
            initial = f"late:{job['id']}:{job['version']}:first"
            key = initial if initial not in self.data["outbox"] else (f"late:{job['id']}:{job['version']}:{current.date()}:{slot}" if slot else None)
            if not key:
                continue
            due = datetime.fromisoformat(job["due"]).strftime("%d.%m %H:%M")
            approval = job["status"] == "approval"
            who = "" if approval else ("@" + job["manager"] if job["manager"] else "")
            self.enqueue(key, f"⏰ Просрочено. @{self.leader} {who}\n{self.short(job)}\n"
                              f"Статус: {STATUS[job['status']]}, срок был {due} МСК", job["id"])
            if approval:
                self.dm(key, self.leader, f"⏰ Ждёт вашего подтверждения с {due}.\n{self.short(job)}", job["id"])
            elif job["manager"]:
                self.dm(key, job["manager"], f"⏰ Просрочена ваша заявка (срок был {due}).\n{self.short(job)}\n"
                                             "Откройте её и обновите статус или перенесите срок с причиной.", job["id"])
            if key == initial and slot:
                self.data["outbox"][f"late:{job['id']}:{job['version']}:{current.date()}:{slot}"] = {"sent": True}

    def digest(self, current):
        """Утренняя сводка в личку, Пн–Пт 09:30–12:00 (один раз в день)."""
        if not ((current.hour == 9 and current.minute >= 30) or 10 <= current.hour < 12):
            return
        day = current.date().isoformat()
        active = [j for j in self.data["jobs"].values() if j["status"] != "closed"]
        for name in [*MANAGERS, self.leader]:
            key = f"digest:{day}:{name}"
            if "dm:" + key in self.data["outbox"]:
                continue
            leader = name == self.leader
            mine = active if leader else [j for j in active if j["manager"] == name and j["status"] != "approval"]
            inbox = sum(1 for ev in self.data["events"].values() if ev["pending"]) if leader else 0
            if not mine and not inbox:
                continue
            late = {j["id"] for j in mine if datetime.fromisoformat(j["due"]) < current}
            text = (f"☀️ Доброе утро! {'Все заявки' if leader else 'Ваши заявки'} по запчастям: "
                    f"{len(mine)} в работе, просрочено {len(late)}.")
            if leader and inbox:
                text += f"\nТребуют уточнения: {inbox}."
            order = sorted(mine, key=lambda j: (j["id"] not in late, j["due"]))[:10]
            rows = [[[("⏰ " if j["id"] in late else "") + f"№{j['id']} {j['plate']} — {STATUS[j['status']]}",
                      f"open:{j['id']}"]] for j in order]
            if len(mine) > 10:
                rows.append([["Все заявки", "list:0"]])
            if inbox:
                rows.append([["Требуют уточнения", "inbox:0"]])
            self.dm(key, name, text, rows=rows)

    async def tick(self, bot, current=None):
        async with self.lock:
            current = current or now()
            self.flush_albums(current)
            if self.data["target"] and working(current):
                self.overdue_reminders(current)
                self.digest(current)
            self.save()
            for key, item in list(self.data["outbox"].items()):
                kind = item.get("kind")
                chat = item.get("chat") or self.data["target"]
                if item.get("sent") or not chat:
                    continue
                if key.startswith(("late:", "dm:late:")):
                    jid, version = key.removeprefix("dm:").split(":")[1:3]
                    job = self.data["jobs"][jid]
                    if job["status"] == "closed" or job["version"] != int(version):
                        item["sent"] = True
                        self.save()
                        continue
                job = self.data["jobs"].get(str(item["job"])) if item.get("job") else None
                markup = keyboard(item["rows"]) if item.get("rows") else (self.link(item["job"]) if item.get("job") else None)
                try:
                    if kind == "edit":
                        if not job.get("card"):
                            if not self.data["outbox"].get(f"card:{job['id']}", {}).get("sent"):
                                continue  # карточка ещё не отправлена — правка подождёт
                            kind = "card"
                        else:
                            try:
                                await bot.edit_message_text(self.render(job), chat_id=self.data["target"],
                                                            message_id=job["card"], reply_markup=markup)
                            except TelegramBadRequest as error:
                                if "not modified" not in str(error).lower():
                                    kind = "card"  # карточку удалили — публикуем заново
                    if kind == "card":
                        sent = await bot.send_message(self.data["target"], self.render(job), reply_markup=markup)
                        job["card"] = getattr(sent, "message_id", None)
                    elif kind != "edit":
                        await bot.send_message(chat, item["text"], reply_markup=markup)
                except TelegramAPIError:
                    if kind == "dm":
                        LOG.warning("Private notification %s not delivered (user has not started the bot?)", key)
                        item["sent"] = True
                        self.save()
                        continue
                    LOG.exception("Procurement notification failed; retained for retry")
                    continue
                item["sent"] = True
                self.save()


async def install(dispatcher, bot, settings, app_storage):
    """Opt-in. No deployment side effects until PARTS_ENABLED=true."""
    if os.getenv("PARTS_ENABLED", "false").lower() != "true":
        return None, None
    fleet_path = Path(os.environ["PARTS_FLEET_PATH"])
    fleet = json.loads(fleet_path.read_text())
    service = Parts(str(settings.database_path) + ".parts", fleet, settings.leader_username)
    service.username = (await bot.get_me()).username
    service.app_storage = app_storage
    service.settings = settings
    for name in [settings.leader_username, *MANAGERS]:
        user = await app_storage.get_user_by_username(name)
        if user:
            if name == settings.leader_username and service.data["leader_id"] is None:
                service.data["leader_id"] = user["user_id"]
            elif name in MANAGERS:
                service.data["users"].setdefault(name, user["user_id"])
    service.save()
    # Existing users have already pressed Start; immediately set their scoped menus.
    for name, uid in {**service.data["users"], settings.leader_username: service.data["leader_id"]}.items():
        if not uid:
            continue
        commands = [BotCommand(command="start", description="Открыть меню")]
        if name == settings.leader_username:
            commands += [BotCommand(command=c, description=d) for c, d in
                         [("stats", "Статистика чатов"), ("settings", "Настройки чатов"), ("fines", "Отчёт по штрафам")]]
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=uid))
    router = Router(name="parts")
    router.message.register(service.message)
    router.edited_message.register(service.edited)
    router.callback_query.register(service.callback, F.data.startswith("parts:"))
    dispatcher.include_router(router)

    async def loop():
        while True:
            try:
                await service.tick(bot)
            except Exception:
                LOG.exception("Procurement scheduler failed")
            await asyncio.sleep(30)

    return service, asyncio.create_task(loop())
