"""Private procurement workflows. Separate state and permissions from response reminders."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup

LOG = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Moscow")
MANAGERS = {"pagorodu": "Арман", "lalalas19": "Рафаэль", "serb_98": "Александр Петрович"}
STATUS = {
    "new": "Требуется ответ", "order": "Не заказано", "partial": "Заказано частично",
    "payment": "Заказано — ждём оплату", "disassembly": "Ждём разбор",
    "blocked": "Не можем заказать", "approval": "Ждём подтверждение руководителя",
    "closed": "Закрыто",
}
CHOICES = {"order": "Нужно заказать", "partial": "Заказано частично", "payment": "Всё заказано — ждём оплату",
           "paid": "Всё заказано и оплачено", "disassembly": "Ждём разбор", "blocked": "Не можем заказать",
           "repaired": "Уже отремонтирована", "unneeded": "Ремонт не требуется"}
TRANS = str.maketrans("ABEKMHOPCTYX", "АВЕКМНОРСТУХ")


def now():
    return datetime.now(TZ)


def normalize(value):
    return re.sub(r"[\s_-]+", "", value.upper()).translate(TRANS)


def plates(text, fleet):
    text = text.upper().translate(TRANS)
    found = re.findall(r"(?<![А-ЯA-Z0-9])[АВЕКМНОРСТУХ]\s*\d{3}\s*[АВЕКМНОРСТУХ]{2}\s*\d{2,3}(?!\d)", text)
    result = {normalize(p) for p in found}
    # Exact known unusual identifiers, e.g. ЕС53674. Never guess their format.
    for plate in fleet:
        if re.search(r"(?<![А-ЯA-Z0-9])" + re.escape(plate) + r"(?![А-ЯA-Z0-9])", text):
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


def accident_date(text):
    match = re.search(r"дтп\s*(?:от\s*)?(\d{2}\.\d{2}\.\d{4})", text, re.I)
    if match:
        try:
            return datetime.strptime(match[1], "%d.%m.%Y").date().isoformat()
        except ValueError:
            pass
    return None


def repair_notice(text):
    value = text.lower().replace("ё", "е")
    if "?" in value or re.search(r"(?:нельзя|не\s+можно|не\s+разрешено|не\s+осмотрен)", value):
        return False
    return bool(re.search(r"можно\s+(?:брать\s+)?в\s+ремонт|разрешен[оа]?\s+.*ремонт", value))


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
        return role == "leader" or (role in MANAGERS and job["manager"] == role)

    def audit(self, actor, action, job=None):
        self.data["history"].append({"at": now().isoformat(), "actor": actor, "action": action,
                                     "job": dict(job) if job else None})

    def enqueue(self, key, text, job=None, chat=None):
        if key not in self.data["outbox"]:
            self.data["outbox"][key] = {"chat": chat, "text": text, "job": job, "sent": False}

    def card(self, job):
        manager = "@" + job["manager"] if job["manager"] else "не определён"
        due = datetime.fromisoformat(job["due"]).strftime("%d.%m.%Y %H:%M")
        lines = [f"Заявка №{job['id']}", f"{job['plate']} — {job['model']}", f"Менеджер: {manager}",
                 f"Статус: {STATUS[job['status']]}", f"Контроль: {due} МСК",
                 f"Запчасти: {job['parts'] or 'список ещё не указан'}", f"Комментарий: {job['comment'] or '—'}"]
        if job.get("request"):
            lines.append("Основание закрытия: " + CHOICES.get(job["request"], job["request"]))
        return "\n".join(lines)

    def link(self, job):
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="Открыть заявку в боте", url=f"https://t.me/{self.username}?start=parts_{job}")]])

    def publish(self, job, message="Обновление заявки"):
        key = f"job:{job['id']}:{len(self.data['history'])}"
        self.enqueue(key, message + "\n\n" + self.card(job), job["id"])
        self.save()

    def new_job(self, plate, event, date=None):
        jid = str(len(self.data["jobs"]) + 1)
        vehicle = self.fleet.get(plate, {})
        job = {"id": jid, "plate": plate, "model": vehicle.get("model", "уточнить"),
               "manager": vehicle.get("manager"), "date": date, "status": "new", "parts": "", "comment": "",
               "due": deadline(now()).isoformat(), "sources": [event], "request": None, "version": 0}
        self.data["jobs"][jid] = job
        self.audit("bot", "created", job)
        self.publish(job, "Новое сообщение о ДТП" + (f"\n@{self.leader}, определите менеджера." if not job["manager"] else ""))
        return job

    async def show(self, bot, chat, job, role):
        if not self.access(job, role):
            await bot.send_message(chat, "Эта заявка вам недоступна.")
            return
        jid, version = job["id"], job["version"]
        rows = []
        if job["status"] == "approval":
            if role == "leader":
                rows = [[("Подтвердить закрытие", f"approve:{jid}:{version}"), ("Вернуть в работу", f"reject:{jid}:{version}")]]
        elif job["status"] != "closed":
            rows = [[(label, f"state:{jid}:{version}:{code}")] for code, label in CHOICES.items()]
            rows += [[("Машина не моя", f"reassign:{jid}:{version}")], [("Комментарий / перенос срока", f"state:{jid}:{version}:keep")]]
        rows.append([("К списку заявок", "list:0")])
        source = job["sources"][-1]
        msg = self.data["events"].get(source, {})
        text = self.card(job)
        if msg.get("link"):
            text += "\nИсточник: " + msg["link"]
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
        text = (message.text or message.caption or "") + "\n" + (message.document.file_name or "" if message.document else "")
        found = plates(text, self.fleet)
        if not found and message.reply_to_message:
            ref = f"{message.chat.id}:{message.reply_to_message.message_id}"
            found = self.data["events"].get(ref, {}).get("plates", [])
        event = {"text": text[:3000], "plates": found, "pending": [], "done": [], "date": accident_date(text),
                 "link": f"https://t.me/c/{str(message.chat.id)[4:]}/{message.message_id}" if str(message.chat.id).startswith("-100") else ""}
        self.data["events"][key] = event
        if not found:
            if message.document or message.photo or re.search(r"дтп|авар|осмотр|ремонт", text, re.I):
                event["pending"] = ["?"]
        elif repair_notice(text):
            for plate in found:
                jobs = [j for j in self.data["jobs"].values() if j["plate"] == plate]
                job = jobs[-1] if jobs else None
                manager = (job or self.fleet.get(plate, {})).get("manager")
                tag = "@" + (manager or self.leader)
                self.enqueue(f"repair:{key}:{plate}", f"{tag}, {plate}: осмотрен, можно брать в ремонт.\n"
                             + ("Статус заказа: " + STATUS[job["status"]] if job else "Заявки на запчасти нет — проверьте заказ.")
                             + "\n" + event["link"], job["id"] if job else None)
        else:
            for plate in found:
                jobs = [j for j in self.data["jobs"].values() if j["plate"] == plate]
                exact = [j for j in jobs if event["date"] and j["date"] == event["date"]]
                if exact:
                    exact[-1]["sources"].append(key)
                    self.audit("bot", "additional source", exact[-1])
                    self.publish(exact[-1], "Дополнительная информация по ДТП")
                elif jobs and (not event["date"] or any(not j["date"] for j in jobs)):
                    event["pending"].append(plate)
                else:
                    self.new_job(plate, key, event["date"])
        if event["pending"]:
            self.enqueue("unclear:" + key, f"@{self.leader}, нужно уточнить сообщение из группы ДТП: "
                         + ", ".join(event["pending"]) + "\nОткройте в боте «Требуют уточнения».\n" + event["link"])
        self.save()

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
                if not message.from_user or (message.from_user.username or "").lower() != self.leader:
                    return
                self.register(message.from_user)
                if self.role(message.from_user) != "leader":
                    return
                arg = text.split()[-1]
                if arg not in ("source", "target"):
                    await message.answer("Укажите /parts_bind source в ДТП или /parts_bind target в закупе.")
                    return
                other = "target" if arg == "source" else "source"
                if message.chat.id == self.data[other]:
                    await message.answer("Для ДТП и закупа нужны разные группы.")
                    return
                self.data[arg] = message.chat.id
                self.audit(message.from_user.id, "bind " + arg)
                self.save()
                await message.answer("Группа закреплена: " + ("ДТП" if arg == "source" else "контроль закупа"))
                return
            if message.chat.id == self.data["source"]:
                if message.from_user and not message.from_user.is_bot:
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
        if step == "parts":
            session["parts"] = text
            session["step"] = "comment"
            self.save()
            await message.answer("Комментарий: что осталось заказать / причина ожидания или переноса. Если пояснения не нужны — напишите «нет».")
        elif step == "comment":
            if session["action"] in ("partial", "blocked", "repaired", "unneeded", "keep") and text.lower() == "нет":
                await message.answer("Для этого действия нужна причина или пояснение.")
                return
            session["comment"] = "" if text.lower() == "нет" else text
            if session["action"] in ("paid", "repaired", "unneeded"):
                self.finish(message.from_user.id, session, job, None)
                await message.answer("Отправлено руководителю на подтверждение.")
            else:
                session["step"] = "date"
                self.save()
                await message.answer("Когда проверить результат? Дата и время МСК, например: 27.11.2026 16:00. Пн–Пт, 09:00–17:59.")
        elif step == "date":
            try:
                due = parse_date(text, now())
            except ValueError:
                await message.answer("Нужна будущая дата в формате ДД.ММ.ГГГГ ЧЧ:ММ, Пн–Пт, 09:00–17:59 МСК.")
                return
            self.finish(message.from_user.id, session, job, due)
            await self.show(bot, message.chat.id, job, role)

    def finish(self, actor, session, job, due):
        action = session["action"]
        job.update(parts=session.get("parts", job["parts"]), comment=session["comment"], version=job["version"] + 1)
        if action in ("paid", "repaired", "unneeded"):
            job.update(status="approval", request=action, due=deadline(now()).isoformat())
        else:
            job.update(status=job["status"] if action == "keep" else action, due=due.isoformat())
        self.data["sessions"].pop(str(actor), None)
        self.audit(actor, action, job)
        self.publish(job, f"@{self.leader}, подтвердите закрытие" if job["status"] == "approval" else "Обновление заявки")

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
            jobs = [j for j in self.data["jobs"].values() if self.access(j, role) and j["status"] != "closed"
                    and (action != "overdue" or datetime.fromisoformat(j["due"]) < now())]
            rows = [[(f"№{j['id']} {j['plate']} — {STATUS[j['status']]}", f"open:{j['id']}")] for j in jobs[offset:offset + 15]]
            if offset + 15 < len(jobs):
                rows.append([("Далее", f"{action}:{offset + 15}")])
            await cb.answer()
            await bot.send_message(chat, "Заявки" if jobs else "Активных заявок нет.", reply_markup=keyboard(rows) if rows else None)
            return
        if action == "inbox" and role == "leader":
            entries = [(key, ev) for key, ev in self.data["events"].items() if ev["pending"]]
            offset = int(args[1])
            await cb.answer()
            if offset >= len(entries):
                await bot.send_message(chat, "Нет сообщений для уточнения.")
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
        if int(args[2]) != job["version"] or job["status"] == "closed":
            await cb.answer("Карточка изменилась. Откройте её заново.", show_alert=True)
            return
        if action in ("approve", "reject"):
            if role != "leader" or job["status"] != "approval":
                await cb.answer("Решение принимает руководитель.", show_alert=True)
                return
            job["status"] = "closed" if action == "approve" else "new"
            if action == "reject":
                job.update(due=deadline(now()).isoformat(), request=None)
            job["version"] += 1
            self.audit(cb.from_user.id, action, job)
            self.publish(job, "Решение руководителя")
        elif job["status"] == "approval":
            await cb.answer("Ожидается решение руководителя.", show_alert=True)
            return
        elif action == "reassign":
            await bot.send_message(chat, "Выберите менеджера:", reply_markup=keyboard([
                [(name, f"assign:{job['id']}:{job['version']}:{username}")] for username, name in MANAGERS.items()
            ]))
        elif action == "assign" and args[3] in MANAGERS:
            job["manager"] = args[3]
            job["version"] += 1
            self.audit(cb.from_user.id, "reassign", job)
            self.publish(job, f"Ответственный изменён. @{self.leader}, изменение сохранено для этой заявки.")
        elif action == "state" and args[3] in {*CHOICES, "keep"}:
            state = args[3]
            need_parts = state in ("order", "partial", "payment", "paid")
            self.data["sessions"][str(cb.from_user.id)] = {"job": job["id"], "version": job["version"],
                "action": state, "step": "parts" if need_parts else "comment"}
            self.save()
            await bot.send_message(chat, "Напишите полный список запчастей одним сообщением." if need_parts else
                                   "Напишите комментарий / причину. Если пояснения не нужны — «нет». /start — отмена.")
        await cb.answer()

    async def tick(self, bot, current=None):
        async with self.lock:
            current = current or now()
            if self.data["target"] and working(current):
                slot = "16" if current.hour >= 16 else "10" if current.hour >= 10 else None
                for job in self.data["jobs"].values():
                    if job["status"] == "closed" or datetime.fromisoformat(job["due"]) > current:
                        continue
                    if job["status"] != "new" and not slot:
                        continue
                    if job["status"] != "new" and current.replace(hour=int(slot), minute=0, second=0, microsecond=0) < datetime.fromisoformat(job["due"]):
                        continue
                    # Initial two-hour response deadline is escalated promptly; scheduled follow-ups twice daily.
                    initial = f"late:{job['id']}:{job['version']}:first"
                    key = initial if initial not in self.data["outbox"] else (f"late:{job['id']}:{job['version']}:{current.date()}:{slot}" if slot else None)
                    if key:
                        manager = "" if job["status"] == "approval" else ("@" + job["manager"] if job["manager"] else "")
                        self.enqueue(key, f"Просрочено. @{self.leader} {manager}\n\n" + self.card(job), job["id"])
                        if key == initial and slot:
                            self.data["outbox"][f"late:{job['id']}:{job['version']}:{current.date()}:{slot}"] = {"sent": True}
            self.save()
            for key, item in self.data["outbox"].items():
                chat = item.get("chat") or self.data["target"]
                if item["sent"] or not chat:
                    continue
                if key.startswith("late:"):
                    _, jid, version, *_ = key.split(":")
                    job = self.data["jobs"][jid]
                    if job["status"] == "closed" or job["version"] != int(version):
                        item["sent"] = True
                        self.save()
                        continue
                try:
                    await bot.send_message(chat, item["text"], reply_markup=self.link(item["job"]) if item.get("job") else None)
                except TelegramAPIError:
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
