"""Этап 4: решение ответственного по заявке — ✅/❌, уведомления, лента, журнал."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import database
from transfers import approval, handlers, notify, repo, requests as req_mod, service

MANAGER = 777


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with database.transaction() as conn:
        for table in (
            "transfer_squad_ops", "transfer_slot_purchases", "transfer_club_budgets",
            "transfer_core_snapshot", "transfer_players", "transfer_topics",
            "transfer_sanctions", "squad_players", "users",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE transfers SET urn_item_id = NULL")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM transfer_windows")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     "('transfer_windows', 'transfers', 'transfer_club_budgets')")
    monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", MANAGER, raising=False)
    monkeypatch.setattr(config, "ADMIN_IDS", [990001])
    journal = []

    async def _record(*args, **kwargs):
        journal.append((args, kwargs))

    monkeypatch.setattr(handlers.admin_journal, "record", _record)
    handlers._pending.clear()
    yield journal
    handlers._pending.clear()


def _user(user_id, username, team):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name) VALUES (?, ?, ?)",
                     (user_id, username, team))


def _setup():
    """Окно, два клуба с бюджетом и подтверждённая сделка Арсенал → Челси."""
    wid = repo.create_window(1, 10, title="ТО Зима")
    repo.open_window(wid, 1)
    _user(101, "chelsea", "Челси")
    _user(102, "arsenal", "Арсенал")
    repo.set_club_budget(wid, "Челси", 50000, 1)
    repo.set_club_budget(wid, "Арсенал", 50000, 1)
    deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="B. Saka", price="15", ovr=106)
    return wid, req_mod.confirm(102, deal["id"])


# ─── Логика решения ──────────────────────────────────────────────────────────

class TestApprove:
    def test_deal_approved_and_directory_updated(self):
        _, deal = _setup()
        decision = approval.approve(MANAGER, deal["id"])
        t = decision.transfer
        assert t["status"] == "approved" and t["decided_by"] == MANAGER
        player = repo.get_transfer(deal["id"])
        assert player["status"] == "approved"
        row = repo.get_player("B. Saka")
        assert row is not None and row["last_club"] == "Челси" and row["ovr"] == 106 and row["price_k"] == 15000

    def test_only_manager_decides(self):
        _, deal = _setup()
        for outsider in (990001, 101, 102, None):
            with pytest.raises(service.InputError, match="только ответственный"):
                approval.approve(outsider, deal["id"])
            with pytest.raises(service.InputError, match="только ответственный"):
                approval.reject(outsider, deal["id"])
        assert repo.get_transfer(deal["id"])["status"] == "pending_manager"

    def test_second_press_refused(self):
        _, deal = _setup()
        approval.approve(MANAGER, deal["id"])
        with pytest.raises(service.InputError, match="уже одобрена"):
            approval.approve(MANAGER, deal["id"])
        with pytest.raises(service.InputError, match="уже одобрена"):
            approval.reject(MANAGER, deal["id"])

    def test_not_confirmed_by_counterparty_yet(self):
        _setup()
        deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="Rice", price="5", ovr=100)
        with pytest.raises(service.InputError, match="ждёт вторую сторону"):
            approval.approve(MANAGER, deal["id"])

    def test_unknown_transfer(self):
        _setup()
        with pytest.raises(service.InputError, match="не найдена"):
            approval.approve(MANAGER, 9999)

    def test_hard_block_at_approval(self):
        wid, deal = _setup()
        # Потолок снизили уже после подачи — одобрить нельзя
        repo.update_window_settings(wid, {"ovr_cap": 100})
        with pytest.raises(service.InputError, match="Одобрить нельзя"):
            approval.approve(MANAGER, deal["id"])
        assert repo.get_transfer(deal["id"])["status"] == "pending_manager"

    def test_decision_after_window_closed(self):
        wid, deal = _setup()
        repo.close_window(wid, 1)
        # Закрытие автоотклоняет неподтверждённое, а ждущее решения остаётся
        assert repo.get_transfer(deal["id"])["status"] == "pending_manager"
        assert approval.approve(MANAGER, deal["id"]).transfer["status"] == "approved"

    def test_warnings_are_refreshed_not_blocking(self):
        wid, deal = _setup()
        repo.set_club_budget(wid, "Челси", 1000, 1)  # на 15 млн денег нет
        decision = approval.approve(MANAGER, deal["id"])
        assert any(w["code"] == "BUDGET_EXCEEDED" for w in decision.warnings)
        stored = json.loads(repo.get_transfer(deal["id"])["warnings"])
        assert any(w["code"] == "BUDGET_EXCEEDED" for w in stored)

    def test_surcharge_and_urn_flow(self):
        wid = repo.create_window(1, 10)
        repo.open_window(wid, 1)
        _user(101, "chelsea", "Челси")
        _user(102, "arsenal", "Арсенал")
        repo.set_club_budget(wid, "Арсенал", 50000, 1)
        repo.update_window_settings(wid, {"surcharge_table": {105: 35000}, "urn_max_per_club": 2})

        sale = req_mod.create_urn_sale(101, player="Card X", tm_price="10", special_price="2", sellable=True)
        # покупка из урны, пока продажа не одобрена, невозможна
        with pytest.raises(service.InputError):
            req_mod.create_urn_buy(102, urn_item_id=sale["id"])
        assert approval.approve(MANAGER, sale["id"]).transfer["status"] == "approved"

        buy = req_mod.create_urn_buy(102, urn_item_id=sale["id"])
        assert approval.approve(MANAGER, buy["id"]).transfer["status"] == "approved"
        assert repo.get_player("Card X")["last_club"] == "Арсенал"

        repo.set_club_budget(wid, "Челси", 50000, 1)
        sc = req_mod.create_surcharge(101, player="Palmer", ovr=105)
        assert approval.approve(MANAGER, sc["id"]).transfer["status"] == "approved"


class TestReject:
    def test_reject_with_reason(self):
        _, deal = _setup()
        t = approval.reject(MANAGER, deal["id"], "  не   по правилам \n лиги  ")
        assert t["status"] == "rejected" and t["decided_reason"] == "не по правилам лиги"

    def test_reason_optional_and_capped(self):
        _, deal = _setup()
        assert approval.reject(MANAGER, deal["id"], "   ")["decided_reason"] is None
        _, other = _setup_second()
        t = approval.reject(MANAGER, other["id"], "я" * 1000)
        assert len(t["decided_reason"]) == approval.REJECT_REASON_MAX

    def test_surcharge_rejected(self):
        wid = repo.create_window(1, 10)
        repo.open_window(wid, 1)
        _user(101, "chelsea", "Челси")
        repo.update_window_settings(wid, {"surcharge_table": {105: 35000}})
        sc = req_mod.create_surcharge(101, player="Palmer", ovr=105)
        assert approval.reject(MANAGER, sc["id"], "нет")["status"] == "rejected"


def _setup_second():
    deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="Rice", price="5", ovr=100)
    return None, req_mod.confirm(102, deal["id"])


# ─── Уведомления ─────────────────────────────────────────────────────────────

def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    bot.send_photo = AsyncMock(return_value=MagicMock(photo=[]))
    bot.edit_message_reply_markup = AsyncMock()
    return bot


def _texts(bot, chat_id=None):
    return [c.kwargs["text"] for c in bot.send_message.await_args_list
            if chat_id is None or c.kwargs.get("chat_id") == chat_id]


class TestNotify:
    def test_keyboard_and_limits(self):
        kb = notify.approval_keyboard(123456789)
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert data == ["tw:ap:123456789", "tw:rj:123456789"]
        assert all(len(d.encode()) <= 64 for d in data)

    def test_card_in_requests_topic_has_buttons(self):
        _, deal = _setup()
        repo.bind_topic("requests", -100500, 7, 1)
        bot = _bot()
        assert asyncio.run(notify.post_request_card(bot, deal)) is True
        kb = bot.send_message.await_args.kwargs["reply_markup"]
        assert [b.callback_data for row in kb.inline_keyboard for b in row] == [
            f"tw:ap:{deal['id']}", f"tw:rj:{deal['id']}"]

    def test_card_fallback_to_manager_dm_keeps_buttons(self):
        _, deal = _setup()
        bot = _bot()
        assert asyncio.run(notify.post_request_card(bot, deal)) is False
        call = bot.send_message.await_args.kwargs
        assert call["chat_id"] == MANAGER and call["reply_markup"] is not None

    def test_no_buttons_while_waiting_for_counterparty(self):
        _setup()
        deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="Rice", price="5", ovr=100)
        repo.bind_topic("requests", -100500, 7, 1)
        bot = _bot()
        asyncio.run(notify.post_request_card(bot, deal))
        assert bot.send_message.await_args.kwargs.get("reply_markup") is None

    def test_approved_dm_all_parties_and_feed(self):
        _, deal = _setup()
        t = approval.approve(MANAGER, deal["id"]).transfer
        repo.bind_topic("feed", -100500, 9, 1)
        bot = _bot()
        assert asyncio.run(notify.notify_approved(bot, t)) is True
        assert any("одобрена" in x for x in _texts(bot, 101))
        assert any("одобрена" in x for x in _texts(bot, 102))
        feed = [c.kwargs for c in bot.send_message.await_args_list if c.kwargs.get("chat_id") == -100500]
        assert feed and feed[0]["message_thread_id"] == 9 and "B. Saka" in feed[0]["text"]

    def test_rejected_reason_in_dm_and_feed(self):
        _, deal = _setup()
        t = approval.reject(MANAGER, deal["id"], "нет мест в ядре")
        repo.bind_topic("feed", -100500, 9, 1)
        bot = _bot()
        asyncio.run(notify.notify_rejected(bot, t))
        assert all("нет мест в ядре" in x for x in _texts(bot, 101) + _texts(bot, 102))
        assert any("Отклонён" in x and "нет мест в ядре" in x for x in _texts(bot, -100500))

    def test_rejected_without_reason_has_no_reason_line(self):
        _, deal = _setup()
        t = approval.reject(MANAGER, deal["id"])
        bot = _bot()
        asyncio.run(notify.notify_rejected(bot, t))
        assert all("Причина" not in x for x in _texts(bot, 101))

    def test_unreachable_party_reported_to_alerts(self):
        _, deal = _setup()
        t = approval.approve(MANAGER, deal["id"]).transfer
        repo.bind_topic("alerts", -100500, 11, 1)
        bot = _bot()

        async def _send(chat_id, text=None, **kwargs):
            if chat_id == 102:
                raise handlers.TelegramError("Forbidden: bot was blocked by the user")
            return MagicMock()

        bot.send_message = AsyncMock(side_effect=_send)
        asyncio.run(notify.notify_approved(bot, t))
        alerts = [c.kwargs["text"] for c in bot.send_message.await_args_list
                  if c.kwargs.get("chat_id") == -100500]
        assert any("102" in x and "не дошло" in x for x in alerts)


# ─── Кнопки ──────────────────────────────────────────────────────────────────

class FakeMessage:
    def __init__(self, user_id, text=None, chat_type="private", chat_id=None):
        self.text = text
        self.message_id = 55
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type=chat_type, id=chat_id if chat_id is not None else user_id)
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.edits = []
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


def _update(user_id=MANAGER, text=None, data=None, chat_type="private", chat_id=None):
    msg = FakeMessage(user_id, text, chat_type, chat_id)
    query = FakeQuery(data, msg) if data else None
    return SimpleNamespace(effective_user=msg.from_user, effective_chat=msg.chat,
                           effective_message=msg, callback_query=query, message=msg)


def _run(handler, update, bot):
    asyncio.run(handler(update, SimpleNamespace(bot=bot)))
    return update


def _group_press(handler, data, bot, user_id=MANAGER):
    return _run(handler, _update(user_id, data=data, chat_type="supergroup", chat_id=-100500), bot)


class TestButtons:
    def test_routes_registered(self):
        from telegram.ext import CallbackQueryHandler

        added = []
        handlers.register_handlers(SimpleNamespace(add_handler=lambda h, *a, **k: added.append(h)))
        patterns = [h.pattern for h in added if isinstance(h, CallbackQueryHandler)]
        for data in ("tw:ap:12", "tw:rj:12", "tw:rjn:12", "tw:rjc"):
            assert any(p.match(data) for p in patterns), data

    def test_approve_button_in_group_topic(self, _clean):
        _, deal = _setup()
        repo.bind_topic("feed", -100999, 9, 1)
        bot = _bot()
        upd = _group_press(handlers.cb_approve, f"tw:ap:{deal['id']}", bot)
        assert repo.get_transfer(deal["id"])["status"] == "approved"
        assert upd.callback_query.answers[0] == ("✅ Одобрено", False)
        bot.edit_message_reply_markup.assert_awaited_once()
        assert any("Одобрена заявка" in x for x in _texts(bot, -100500))
        assert any("одобрена" in x for x in _texts(bot, 101))
        assert any("Одобрен трансфер" in x for x in _texts(bot, -100999))
        assert [a[0][1] for a in _clean] == ["transfer_request_approved"]
        assert _clean[0][0][0] == MANAGER and _clean[0][0][3] == deal["id"]

    def test_approve_refused_for_admin_and_coach(self, _clean):
        _, deal = _setup()
        bot = _bot()
        for outsider in (990001, 101):
            upd = _group_press(handlers.cb_approve, f"tw:ap:{deal['id']}", bot, user_id=outsider)
            assert upd.callback_query.answers[0][1] is True
            upd = _group_press(handlers.cb_reject, f"tw:rj:{deal['id']}", bot, user_id=outsider)
            assert upd.callback_query.answers[0][1] is True
        assert repo.get_transfer(deal["id"])["status"] == "pending_manager"
        assert not handlers._pending and not _clean and not bot.send_message.await_args_list

    def test_double_press_alerts(self):
        _, deal = _setup()
        bot = _bot()
        _group_press(handlers.cb_approve, f"tw:ap:{deal['id']}", bot)
        upd = _group_press(handlers.cb_approve, f"tw:ap:{deal['id']}", bot)
        text, alert = upd.callback_query.answers[0]
        assert alert and "уже одобрена" in text

    def test_hard_block_shown_as_alert(self):
        wid, deal = _setup()
        repo.update_window_settings(wid, {"ovr_cap": 100})
        upd = _group_press(handlers.cb_approve, f"tw:ap:{deal['id']}", _bot())
        text, alert = upd.callback_query.answers[0]
        assert alert and "Одобрить нельзя" in text
        assert repo.get_transfer(deal["id"])["status"] == "pending_manager"

    def test_reject_asks_reason_in_dm_then_decides(self, _clean):
        _, deal = _setup()
        bot = _bot()
        _group_press(handlers.cb_reject, f"tw:rj:{deal['id']}", bot)
        assert repo.get_transfer(deal["id"])["status"] == "pending_manager"
        prompt = bot.send_message.await_args.kwargs
        assert prompt["chat_id"] == MANAGER
        data = [b.callback_data for row in prompt["reply_markup"].inline_keyboard for b in row]
        assert data == [f"tw:rjn:{deal['id']}", "tw:rjc"]
        assert handlers.AWAITING_INPUT.filter(FakeMessage(MANAGER, "x"))

        upd = _run(handlers.on_input, _update(text="дубль заявки"), bot)
        assert "отклонена" in upd.effective_message.replies[-1]
        t = repo.get_transfer(deal["id"])
        assert t["status"] == "rejected" and t["decided_reason"] == "дубль заявки"
        assert not handlers._pending
        assert any("дубль заявки" in x for x in _texts(bot, 101))
        assert any("Отклонена заявка" in x for x in _texts(bot, -100500))
        assert [a[0][1] for a in _clean] == ["transfer_request_rejected"]
        assert _clean[0][1]["new"]["reason"] == "дубль заявки"

    def test_reject_without_reason_button(self):
        _, deal = _setup()
        bot = _bot()
        _group_press(handlers.cb_reject, f"tw:rj:{deal['id']}", bot)
        _run(handlers.cb_reject_no_reason, _update(data=f"tw:rjn:{deal['id']}"), bot)
        t = repo.get_transfer(deal["id"])
        assert t["status"] == "rejected" and t["decided_reason"] is None

    def test_reject_cancel_keeps_request(self):
        _, deal = _setup()
        bot = _bot()
        _group_press(handlers.cb_reject, f"tw:rj:{deal['id']}", bot)
        _run(handlers.cb_reject_cancel, _update(data="tw:rjc"), bot)
        assert repo.get_transfer(deal["id"])["status"] == "pending_manager" and not handlers._pending

    def test_stale_no_reason_button_does_nothing(self):
        _, deal = _setup()
        upd = _run(handlers.cb_reject_no_reason, _update(data=f"tw:rjn:{deal['id']}"), _bot())
        assert upd.callback_query.answers[0][1] is True
        assert repo.get_transfer(deal["id"])["status"] == "pending_manager"

    def test_reject_button_when_manager_never_started_bot(self):
        _, deal = _setup()
        bot = _bot()

        async def _fail(chat_id, text=None, **kwargs):
            raise handlers.TelegramError("Forbidden")

        bot.send_message = AsyncMock(side_effect=_fail)
        upd = _group_press(handlers.cb_reject, f"tw:rj:{deal['id']}", bot)
        assert upd.callback_query.answers[0][1] is True and not handlers._pending
