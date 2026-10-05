"""Доска «ищу / продаю»: лоты, отклик как обычная сделка, снятие и экран в панели `/to`."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import database
from services import admin_journal
from transfers import approval, board, handlers, notify, repo, requests as req_mod, sanctions, service

MANAGER = 777
GROUP, FEED_THREAD = -100500, 42


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with database.transaction() as conn:
        conn.execute("DELETE FROM transfer_board_responses")
        conn.execute("DELETE FROM transfer_board_lots")
        for table in (
            "transfer_squad_ops", "transfer_slot_purchases", "transfer_club_budgets",
            "transfer_core_snapshot", "transfer_players", "transfer_topics",
            "transfer_sanctions", "transfer_reminders", "squad_players", "users",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE transfers SET urn_item_id = NULL")
        conn.execute("DELETE FROM transfer_swap_links")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM transfer_windows")
    monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", MANAGER, raising=False)
    monkeypatch.setattr(config, "ADMIN_IDS", [990001])
    journal = []

    async def _record(*args, **kwargs):
        journal.append((args, kwargs))

    monkeypatch.setattr(handlers.admin_journal, "record", _record)
    yield journal


def _user(user_id, username, team):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name) VALUES (?, ?, ?)",
                     (user_id, username, team))


def _squad(team, *players):
    with database.transaction() as conn:
        for name in players:
            conn.execute("INSERT INTO squad_players (team_name, player_name, position) VALUES (?, ?, 'CM')",
                         (team, name))


def _bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    bot.send_photo = AsyncMock(return_value=MagicMock())
    return bot


def _window(open_it=True):
    wid = repo.create_window(1, 10, title="ТО Зима")
    if open_it:
        repo.open_window(wid, 1)
    for uid, name, team in ((101, "chelsea", "Челси"), (102, "arsenal", "Арсенал"), (103, "pool", "Ливерпуль")):
        _user(uid, name, team)
        repo.set_club_budget(wid, team, 100000, 1)
    _squad("Арсенал", "B. Saka", "P1", "P2", "P3", "P4", "P5", "P6")
    _squad("Челси", "C1", "C2", "C3", "C4", "C5", "C6")
    _squad("Ливерпуль", "L1", "L2", "L3", "L4", "L5", "L6")
    return wid


def _sell_saka():
    return board.create_lot(102, side="sell", player="B. Saka", ovr=106, price="15")


# ─── Лоты ────────────────────────────────────────────────────────────────────

class TestCreateLot:
    def test_sell_lot(self):
        _window()
        lot = _sell_saka()
        assert (lot["club_name"], lot["side"], lot["player_name"], lot["ovr"], lot["price_k"]) == \
            ("Арсенал", "sell", "B. Saka", 106, 15000)
        assert lot["status"] == "open"

    def test_buy_lot_with_note_only(self):
        _window()
        lot = board.create_lot(101, side="buy", note="  ищу  ЦЗ до 105 ")
        assert lot["player_name"] is None and lot["note"] == "ищу ЦЗ до 105"

    @pytest.mark.parametrize("kwargs, message", [
        (dict(side="sell"), "Укажите игрока"),
        (dict(side="buy"), "опишите, кого ищете"),
        (dict(side="swap", player="X"), "ищете вы игрока или продаёте"),
        (dict(side="buy", note="x" * 201), "длиннее 200"),
        (dict(side="sell", player="B. Saka", price="abc"), "Не понял сумму"),
    ])
    def test_input_errors(self, kwargs, message):
        _window()
        with pytest.raises(service.InputError, match=message):
            board.create_lot(102, **kwargs)

    def test_needs_open_window(self):
        _window(open_it=False)
        with pytest.raises(service.InputError, match="окно открыто"):
            _sell_saka()

    def test_coach_without_club(self):
        _window()
        with pytest.raises(service.InputError):
            board.create_lot(555, side="buy", note="кто-нибудь")

    def test_limit_per_club(self):
        _window()
        for name in ("P1", "P2", "P3"):
            board.create_lot(102, side="sell", player=name)
        with pytest.raises(service.InputError, match="уже 3 лота"):
            board.create_lot(102, side="sell", player="P4")
        # Чужой клуб лимит не трогает.
        board.create_lot(101, side="buy", note="ищу вратаря")

    def test_duplicate_lot(self):
        _window()
        _sell_saka()
        with pytest.raises(service.InputError, match="уже висит"):
            board.create_lot(102, side="sell", player="b.  saka")
        # Тот же игрок с другой стороны — другой лот.
        board.create_lot(102, side="buy", player="B. Saka")

    def test_sanctioned_club_blocked(self):
        _window()
        sanctions.add(MANAGER, club_name="Челси", seasons=1)
        with pytest.raises(service.InputError, match="санкцией"):
            board.create_lot(101, side="buy", note="ищу")
        data = board.list_board(101)
        assert data["can_post"] is False


class TestListBoard:
    def test_flags_for_viewers(self):
        _window()
        lot = _sell_saka()
        own = board.list_board(102)
        assert own["open"] and own["club"] == "Арсенал" and own["my_open"] == 1 and own["can_post"]
        assert own["lots"][0]["mine"] is True and own["lots"][0]["can_respond"] is False
        other = board.list_board(101)
        card = other["lots"][0]
        assert card["id"] == lot["id"] and card["mine"] is False and card["can_respond"] is True
        assert card["price"] == "15 млн" or "15" in card["price"]
        assert card["side_label"] == "Продаю"

    def test_closed_window_shows_nothing(self):
        wid = _window()
        _sell_saka()
        repo.close_window(wid, 1)
        data = board.list_board(101)
        assert data["open"] is False and data["lots"] == [] and data["can_post"] is False

    def test_newest_first(self):
        _window()
        first = board.create_lot(101, side="buy", note="ищу вратаря")
        second = _sell_saka()
        assert [lot["id"] for lot in board.live_lots()] == [second["id"], first["id"]]


# ─── Отклик ──────────────────────────────────────────────────────────────────

class TestRespond:
    def test_response_is_a_linked_deal(self):
        _window()
        lot = _sell_saka()
        deal = board.respond(101, lot["id"], role="buy", other_club="Арсенал", player="B. Saka",
                             price="15", ovr=106)
        assert deal["status"] == "pending_counterparty" and deal["kind"] == "deal"
        assert repo.board_lot_of(deal["id"]) == lot["id"]
        assert repo.get_lot(lot["id"])["responses_pending"] == 1
        assert board.list_board(101)["lots"][0]["responses"] == {"pending": 1, "approved": 0}

    def test_buy_lot_answered_by_selling(self):
        _window()
        lot = board.create_lot(101, side="buy", note="ищу вингера")
        deal = board.respond(102, lot["id"], role="sell", player="B. Saka", price="14", ovr=106)
        assert deal["from_club"] == "Арсенал" and deal["to_club"] == "Челси"

    @pytest.mark.parametrize("kwargs, message", [
        (dict(role="sell", player="B. Saka", price="15"), "отвечают покупкой"),
        (dict(role="buy", other_club="Ливерпуль", player="B. Saka", price="15"), "клубу лота"),
        (dict(role="buy", player="P1", price="15"), "другого игрока"),
    ])
    def test_mismatches(self, kwargs, message):
        _window()
        lot = _sell_saka()
        with pytest.raises(service.InputError, match=message):
            board.respond(101, lot["id"], **kwargs)
        assert repo.get_lot(lot["id"])["responses_pending"] == 0

    def test_own_lot(self):
        _window()
        lot = board.create_lot(102, side="buy", note="ищу")
        with pytest.raises(service.InputError, match="вашего клуба"):
            board.respond(102, lot["id"], role="sell", player="P1", price="5")

    def test_sold_lot_leaves_board(self):
        _window()
        lot = _sell_saka()
        deal = board.respond(101, lot["id"], role="buy", player="B. Saka", price="15", ovr=106)
        approval.approve(MANAGER, req_mod.confirm(102, deal["id"])["id"])
        assert repo.get_lot(lot["id"])["responses_approved"] == 1
        assert board.live_lots() == []
        with pytest.raises(service.InputError, match="снят с доски"):
            board.respond(103, lot["id"], role="buy", player="B. Saka", price="16")

    def test_buy_lot_survives_approval(self):
        _window()
        lot = board.create_lot(101, side="buy", note="ищу вингера")
        deal = board.respond(102, lot["id"], role="sell", player="B. Saka", price="14", ovr=106)
        approval.approve(MANAGER, req_mod.confirm(101, deal["id"])["id"])
        assert [x["id"] for x in board.live_lots()] == [lot["id"]]


# ─── Снятие ──────────────────────────────────────────────────────────────────

class TestClose:
    def test_author_closes(self):
        _window()
        lot = _sell_saka()
        with pytest.raises(service.InputError, match="только его клуб"):
            board.close_lot(101, lot["id"])
        closed = board.close_lot(102, lot["id"])
        assert closed["status"] == "closed" and closed["closed_reason"] == "author"
        assert board.live_lots() == []
        with pytest.raises(service.InputError, match="уже снят"):
            board.close_lot(102, lot["id"])

    def test_manager_removes(self):
        _window()
        lot = _sell_saka()
        with pytest.raises(service.InputError, match="ответственный"):
            board.remove_lot(990001, lot["id"])
        removed = board.remove_lot(MANAGER, lot["id"])
        assert removed["closed_reason"] == "manager" and removed["closed_by"] == MANAGER

    def test_unknown_lot(self):
        _window()
        with pytest.raises(service.InputError, match="не найден"):
            board.close_lot(102, 999)


# ─── Уведомления ─────────────────────────────────────────────────────────────

class TestNotify:
    def test_lot_line(self):
        line = notify.lot_line({"side": "sell", "player_name": "Rodri", "ovr": 106, "price_k": 20000})
        assert line.startswith("Продаю: <b>Rodri (OVR 106)</b>") and "20" in line
        assert notify.lot_line({"side": "buy", "player_name": None, "ovr": None, "price_k": None}) == "<b>Ищу</b>"

    def test_announce_quietly_to_feed(self):
        _window()
        repo.bind_topic("feed", GROUP, FEED_THREAD, 1)
        bot = _bot()
        assert asyncio.run(notify.announce_board_lot(bot, _sell_saka())) is True
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs["chat_id"] == GROUP and kwargs["message_thread_id"] == FEED_THREAD
        assert kwargs["disable_notification"] is True and "Доска ТО" in kwargs["text"]

    def test_no_feed_no_dm(self):
        _window()
        bot = _bot()
        assert asyncio.run(notify.announce_board_lot(bot, _sell_saka())) is False
        bot.send_message.assert_not_called()

    def test_response_note_in_proposal(self):
        _window()
        lot = _sell_saka()
        deal = board.respond(101, lot["id"], role="buy", player="B. Saka", price="15", ovr=106)
        bot = _bot()
        asyncio.run(notify.notify_deal_proposal(bot, deal, note=notify.board_response_note(lot)))
        texts = [c.kwargs.get("text", "") for c in bot.send_message.call_args_list]
        assert any("Отклик на ваш лот" in t for t in texts)


# ─── Панель /to ──────────────────────────────────────────────────────────────

class FakeQuery:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(chat=SimpleNamespace(type="private", id=user_id))
        self.edits = []
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        if text:
            self.alerts.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


def _press(handler, data, user_id=MANAGER, bot=None):
    query = FakeQuery(data, user_id)
    user = SimpleNamespace(id=user_id)
    chat = SimpleNamespace(type="private", id=user_id)
    update = SimpleNamespace(callback_query=query, effective_user=user, effective_chat=chat,
                             effective_message=query.message, message=None)
    asyncio.run(handler(update, SimpleNamespace(bot=bot or _bot())))
    return query


def _buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class TestPanel:
    def test_hub_button_only_with_lots(self):
        _window()
        assert "tw:bd:0" not in _buttons(handlers._hub_view()[1])
        _sell_saka()
        assert "tw:bd:0" in _buttons(handlers._hub_view()[1])

    def test_board_view_manager_buttons(self):
        _window()
        lot = _sell_saka()
        text, kb = handlers._board_view(0, True)
        assert "B. Saka" in text and "Арсенал" in text
        assert f"tw:bdx:{lot['id']}" in _buttons(kb)
        _, kb = handlers._board_view(0, False)
        assert not any(b.startswith("tw:bdx:") for b in _buttons(kb))

    def test_board_view_paginates(self):
        _window()
        _user(104, "spurs", "Тоттенхэм")
        for uid in (101, 102, 103, 104):
            for i in range(3):
                board.create_lot(uid, side="buy", note=f"ищу {i}")
        text, kb = handlers._board_view(0, True)
        assert "Страница 1 из 2" in text and "tw:bd:1" in _buttons(kb)

    def test_manager_removes_from_panel(self, _clean):
        _window()
        lot = _sell_saka()
        bot = _bot()
        query = _press(handlers.cb_board_remove, f"tw:bdx:{lot['id']}", bot=bot)
        assert repo.get_lot(lot["id"])["status"] == "closed"
        assert query.edits and f"Лот #{lot['id']} снят" in query.edits[-1][0]
        (args, _kwargs), = _clean
        assert args[1] == "transfer_board_lot_removed" and args[3] == lot["id"]
        assert bot.send_message.call_args.kwargs["chat_id"] == 102

    def test_admin_cannot_remove(self, _clean):
        _window()
        lot = _sell_saka()
        query = _press(handlers.cb_board_remove, f"tw:bdx:{lot['id']}", user_id=990001)
        assert repo.get_lot(lot["id"])["status"] == "open" and query.alerts and not _clean

    def test_journal_action_labelled(self):
        assert "transfer_board_lot_removed" in admin_journal.ACTIONS
