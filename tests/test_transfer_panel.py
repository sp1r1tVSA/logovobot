"""Панель `/to`: ссылки на темы, бюджеты по дивизионам, ввод значений после кнопки."""

import asyncio
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

import config
import database
from transfers import handlers, repo, service

MANAGER = 777


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with database.transaction() as conn:
        for table in ("transfer_slot_purchases", "transfer_club_budgets", "transfer_core_snapshot", "transfer_topics"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE transfers SET urn_item_id = NULL")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM transfer_windows")
    monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", MANAGER, raising=False)
    journal = []

    async def _record(*args, **kwargs):
        journal.append((args, kwargs))

    monkeypatch.setattr(handlers.admin_journal, "record", _record)
    handlers._pending.clear()
    yield journal
    handlers._pending.clear()


# ─── Ссылки на темы ──────────────────────────────────────────────────────────

class TestTopicLink:
    @pytest.mark.parametrize("text, expected", [
        ("https://t.me/c/1234567890/15", (-1001234567890, 15)),
        ("t.me/c/1234567890/15/321", (-1001234567890, 15)),
        ("https://t.me/c/1234567890/321?thread=15", (-1001234567890, 15)),
        ("  https://telegram.me/logovo_fifa/15/  ", ("@logovo_fifa", 15)),
        ("https://t.me/logovo_fifa/321?topic=15", ("@logovo_fifa", 15)),
    ])
    def test_parse(self, text, expected):
        assert service.parse_topic_link(text) == expected

    def test_general_topic_rejected(self):
        with pytest.raises(service.InputError, match="Общая"):
            service.parse_topic_link("https://t.me/c/1234567890/1")

    @pytest.mark.parametrize("text", ["", None, "привет", "https://t.me/c/123", "https://example.com/c/1/15",
                                      "https://t.me/+AbCdEf/15"])
    def test_garbage(self, text):
        with pytest.raises(service.InputError, match="не ссылка"):
            service.parse_topic_link(text)


# ─── Бюджеты по дивизионам ───────────────────────────────────────────────────

class TestBudgetPages:
    def test_pages_cover_league_once(self):
        wid = service.create_window(10)
        service.set_budget(wid, "Арсенал", "50", 10)
        pages = service.budget_pages(wid)
        clubs = [row["club"] for _, rows in pages for row in rows]
        assert sorted(clubs) == service.league_clubs()
        assert len(clubs) == len(set(clubs))
        rows = {row["club"]: row for _, rows in pages for row in rows}
        assert rows["Арсенал"]["budget_k"] == 50000 and rows["Арсенал"]["source"] == "manual"
        assert rows["Лидс"]["budget_k"] is None
        assert all(name != service.OUTSIDE_LEAGUE for name, _ in pages)

    def test_budget_outside_league_gets_own_page(self):
        wid = service.create_window(10)
        repo.set_club_budget(wid, "Клуб Из Прошлого", 7000, 10, source="manual")
        name, rows = service.budget_pages(wid)[-1]
        assert name == service.OUTSIDE_LEAGUE
        assert [r["club"] for r in rows] == ["Клуб Из Прошлого"]

    def test_view_buttons_fit_callback_limit(self):
        wid = service.create_window(10)
        for page in range(len(service.budget_pages(wid))):
            _, kb = handlers._budgets_view(wid, page)
            for row in kb.inline_keyboard:
                for button in row:
                    assert len(button.callback_data.encode()) <= 64


# ─── Поддельный Telegram ─────────────────────────────────────────────────────

class FakeMessage:
    def __init__(self, user_id, text=None, chat_type="private"):
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type=chat_type, id=user_id)
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.edits = []
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        if text:
            self.alerts.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


class FakeBot:
    username = "logovo_bot"

    def __init__(self, forum=True, send_error=None):
        self.forum = forum
        self.send_error = send_error
        self.sent = []

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=-1009 if isinstance(chat_id, str) else chat_id, is_forum=self.forum)

    async def send_message(self, chat_id, text, **kwargs):
        if self.send_error:
            raise self.send_error
        self.sent.append((chat_id, text, kwargs))


def _update(user_id=MANAGER, text=None, data=None, chat_type="private"):
    msg = FakeMessage(user_id, text, chat_type)
    query = FakeQuery(data, msg) if data else None
    return SimpleNamespace(effective_user=msg.from_user, effective_chat=msg.chat,
                           effective_message=msg, callback_query=query, message=msg)


def _run(handler, update, bot=None):
    asyncio.run(handler(update, SimpleNamespace(bot=bot or FakeBot())))
    return update


def _press(handler, data, user_id=MANAGER, bot=None):
    return _run(handler, _update(user_id, data=data), bot)


def _send(text, user_id=MANAGER, bot=None):
    return _run(handlers.on_input, _update(user_id, text=text), bot)


def _last(update):
    if update.callback_query and update.callback_query.edits:
        return update.callback_query.edits[-1][0]
    return update.effective_message.replies[-1]


# ─── Панель ──────────────────────────────────────────────────────────────────

class TestPanel:
    def test_only_to_command_registered(self):
        from telegram.ext import CallbackQueryHandler, CommandHandler

        added = []
        handlers.register_handlers(SimpleNamespace(add_handler=lambda h, *a, **k: added.append(h)))
        commands = {c for h in added if isinstance(h, CommandHandler) for c in h.commands}
        assert commands == {"to"}
        patterns = [h.pattern.pattern for h in added if isinstance(h, CallbackQueryHandler)]
        assert all(p.startswith("^tw:") for p in patterns)

    def test_env_admin_uses_panel(self, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_IDS", [990001])
        service.create_window(10)
        _press(handlers.cb_auto_set, "tw:auto_set", user_id=990001)
        upd = _send("10.10.2099 20:00", user_id=990001)
        assert "✅" in _last(upd) and repo.get_active_window()["auto_close_at"] == "2099-10-10 20:00:00"

    def test_strangers_get_nothing(self):
        upd = _press(handlers.cb_topic, "tw:topic:feed", user_id=1)
        assert upd.callback_query.alerts and not handlers._pending
        assert not handlers.AWAITING_INPUT.filter(FakeMessage(1, "x"))

    def test_hub_without_window(self):
        upd = _run(handlers.cmd_hub, _update(text="/to"))
        assert "Незакрытого окна нет" in _last(upd)

    def test_settings_are_read_only(self):
        service.create_window(10)
        upd = _press(handlers.cb_settings, "tw:settings")
        text, kb = upd.callback_query.edits[-1]
        assert "задаются правилами ТО" in text and "/to_set" not in text and "<code>" not in text
        assert [b.callback_data for row in kb.inline_keyboard for b in row] == ["tw:hub"]

    def test_budget_input_flow(self, _clean):
        wid = service.create_window(10)
        club = service.budget_pages(wid)[0][1][1]["club"]
        _press(handlers.cb_budget_club, "tw:bclub:0:1")
        assert handlers.AWAITING_INPUT.filter(FakeMessage(MANAGER, "1"))
        assert not handlers.AWAITING_INPUT.filter(FakeMessage(MANAGER, "1", chat_type="supergroup"))

        upd = _send("много")
        assert "⚠️" in _last(upd) and MANAGER in handlers._pending      # ждём дальше

        upd = _send("12,5")
        assert repo.get_club_budget(wid, club) == 12500
        assert "12.5 млн" in _last(upd) and MANAGER not in handlers._pending
        assert _clean[-1][0][1] == "transfer_budget_set"

    def test_other_button_cancels_input(self):
        service.create_window(10)
        _press(handlers.cb_budget_club, "tw:bclub:0:0")
        _press(handlers.cmd_hub, "tw:hub")
        assert not handlers._pending

    def test_input_dropped_when_window_changed(self):
        wid = service.create_window(10)
        _press(handlers.cb_auto_set, "tw:auto_set")
        service.close_window(wid, 10)
        upd = _send("10.10 20:00")
        assert "сменилось" in _last(upd) and not handlers._pending

    def test_autoclose_set_and_off(self, _clean):
        wid = service.create_window(10)
        _press(handlers.cb_auto_set, "tw:auto_set")
        _send("10.10.2099 20:00")
        assert repo.get_window(wid)["auto_close_at"] == "2099-10-10 20:00:00"
        _press(handlers.cb_auto_off, "tw:auto_off")
        assert repo.get_window(wid)["auto_close_at"] is None
        assert [j[0][1] for j in _clean] == ["transfer_window_settings"] * 2

    def test_topic_bound_by_link(self, _clean):
        bot = FakeBot()
        _press(handlers.cb_topic, "tw:topic:requests")
        _send("https://t.me/c/1234567890/15", bot=bot)
        topic = repo.get_topic("requests")
        assert (topic["group_chat_id"], topic["message_thread_id"]) == (-1001234567890, 15)
        assert bot.sent[0][0] == -1001234567890 and bot.sent[0][2]["message_thread_id"] == 15
        assert _clean[-1][0][1] == "transfer_topic_bound" and not handlers._pending

    @pytest.mark.parametrize("bot, text, error", [
        (FakeBot(), "не ссылка", "не ссылка"),
        (FakeBot(forum=False), "https://t.me/c/1234567890/15", "нет тем"),
        (FakeBot(send_error=BadRequest("Message thread not found")), "https://t.me/c/1234567890/15",
         "Не получилось написать"),
    ])
    def test_topic_errors_keep_waiting(self, bot, text, error):
        _press(handlers.cb_topic, "tw:topic:feed")
        upd = _send(text, bot=bot)
        assert error in _last(upd)
        assert repo.get_topic("feed") is None and MANAGER in handlers._pending

    def test_pending_expires(self, monkeypatch):
        handlers._set_pending(MANAGER, "topic", topic_type="feed")
        monkeypatch.setattr(handlers, "INPUT_TTL_SECONDS", -1)
        handlers._set_pending(MANAGER, "topic", topic_type="feed")
        assert handlers._get_pending(MANAGER) is None


# ─── Возврат слотов ──────────────────────────────────────────────────────────

class TestSlotRefundPanel:
    def _open(self):
        with database.transaction() as conn:
            for table in ("users", "coin_transactions", "user_wallets"):
                conn.execute(f"DELETE FROM {table}")
        wid = service.create_window(10)
        repo.update_window_settings(wid, {"slot_price_coins": 300, "max_extra_slots": 2})
        repo.open_window(wid, 1)
        return wid

    def _bought(self, wid=None):
        from transfers import slots

        wid = wid or self._open()
        with database.transaction() as conn:
            conn.execute("INSERT INTO users (telegram_id, username, team_name) VALUES (101, 'c', 'Челси')")
        database.get_or_create_wallet(101)
        with database.transaction() as conn:
            conn.execute("UPDATE user_wallets SET balance = 1000 WHERE user_id = 101")
        slots.buy(101, "buy")
        return wid, repo.list_slot_purchases(wid)[0]["id"]

    def test_hub_button_appears_only_with_purchases(self):
        wid = self._open()
        _, kb = handlers._hub_view()
        assert "tw:slots:0" not in [b.callback_data for r in kb.inline_keyboard for b in r]
        self._bought(wid)
        _, kb = handlers._hub_view()
        assert "tw:slots:0" in [b.callback_data for r in kb.inline_keyboard for b in r]

    def test_refund_flow(self, _clean):
        _, pid = self._bought()
        upd = _press(handlers.cb_slots, "tw:slots:0")
        text, kb = upd.callback_query.edits[-1]
        labels = [b.text for r in kb.inline_keyboard for b in r]
        assert any("Челси" in x for x in labels)
        assert f"tw:slr:{pid}" in [b.callback_data for r in kb.inline_keyboard for b in r]
        upd = _press(handlers.cb_slot_refund_ask, f"tw:slr:{pid}")
        assert "Вернуть слот" in _last(upd)
        bot = FakeBot()
        bot.sent.clear()
        upd = _press(handlers.cb_slot_refund_yes, f"tw:slc:{pid}", bot=bot)
        assert "Слот возвращён" in _last(upd) and database.get_wallet_balance(101) == 1000
        assert _clean[-1][0][1] == "transfer_slot_refunded"
        assert repo.get_slot_purchase(pid)["status"] == "refunded"

    def test_only_the_manager_refunds(self, monkeypatch):
        _, pid = self._bought()
        monkeypatch.setattr(config, "ADMIN_IDS", [990001])
        upd = _press(handlers.cb_slot_refund_yes, f"tw:slc:{pid}", user_id=990001)
        assert upd.callback_query.alerts and repo.get_slot_purchase(pid)["status"] == "active"

    def test_routes_registered(self):
        from telegram.ext import CallbackQueryHandler

        added = []
        handlers.register_handlers(SimpleNamespace(add_handler=lambda h, *a, **k: added.append(h)))
        cbs = [h for h in added if isinstance(h, CallbackQueryHandler)]
        expected = {"tw:slots:0": handlers.cb_slots, "tw:slr:5": handlers.cb_slot_refund_ask,
                    "tw:slc:5": handlers.cb_slot_refund_yes, "tw:sly:5": handlers.cb_sanction_lift_yes}
        for data, cb in expected.items():
            # PTB берёт первый подошедший обработчик — пересечение паттернов уводит кнопку не туда
            first = next(h for h in cbs if h.pattern.match(data))
            assert first.callback is cb, data
