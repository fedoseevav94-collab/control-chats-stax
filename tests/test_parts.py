import asyncio
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from aiogram.types import User

from bot.parts import Parts, TZ, deadline, parse_date, plates, repair_notice


def make(tmp_path):
    p = Parts(tmp_path / "parts.db", [{"plate": "В326СВ761", "model": "Hyundai Sonata", "manager": "lalalas19"}])
    p.data.update(target=-10, source=-10020, leader_id=1)
    p.data["users"] = {"lalalas19": 2, "pagorodu": 3}
    p.username = "testbot"
    return p


def user(uid, name=None):
    return User(id=uid, is_bot=False, first_name="Test", username=name)


def message(mid, text):
    return NS(chat=NS(id=-10020, type="supergroup"), message_id=mid, text=text, caption=None,
              document=None, photo=None, reply_to_message=None)


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
        p, bot = make(tmp_path), NS(send_message=AsyncMock())
        j = p.new_job("В326СВ761", "source")
        await p.callback(callback(3, "state:1:0:paid"), bot)
        assert not p.data["sessions"]
        await p.callback(callback(2, "state:1:0:paid"), bot)
        session = p.data["sessions"]["2"]
        session.update(parts="Бампер", comment="Оплата подтверждена")
        p.finish(2, session, j, None)
        assert j["status"] == "approval"
        await p.callback(callback(2, "approve:1:1"), bot)
        assert j["status"] == "approval"
        await p.callback(callback(1, "approve:1:1"), bot)
        assert j["status"] == "closed"
        count = len(p.data["history"])
        await p.callback(callback(1, "approve:1:1"), bot)
        assert len(p.data["history"]) == count
        p.db.close()
    asyncio.run(run())


def test_partial_persistence_and_reminders(tmp_path):
    async def run():
        p, bot = make(tmp_path), NS(send_message=AsyncMock())
        j = p.new_job("В326СВ761", "source")
        due = datetime(2026, 11, 25, 10, tzinfo=TZ)
        p.finish(2, {"action": "partial", "parts": "Фара, бампер", "comment": "Фара оплачена; бампер не заказан"}, j, due)
        assert j["status"] == "partial"
        await p.tick(bot, datetime(2026, 11, 25, 11, tzinfo=TZ))
        sent = bot.send_message.call_count
        await p.tick(bot, datetime(2026, 11, 25, 11, 1, tzinfo=TZ))
        assert bot.send_message.call_count == sent
        assert any("@fedos_av" in str(c) for c in bot.send_message.call_args_list)
        assert all("@dislavsergeevich" not in str(c) for c in bot.send_message.call_args_list)
        p.db.close()
        p = make(tmp_path)
        assert p.data["jobs"]["1"]["status"] == "partial"
        await p.tick(bot, datetime(2026, 11, 25, 11, 2, tzinfo=TZ))
        assert bot.send_message.call_count == sent
        await p.tick(bot, datetime(2026, 11, 25, 16, tzinfo=TZ))
        assert bot.send_message.call_count == sent + 1
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
        p, bot = make(tmp_path), NS(send_message=AsyncMock())
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
        p, bot = make(tmp_path), NS(send_message=AsyncMock())
        j = p.new_job("В326СВ761", "source")
        j["due"] = datetime(2026, 11, 25, 12, tzinfo=TZ).isoformat()
        await p.tick(bot, datetime(2026, 11, 25, 12, tzinfo=TZ))
        assert "late:1:0:first" in p.data["outbox"]
        sent = bot.send_message.call_count
        await p.tick(bot, datetime(2026, 11, 28, 16, tzinfo=TZ))
        assert sent == bot.send_message.call_count
        p.db.close()
    asyncio.run(run())


def test_private_text_workflow(tmp_path):
    async def run():
        p, bot = make(tmp_path), NS(send_message=AsyncMock())
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
