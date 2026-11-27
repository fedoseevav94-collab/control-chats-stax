import asyncio
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramAPIError
from aiogram.types import User

from bot.parts import Parts, TZ, accident_date, deadline, normalize, parse_date, plates, repair_notice


def make(tmp_path):
    p = Parts(tmp_path / "parts.db", [{"plate": "В326СВ761", "model": "Hyundai Sonata", "manager": "lalalas19"}])
    p.data.update(target=-10, source=-10020, leader_id=1)
    p.data["users"] = {"lalalas19": 2, "pagorodu": 3}
    p.username = "testbot"
    return p


def make_bot():
    return NS(send_message=AsyncMock(return_value=NS(message_id=777)), edit_message_text=AsyncMock())


def group_calls(bot, chat=-10):
    return [c for c in bot.send_message.call_args_list if c.args[0] == chat]


def user(uid, name=None):
    return User(id=uid, is_bot=False, first_name="Test", username=name)


def message(mid, text, album=None, photo=False):
    return NS(chat=NS(id=-10020, type="supergroup"), message_id=mid, text=None if photo else text,
              caption=text if photo else None, document=None, photo=[1] if photo else None,
              reply_to_message=None, media_group_id=album)


def callback(uid, data):
    return NS(data="parts:" + data, from_user=user(uid), message=NS(chat=NS(id=uid, type="private")), answer=AsyncMock())


def test_weekend_and_working_minutes():
    start = datetime(2026, 11, 27, 17, 0, tzinfo=TZ)
    assert deadline(start) == datetime(2026, 11, 30, 10, 0, tzinfo=TZ)
    assert deadline(datetime(2026, 11, 28, 12, tzinfo=TZ)) == datetime(2026, 11, 30, 11, tzinfo=TZ)
    try:
        parse_date("28.11.2026 10:00", start)
        assert False
    except ValueError:
        pass


def test_recognition():
    assert plates("B326CB761.zip и В776НВ172", {}) == ["В326СВ761", "В776НВ172"]
    assert plates("ЕС53674", {"ЕС53674": {}}) == ["ЕС53674"]
    assert repair_notice("Осмотрены, можно в ремонт!")
    assert not repair_notice("Не осмотрены, нельзя в ремонт")


def test_ingestion_deduplication_and_ambiguity(tmp_path):
    async def run():
        p = make(tmp_path)
        await p.ingest(message(1, "В326СВ761 ДТП от 15.11.2026"))
        await p.ingest(message(1, "В326СВ761 ДТП от 15.11.2026"))
        await p.ingest(message(2, "B326CB761.zip ДТП от 15.11.2026"))
        assert len(p.data["jobs"]) == 1
        assert len(p.data["jobs"]["1"]["sources"]) == 2
        await p.ingest(message(3, "В326СВ761 новые фотографии"))
        assert p.data["events"]["-10020:3"]["pending"] == ["В326СВ761"]
        await p.ingest(message(4, "В326СВ761 ДТП от 25.11.2026"))
        assert len(p.data["jobs"]) == 2
        await p.ingest(message(5, "В326СВ761 осмотрена, можно в ремонт"))
        assert len(p.data["jobs"]) == 2
        assert any(k.startswith("repair:") for k in p.data["outbox"])
        p.db.close()
    asyncio.run(run())


def test_acl_closure_and_stale_buttons(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        j = p.new_job("В326СВ761", "source")
        await p.callback(callback(99, "state:1:0:paid"), bot)  # посторонний — нет доступа
        assert not p.data["sessions"]
        await p.callback(callback(2, "state:1:0:paid"), bot)
        session = p.data["sessions"]["2"]
        session.update(parts="Бампер", comment="Оплата подтверждена")
        p.finish(2, session, j, None)
        assert j["status"] == "closed" and j["request"] == "paid"  # закрывается сразу, без подтверждения
        assert any(k.startswith("dm:closed:1") for k in p.data["outbox"])  # руководителю — уведомление
        await p.callback(callback(2, "reopen:1:1"), bot)  # менеджер вернуть не может
        assert j["status"] == "closed"
        await p.callback(callback(1, "reopen:1:1"), bot)  # руководитель возвращает в работу
        assert j["status"] == "new" and j["version"] == 2
        count = len(p.data["history"])
        await p.callback(callback(1, "reopen:1:1"), bot)  # устаревшая кнопка ничего не меняет
        assert len(p.data["history"]) == count
        p.db.close()
    asyncio.run(run())


def test_partial_persistence_and_reminders(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        j = p.new_job("В326СВ761", "source")
        due = datetime(2026, 11, 25, 10, tzinfo=TZ)
        p.finish(2, {"action": "partial", "parts": "Фара, бампер", "comment": "Фара оплачена; бампер не заказан"}, j, due)
        assert j["status"] == "partial"
        await p.tick(bot, datetime(2026, 11, 25, 11, tzinfo=TZ))
        sent = len(group_calls(bot))
        await p.tick(bot, datetime(2026, 11, 25, 11, 1, tzinfo=TZ))
        assert len(group_calls(bot)) == sent
        assert any("@fedos_av" in str(c) for c in bot.send_message.call_args_list)
        assert all("@dislavsergeevich" not in str(c) for c in bot.send_message.call_args_list)
        p.db.close()
        p = make(tmp_path)
        assert p.data["jobs"]["1"]["status"] == "partial"
        await p.tick(bot, datetime(2026, 11, 25, 11, 2, tzinfo=TZ))
        assert len(group_calls(bot)) == sent
        await p.tick(bot, datetime(2026, 11, 25, 16, tzinfo=TZ))
        assert len(group_calls(bot)) == sent + 1
        p.db.close()
    asyncio.run(run())


def test_identity_does_not_follow_reused_username(tmp_path):
    p = make(tmp_path)
    assert p.register(user(55, "lalalas19")) is None
    assert p.role(user(2, "new_username")) == "lalalas19"
    assert p.register(user(56, "fedos_av")) is None
    p.db.close()


def test_due_between_slots_waits_until_16(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        j = p.new_job("В326СВ761", "source")
        j.update(status="order", due=datetime(2026, 11, 25, 12, tzinfo=TZ).isoformat())
        await p.tick(bot, datetime(2026, 11, 25, 13, tzinfo=TZ))
        assert not any(k.startswith("late:") for k in p.data["outbox"])
        await p.tick(bot, datetime(2026, 11, 25, 16, tzinfo=TZ))
        assert any(k.startswith("late:") for k in p.data["outbox"])
        p.db.close()
    asyncio.run(run())


def test_initial_two_hour_deadline_escalates_without_waiting_for_slot(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        j = p.new_job("В326СВ761", "source")
        j["due"] = datetime(2026, 11, 25, 12, tzinfo=TZ).isoformat()
        await p.tick(bot, datetime(2026, 11, 25, 12, tzinfo=TZ))
        assert "late:1:0:first" in p.data["outbox"]
        assert "dm:late:1:0:first" in p.data["outbox"]  # дубль просрочки менеджеру в личку
        sent = bot.send_message.call_count
        await p.tick(bot, datetime(2026, 11, 28, 16, tzinfo=TZ))
        assert sent == bot.send_message.call_count
        p.db.close()
    asyncio.run(run())


def test_private_text_workflow(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        j = p.new_job("В326СВ761", "source")
        await p.callback(callback(2, "state:1:0:partial"), bot)
        msg = NS(chat=NS(id=2, type="private"), from_user=user(2), text="Фара, бампер", answer=AsyncMock())
        await p.message(msg, bot)
        msg.text = "нет"
        await p.message(msg, bot)
        assert p.data["sessions"]["2"]["step"] == "comment"
        msg.text = "Фара заказана, бампер ещё нет"
        await p.message(msg, bot)
        msg.text = "31.12.2030 16:00"
        await p.message(msg, bot)
        assert j["status"] == "partial"
        assert not p.data["sessions"]
        p.db.close()
    asyncio.run(run())


def test_other_manager_takes_over_on_action(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        j = p.new_job("В326СВ761", "source")
        assert j["manager"] == "lalalas19"
        await p.callback(callback(3, "open:1"), bot)  # открыть чужую можно, закрепление не меняется
        assert j["manager"] == "lalalas19"
        await p.callback(callback(3, "state:1:0:order"), bot)
        assert j["manager"] == "pagorodu"
        assert p.data["sessions"]["3"]["version"] == j["version"]
        await p.callback(callback(1, "state:%d:%d:order" % (1, j["version"])), bot)  # руководитель не перехватывает
        assert j["manager"] == "pagorodu"
        p.db.close()
    asyncio.run(run())


def test_back_cancel_and_quick_buttons(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        j = p.new_job("В326СВ761", "source")
        await p.callback(callback(2, "state:1:0:order"), bot)
        await p.callback(callback(2, "flow:back"), bot)
        assert not p.data["sessions"] and j["status"] == "new"
        await p.callback(callback(2, "state:1:0:order"), bot)
        msg = NS(chat=NS(id=2, type="private"), from_user=user(2), text="Фара", answer=AsyncMock())
        await p.message(msg, bot)
        await p.callback(callback(2, "flow:back"), bot)
        assert p.data["sessions"]["2"]["step"] == "parts"
        await p.message(msg, bot)
        await p.callback(callback(2, "flow:skip"), bot)
        assert p.data["sessions"]["2"]["step"] == "date"
        await p.callback(callback(2, "flow:cancel"), bot)
        assert not p.data["sessions"] and j["status"] == "new"
        await p.callback(callback(2, "state:1:0:order"), bot)
        await p.message(msg, bot)
        await p.callback(callback(2, "flow:skip"), bot)
        await p.callback(callback(2, "flow:due:203012311600"), bot)
        assert j["status"] == "order" and j["parts"] == "Фара" and not p.data["sessions"]
        await p.callback(callback(2, "state:%d:%d:partial" % (1, j["version"])), bot)
        await p.callback(callback(2, "flow:keepparts"), bot)
        await p.callback(callback(2, "flow:skip"), bot)  # для «частично» комментарий обязателен
        assert p.data["sessions"]["2"]["step"] == "comment"
        p.db.close()
    asyncio.run(run())


def test_accident_date_formats():
    from datetime import date
    today = date(2026, 11, 25)
    assert accident_date("ДТП от 24.11.2026", today) == "2026-11-24"
    assert accident_date("дтп 24.11", today) == "2026-11-24"
    assert accident_date("ДТП 24.11.26", today) == "2026-11-24"
    assert accident_date("B156OX172_ДТП_24-11-2026.zip", today) == "2026-11-24"
    assert accident_date("B156OX172 24.11.2026.zip", today) == "2026-11-24"
    assert accident_date("ДТП 31.12", today) == "2025-12-31"
    assert accident_date("ДТП 28.11.2026", today) is None  # будущая дата
    assert accident_date("тел 89116524252", today) is None
    assert accident_date("ДТП от 30.02.2026", today) is None


def test_album_uses_caption_plate_once(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        await p.ingest(message(10, None, album="A1", photo=True))  # фото до подписи
        await p.ingest(message(11, "В326СВ761 ДТП от 20.11.2026", album="A1", photo=True))
        for mid in range(12, 18):
            await p.ingest(message(mid, None, album="A1", photo=True))
        assert len(p.data["jobs"]) == 1
        assert len(p.data["jobs"]["1"]["sources"]) == 8
        assert not any(ev["pending"] for ev in p.data["events"].values())
        assert not any(k.startswith("unclear:") for k in p.data["outbox"])
        # альбом совсем без номера — одно уточнение на весь альбом
        for mid in range(20, 25):
            await p.ingest(message(mid, None, album="A2", photo=True))
        await p.tick(bot, datetime(2026, 11, 25, 12, tzinfo=TZ))
        assert not any(k.startswith("unclear:") for k in p.data["outbox"])  # ещё ждём подпись
        p.data["albums"]["A2"]["at"] = datetime(2026, 11, 25, 11, tzinfo=TZ).isoformat()
        await p.tick(bot, datetime(2026, 11, 25, 12, tzinfo=TZ))
        assert sum(1 for k in p.data["outbox"] if k.startswith("unclear:")) == 1
        p.db.close()
    asyncio.run(run())


def test_single_card_is_edited(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        j = p.new_job("В326СВ761", "source")
        await p.tick(bot, datetime(2026, 11, 25, 9, 0, tzinfo=TZ))
        assert len(group_calls(bot)) == 1 and j["card"] == 777
        p.finish(2, {"action": "order", "parts": "Фара", "comment": ""}, j, datetime(2030, 12, 31, 16, tzinfo=TZ))
        await p.ingest(message(30, "В326СВ761 ДТП от 20.11.2026"))  # без даты у заявки -> уточнение, не новое сообщение
        await p.tick(bot, datetime(2026, 11, 25, 9, 1, tzinfo=TZ))
        assert len(group_calls(bot)) == 1  # обычное обновление — правка той же карточки
        assert bot.edit_message_text.call_count == 1
        assert bot.edit_message_text.call_args.kwargs["message_id"] == 777
        await p.callback(callback(3, "state:1:%d:payment" % j["version"]), bot)  # перехват — сигнал в группу
        await p.tick(bot, datetime(2026, 11, 25, 9, 2, tzinfo=TZ))
        assert len(group_calls(bot)) == 2
        assert any("ранее @lalalas19" in c.args[1] for c in group_calls(bot))
        assert any(c.args[0] == 2 and "взял" in c.args[1] for c in bot.send_message.call_args_list)  # прежнему — в личку
        p.db.close()
    asyncio.run(run())


def test_private_notifications_and_digest(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        p.new_job("В326СВ761", "source")
        await p.tick(bot, datetime(2026, 11, 25, 9, 0, tzinfo=TZ))
        assert any(c.args[0] == 2 and "Новая заявка" in c.args[1] for c in bot.send_message.call_args_list)
        await p.tick(bot, datetime(2026, 11, 26, 9, 40, tzinfo=TZ))  # утро: сводка менеджеру и руководителю
        digests = [c for c in bot.send_message.call_args_list if "Доброе утро" in c.args[1]]
        assert sorted(c.args[0] for c in digests) == [1, 2]
        await p.tick(bot, datetime(2026, 11, 26, 9, 45, tzinfo=TZ))
        assert len([c for c in bot.send_message.call_args_list if "Доброе утро" in c.args[1]]) == 2
        bot.send_message.side_effect = TelegramAPIError(method=None, message="Forbidden: bot was blocked by the user")
        p.dm("x", "pagorodu", "test")
        await p.tick(bot, datetime(2026, 11, 26, 9, 50, tzinfo=TZ))
        assert p.data["outbox"]["dm:x"]["sent"]  # личка недоступна — не повторяем бесконечно
        p.db.close()
    asyncio.run(run())


def test_group_stays_silent_on_service_commands(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        p.data["source"] = p.data["target"] = None
        sent_in_group = AsyncMock()

        def group_msg(text, uid=1, name="fedos_av", chat=-10020):
            return NS(chat=NS(id=chat, type="supergroup", title="ДТП"), message_id=1, text=text,
                      from_user=user(uid, name), answer=sent_in_group, delete=AsyncMock())

        msg = group_msg("/parts_bind")
        await p.message(msg, bot)
        msg.delete.assert_awaited()
        assert p.data["source"] is None
        await p.message(group_msg("/parts_bind source"), bot)
        assert p.data["source"] == -10020
        await p.message(group_msg("/chatid"), bot)  # в рабочей группе — тишина
        await p.message(group_msg("/parts_bind target", uid=3, name="pagorodu", chat=-10), bot)
        assert p.data["target"] is None  # не руководитель — игнор
        sent_in_group.assert_not_awaited()
        assert all(c.args[0] == 1 for c in bot.send_message.call_args_list)  # все ответы — руководителю в личку
        assert any("закреплена" in c.args[1] for c in bot.send_message.call_args_list)
        p.db.close()
    asyncio.run(run())


def test_plate_formats():
    fleet = {normalize(p): {} for p in ["В326СВ761", "В156ОХ172", "ЕС53674", "В776НВ172"]}
    for text in ["В326СВ/761", "В326СВ|761", "В326СВ.761", "в326св761", "B326CB761", "В 326 СВ 761 RUS",
                 "в326св", "Соната В326СВ без региона", "#В326СВ761"]:
        assert plates(text, fleet) == ["В326СВ761"], text
    assert plates("B1560X172", fleet) == ["В156ОХ172"]  # ноль вместо буквы О — только если номер есть в справочнике
    assert plates("ЕС 53674", fleet) == ["ЕС53674"]
    assert plates("тел 8 911 652 42 52, ОСАГО ХХХ 1127112791", fleet) == []
    assert plates("А999АА", fleet) == []  # без региона и нет в справочнике — не угадываем


def test_date_and_repair_formats():
    from datetime import date
    today = date(2026, 11, 25)
    for text, expected in [("ДТП 25 ноября", "2026-11-25"), ("ДТП 24 нояб.", "2026-11-24"), ("ДТП 24,11", "2026-11-24"),
                           ("ДТП 24 11 2026", "2026-11-24"), ("24.11 было ДТП", "2026-11-24"), ("ДТП вчера", "2026-11-24"),
                           ("авария 24.11", "2026-11-24"), ("ДТП в 10:30, 23.11", "2026-11-23")]:
        assert accident_date(text, today) == expected, text
    for text in ["ДТП 10 30", "Долг 12 263 ₽ до 30.12", "оплатить до 01.12", "тел 8 911 652 42 52"]:
        assert accident_date(text, today) is None, text
    for text in ["Можно ставить в ремонт", "можно ремонтировать", "осмотр пройден", "разрешили ремонт", "в ремонт можно"]:
        assert repair_notice(text), text
    for text in ["можно в ремонт?", "нельзя в ремонт", "пока не осмотрели, ремонт не начинать"]:
        assert not repair_notice(text), text


def test_voice_anonymous_admin_and_edited_message(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        voice = NS(chat=NS(id=-10020, type="supergroup"), message_id=40, text=None, caption=None, document=None,
                   photo=None, voice=object(), reply_to_message=None, media_group_id=None)
        await p.ingest(voice)
        assert p.data["events"]["-10020:40"]["pending"] == ["?"]  # голосовое без номера — на уточнение, не теряется
        anon = NS(chat=NS(id=-10020, type="supergroup"), message_id=41, text="ДТП 24.11 В326СВ761", caption=None,
                  document=None, photo=None, reply_to_message=None, media_group_id=None,
                  from_user=User(id=1087968824, is_bot=True, first_name="Group", username="GroupAnonymousBot"),
                  sender_chat=NS(id=-10020))
        await p.message(anon, bot)
        assert len(p.data["jobs"]) == 1  # сообщение анонимного админа обработано
        first = NS(chat=NS(id=-10020, type="supergroup"), message_id=42, text="ДТП, фото позже", caption=None,
                   document=None, photo=None, reply_to_message=None, media_group_id=None)
        await p.ingest(first)
        assert p.data["events"]["-10020:42"]["pending"] == ["?"]
        first.text = "ДТП 23.11 В776НВ172"  # автор исправил сообщение
        await p.edited(first, bot)
        assert len(p.data["jobs"]) == 2 and not p.data["events"]["-10020:42"]["pending"]
        await p.edited(first, bot)  # повторная правка — без дублей
        assert len(p.data["jobs"]) == 2
        p.db.close()
    asyncio.run(run())


def test_legacy_approval_closed_and_not_in_manager_digest(tmp_path):
    async def run():
        p, bot = make(tmp_path), make_bot()
        j = p.new_job("В847ХУ172", "source", )
        j.update(manager="lalalas19", status="approval", request="paid")
        p.save()
        p.db.close()
        p = make(tmp_path)  # перезапуск бота
        assert p.data["jobs"]["1"]["status"] == "closed"
        await p.tick(bot, datetime(2026, 11, 27, 9, 40, tzinfo=TZ))
        assert not any("Доброе утро" in c.args[1] for c in bot.send_message.call_args_list)  # нечего делать — нет сводки
        p.db.close()
    asyncio.run(run())
