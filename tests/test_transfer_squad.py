"""Этап 5: применение трансфера к составу, откат, отмена одобренной заявки, правило ядра."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import database
from transfers import approval, handlers, repo, requests as req_mod, service, squad

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
    monkeypatch.setattr(handlers, "_prefetch_portrait_later", lambda t: None)
    yield journal
    handlers._pending.clear()


def _user(user_id, username, team):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name) VALUES (?, ?, ?)",
                     (user_id, username, team))


def _squad(team, *players):
    with database.transaction() as conn:
        for item in players:
            name, pos = item if isinstance(item, tuple) else (item, "CM")
            conn.execute("INSERT INTO squad_players (team_name, player_name, position) VALUES (?, ?, ?)",
                         (team, name, pos))


def _names(club):
    return sorted(r["player_name"] for r in repo.squad_rows(club))


def _window():
    wid = repo.create_window(1, 10, title="ТО Зима")
    repo.open_window(wid, 1)
    _user(101, "chelsea", "Челси")
    _user(102, "arsenal", "Арсенал")
    repo.set_club_budget(wid, "Челси", 50000, 1)
    repo.set_club_budget(wid, "Арсенал", 50000, 1)
    return wid


def _deal(player="B. Saka", price="15", ovr=106):
    deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player=player, price=price, ovr=ovr)
    deal = req_mod.confirm(102, deal["id"])
    return approval.approve(MANAGER, deal["id"]).transfer


def _setup():
    """Окно, Арсенал с Сакой и ещё шестью, Челси с двумя, одобренная сделка Сака → Челси."""
    wid = _window()
    _squad("Арсенал", ("B. Saka", "RW"), "P1", "P2", "P3", "P4", "P5", "P6")
    _squad("Челси", "C1", "C2")
    service.snapshot_core(wid)
    return wid, _deal()


# ─── Применение ──────────────────────────────────────────────────────────────

class TestApply:
    def test_deal_moves_player_with_position(self):
        _, t = _setup()
        assert squad.needs_apply(t)
        assert squad.preview(t) == ["➖ B. Saka уходит из Арсенал", "➕ B. Saka приходит в Челси"]
        result = squad.apply(MANAGER, t["id"])
        assert result.lines == ["➖ B. Saka убран из Арсенал", "➕ B. Saka добавлен в Челси"]
        assert "B. Saka" not in _names("Арсенал") and "B. Saka" in _names("Челси")
        row = next(r for r in repo.squad_rows("Челси") if r["player_name"] == "B. Saka")
        assert row["position"] == "RW"
        assert result.transfer["squad_applied_at"] and not squad.needs_apply(result.transfer)
        assert [o["op"] for o in repo.list_squad_ops(t["id"])] == ["remove", "add"]

    def test_added_row_is_normalized(self):
        _, t = _setup()
        squad.apply(MANAGER, t["id"])
        with database.transaction() as conn:
            row = conn.execute("SELECT norm_name, norm_team_name FROM squad_players WHERE player_name = ?",
                               ("B. Saka",)).fetchone()
        assert row["norm_name"] and row["norm_team_name"]

    def test_already_absent_and_present_are_notes(self):
        _, t = _setup()
        with database.transaction() as conn:
            conn.execute("DELETE FROM squad_players WHERE player_name = 'B. Saka'")
        _squad("Челси", ("B. Saka", "RW"))
        result = squad.apply(MANAGER, t["id"])
        assert result.lines == [] and len(result.notes) == 2
        assert repo.list_squad_ops(t["id"]) == []
        assert result.transfer["squad_applied_at"]

    def test_free_agent_and_urn_kinds(self):
        wid = _window()
        _squad("Челси", ("Card X", "ST"), "C1", "C2", "C3", "C4", "C5")
        service.snapshot_core(wid)
        repo.update_window_settings(wid, {"urn_max_per_club": 2})
        sale = req_mod.create_urn_sale(101, player="Card X", tm_price="10", special_price="2", sellable=True)
        sale = approval.approve(MANAGER, sale["id"]).transfer
        assert squad.preview(sale) == ["➖ Card X уходит из Челси"]
        squad.apply(MANAGER, sale["id"])
        assert "Card X" not in _names("Челси")

        buy = req_mod.create_urn_buy(102, urn_item_id=sale["id"])
        buy = approval.approve(MANAGER, buy["id"]).transfer
        squad.apply(MANAGER, buy["id"])
        row = next(r for r in repo.squad_rows("Арсенал") if r["player_name"] == "Card X")
        assert row["position"] is None  # позиция в урне не хранится — у выкупа её нет

    def test_surcharge_does_not_change_squad(self):
        wid = _window()
        repo.update_window_settings(wid, {"surcharge_table": {105: 35000}})
        sc = req_mod.create_surcharge(101, player="Palmer", ovr=105)
        sc = approval.approve(MANAGER, sc["id"]).transfer
        assert not squad.changes_squad(sc) and not squad.needs_apply(sc)
        with pytest.raises(service.InputError, match="Доплата"):
            squad.apply(MANAGER, sc["id"])

    def test_only_manager_and_only_approved(self):
        _, t = _setup()
        for outsider in (990001, 101, None):
            with pytest.raises(service.InputError, match="только ответственный"):
                squad.apply(outsider, t["id"])
        pending = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="P1", price="1", ovr=90)
        with pytest.raises(service.InputError, match="только по одобренной"):
            squad.apply(MANAGER, pending["id"])
        with pytest.raises(service.InputError, match="не найдена"):
            squad.apply(MANAGER, 9999)

    def test_second_apply_refused(self):
        _, t = _setup()
        squad.apply(MANAGER, t["id"])
        with pytest.raises(service.InputError, match="уже изменён"):
            squad.apply(MANAGER, t["id"])
        assert _names("Челси").count("B. Saka") == 1


class TestCoreRule:
    def test_cannot_drop_below_minimum(self):
        wid = _window()
        _squad("Арсенал", "A1", "A2", "A3", "A4", "A5")
        service.snapshot_core(wid)
        t = repo.insert_transfer(wid, "deal", "A1", "approved", from_club="Арсенал", to_club="Челси")
        with pytest.raises(service.InputError, match="минимум 5"):
            squad.apply(MANAGER, t)
        assert "A1" in _names("Арсенал") and "A1" not in _names("Челси")
        assert repo.get_transfer(t)["squad_applied_at"] is None

    def test_newcomer_can_leave_freely(self):
        wid = _window()
        _squad("Арсенал", "A1", "A2", "A3", "A4", "A5")
        service.snapshot_core(wid)
        _squad("Арсенал", "New Guy")  # пришёл после снимка — не ядро
        t = repo.insert_transfer(wid, "deal", "New Guy", "approved", from_club="Арсенал", to_club="Челси")
        assert squad.apply(MANAGER, t).lines


# ─── Откат ───────────────────────────────────────────────────────────────────

class TestRollback:
    def test_rollback_restores_exactly(self):
        _, t = _setup()
        before = (_names("Арсенал"), _names("Челси"))
        squad.apply(MANAGER, t["id"])
        result = squad.rollback(MANAGER, t["id"])
        assert result.lines == ["➖ B. Saka убран из Челси", "➕ B. Saka возвращён в Арсенал"]
        assert (_names("Арсенал"), _names("Челси")) == before
        row = next(r for r in repo.squad_rows("Арсенал") if r["player_name"] == "B. Saka")
        assert row["position"] == "RW"
        t = result.transfer
        assert t["status"] == "approved" and t["squad_applied_at"] is None and squad.needs_apply(t)
        assert repo.list_squad_ops(t["id"]) == []
        # можно применить заново
        squad.apply(MANAGER, t["id"])
        assert "B. Saka" in _names("Челси")

    def test_rollback_without_apply_refused(self):
        _, t = _setup()
        with pytest.raises(service.InputError, match="откатывать нечего"):
            squad.rollback(MANAGER, t["id"])

    def test_later_transfer_blocks_rollback(self):
        wid, t = _setup()
        squad.apply(MANAGER, t["id"])
        # Челси тут же перепродаёт Саку обратно
        later = repo.insert_transfer(wid, "deal", "B. Saka", "approved", from_club="Челси", to_club="Арсенал")
        squad.apply(MANAGER, later)
        with pytest.raises(service.InputError, match=f"#{later}"):
            squad.rollback(MANAGER, t["id"])
        squad.rollback(MANAGER, later)
        squad.rollback(MANAGER, t["id"])
        assert "B. Saka" in _names("Арсенал") and "B. Saka" not in _names("Челси")


# ─── Отмена одобренной ───────────────────────────────────────────────────────

class TestCancel:
    def test_cancel_not_applied(self):
        _, t = _setup()
        cancelled, rolled = squad.cancel(MANAGER, t["id"], "  ошибка  ")
        assert cancelled["status"] == "cancelled" and cancelled["decided_reason"] == "ошибка"
        assert rolled is None and "B. Saka" in _names("Арсенал")

    def test_cancel_applied_rolls_back(self):
        _, t = _setup()
        squad.apply(MANAGER, t["id"])
        cancelled, rolled = squad.cancel(MANAGER, t["id"])
        assert cancelled["status"] == "cancelled" and rolled.lines
        assert "B. Saka" in _names("Арсенал") and "B. Saka" not in _names("Челси")

    def test_cancel_frees_budget_and_slots(self):
        wid, t = _setup()
        buyer, seller = repo.get_club_ledger(wid, "Челси"), repo.get_club_ledger(wid, "Арсенал")
        assert buyer.spent_k == 15000 and buyer.buys_used == 1 and seller.earned_k == 15000
        squad.cancel(MANAGER, t["id"])
        buyer, seller = repo.get_club_ledger(wid, "Челси"), repo.get_club_ledger(wid, "Арсенал")
        assert buyer.spent_k == 0 and buyer.buys_used == 0
        assert seller.earned_k == 0 and seller.sells_used == 0

    def test_cancel_only_approved_and_once(self):
        _, t = _setup()
        squad.cancel(MANAGER, t["id"])
        with pytest.raises(service.InputError, match="только по одобренной"):
            squad.cancel(MANAGER, t["id"])
        with pytest.raises(service.InputError, match="только ответственный"):
            squad.cancel(990001, t["id"])

    def test_urn_sale_with_buy_is_blocked(self):
        wid = _window()
        repo.update_window_settings(wid, {"urn_max_per_club": 2})
        sale = req_mod.create_urn_sale(101, player="Card X", tm_price="10", special_price="2", sellable=True)
        approval.approve(MANAGER, sale["id"])
        buy = req_mod.create_urn_buy(102, urn_item_id=sale["id"])
        with pytest.raises(service.InputError, match=f"#{buy['id']}"):
            squad.cancel(MANAGER, sale["id"])
        approval.approve(MANAGER, buy["id"])
        squad.cancel(MANAGER, buy["id"])
        assert squad.cancel(MANAGER, sale["id"])[0]["status"] == "cancelled"


# ─── Кнопки ──────────────────────────────────────────────────────────────────

def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    bot.send_photo = AsyncMock(return_value=MagicMock(photo=[]))
    bot.edit_message_reply_markup = AsyncMock()
    return bot


def _texts(bot, chat_id=None):
    return [c.kwargs["text"] for c in bot.send_message.await_args_list
            if chat_id is None or c.kwargs.get("chat_id") == chat_id]


class FakeMessage:
    def __init__(self, user_id, chat_type="private", chat_id=None):
        self.text = None
        self.message_id = 55
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type=chat_type, id=chat_id if chat_id is not None else user_id)

    async def reply_text(self, text, **kwargs):
        pass


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.edits = []
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


def _press(handler, data, bot, user_id=MANAGER, chat_type="private"):
    chat_id = None if chat_type == "private" else -100500
    msg = FakeMessage(user_id, chat_type, chat_id)
    upd = SimpleNamespace(effective_user=msg.from_user, effective_chat=msg.chat, effective_message=msg,
                          callback_query=FakeQuery(data, msg), message=msg)
    asyncio.run(handler(upd, SimpleNamespace(bot=bot)))
    return upd.callback_query


def _buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


class TestButtons:
    def test_routes_registered_and_short(self):
        from telegram.ext import CallbackQueryHandler

        added = []
        handlers.register_handlers(SimpleNamespace(add_handler=lambda h, *a, **k: added.append(h)))
        patterns = [h.pattern for h in added if isinstance(h, CallbackQueryHandler)]
        big = 2 ** 63
        for data in (f"tw:sq:{big}", f"tw:sr:{big}", f"tw:cx:{big}", f"tw:cxy:{big}", f"tw:cxn:{big}",
                     "tw:appr:3", f"tw:tr:{big}", "tw:sqall"):
            assert len(data.encode()) <= 64
            assert any(p.match(data) for p in patterns), data

    def test_approve_reply_carries_squad_buttons(self):
        _window()
        _squad("Арсенал", ("B. Saka", "RW"), "P1", "P2", "P3", "P4", "P5")
        deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="B. Saka", price="15", ovr=106)
        deal = req_mod.confirm(102, deal["id"])
        bot = _bot()
        _press(handlers.cb_approve, f"tw:ap:{deal['id']}", bot, chat_type="supergroup")
        reply = next(c.kwargs for c in bot.send_message.await_args_list if c.kwargs.get("chat_id") == -100500)
        assert _buttons(reply["reply_markup"]) == [f"tw:sq:{deal['id']}", f"tw:cx:{deal['id']}"]

    def test_apply_then_rollback_buttons(self, _clean):
        _, t = _setup()
        bot = _bot()
        q = _press(handlers.cb_squad_apply, f"tw:sq:{t['id']}", bot, chat_type="supergroup")
        text, kb = q.edits[-1]
        assert "Состав обновлён" in text and _buttons(kb) == [f"tw:sr:{t['id']}", f"tw:cx:{t['id']}"]
        assert "B. Saka" in _names("Челси")
        assert any("Состав обновлён" in x for x in _texts(bot, 101))
        assert any("Состав обновлён" in x for x in _texts(bot, 102))

        q = _press(handlers.cb_squad_rollback, f"tw:sr:{t['id']}", bot)
        text, kb = q.edits[-1]
        assert "возвращён" in text and _buttons(kb)[0] == f"tw:sq:{t['id']}" and _buttons(kb)[-1] == "tw:appr:0"
        assert [a[0][1] for a in _clean] == ["transfer_squad_applied", "transfer_squad_reverted"]

    def test_double_press_alerts(self):
        _, t = _setup()
        bot = _bot()
        _press(handlers.cb_squad_apply, f"tw:sq:{t['id']}", bot)
        q = _press(handlers.cb_squad_apply, f"tw:sq:{t['id']}", bot)
        assert q.answers[-1][1] is True and "уже изменён" in q.answers[-1][0]

    def test_outsiders_refused(self, _clean):
        _, t = _setup()
        bot = _bot()
        for handler, data in ((handlers.cb_squad_apply, f"tw:sq:{t['id']}"),
                              (handlers.cb_squad_rollback, f"tw:sr:{t['id']}"),
                              (handlers.cb_cancel_ask, f"tw:cx:{t['id']}"),
                              (handlers.cb_cancel_yes, f"tw:cxy:{t['id']}"),
                              (handlers.cb_squad_all, "tw:sqall")):
            for outsider in (990001, 101):
                q = _press(handler, data, bot, user_id=outsider, chat_type="supergroup")
                assert q.answers[-1][1] is True and not q.edits
        assert repo.get_transfer(t["id"])["status"] == "approved" and "B. Saka" in _names("Арсенал")
        assert not _clean and not bot.send_message.await_args_list

    def test_cancel_flow(self, _clean):
        _, t = _setup()
        squad.apply(MANAGER, t["id"])
        repo.bind_topic("feed", -100999, 9, 1)
        bot = _bot()
        q = _press(handlers.cb_cancel_ask, f"tw:cx:{t['id']}", bot)
        text, kb = q.edits[-1]
        assert "Состав клубов будет возвращён" in text
        assert _buttons(kb) == [f"tw:cxy:{t['id']}", f"tw:cxn:{t['id']}"]
        q = _press(handlers.cb_cancel_back, f"tw:cxn:{t['id']}", bot)
        assert repo.get_transfer(t["id"])["status"] == "approved"

        q = _press(handlers.cb_cancel_yes, f"tw:cxy:{t['id']}", bot)
        assert "Отменена заявка" in q.edits[-1][0]
        assert repo.get_transfer(t["id"])["status"] == "cancelled"
        assert "B. Saka" in _names("Арсенал")
        assert any("отменена" in x for x in _texts(bot, 101))
        assert any("Отменён трансфер" in x for x in _texts(bot, -100999))
        assert [a[0][1] for a in _clean] == ["transfer_request_cancelled"]

    def test_approved_list_and_apply_all(self, _clean):
        _, t = _setup()
        other = _deal(player="P1", price="1", ovr=90)
        text, kb = handlers._hub_view()
        assert "tw:appr:0" in _buttons(kb)
        q = _press(handlers.cb_approved, "tw:appr:0", _bot())
        text, kb = q.edits[-1]
        assert "не применено к составам: 2" in text
        assert {f"tw:tr:{t['id']}", f"tw:tr:{other['id']}", "tw:sqall"} <= set(_buttons(kb))

        q = _press(handlers.cb_open_transfer, f"tw:tr:{t['id']}", _bot())
        assert "B. Saka уходит из Арсенал" in q.edits[-1][0]

        q = _press(handlers.cb_squad_all, "tw:sqall", _bot())
        assert "Применено к составам: 2" in q.edits[-1][0]
        assert {"B. Saka", "P1"} <= set(_names("Челси"))
        assert [a[0][1] for a in _clean] == ["transfer_squad_applied"] * 2

    def test_apply_all_reports_core_failure(self):
        wid = _window()
        _squad("Арсенал", "A1", "A2", "A3", "A4", "A5")
        service.snapshot_core(wid)
        bad = repo.insert_transfer(wid, "deal", "A1", "approved", from_club="Арсенал", to_club="Челси")
        ok = repo.insert_transfer(wid, "free_agent", "Free Guy", "approved", to_club="Челси")
        q = _press(handlers.cb_squad_all, "tw:sqall", _bot())
        assert "Применено к составам: 1" in q.edits[-1][0] and f"#{bad}" in q.edits[-1][0]
        assert repo.get_transfer(ok)["squad_applied_at"] and "Free Guy" in _names("Челси")
