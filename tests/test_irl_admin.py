"""tests/test_irl_admin.py — admin side of IRL betting: panel buttons and /irl_settle."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import database
from handlers import admin_irl, admin_ops
from services import admin_journal, irl_jobs
from services.sports.models import MatchWinnerOdds, PrematchFixture

NOW = datetime(2026, 10, 3, 10, 0, 0)
DAY = NOW.date().isoformat()
ADMIN = 111


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.delenv("LOGOVO_LOCKDOWN", raising=False)
    monkeypatch.setattr(config, "IRL_ENABLED", True)
    monkeypatch.setattr(config, "IRL_BOOKMAKER_ID", 8)
    monkeypatch.setattr(config, "IRL_COMPETITION_PRIORITY", [2, 39])
    monkeypatch.setattr(config, "IRL_AUTO_PUBLISH", False)
    monkeypatch.setattr(admin_ops, "is_global_admin", lambda uid: uid == ADMIN)
    monkeypatch.setattr(database, "now_msk", lambda: NOW)
    monkeypatch.setattr(database, "now_msk_str", lambda fmt="%Y-%m-%d %H:%M:%S": NOW.strftime(fmt))
    monkeypatch.setattr(admin_irl, "now_msk", lambda: NOW)
    monkeypatch.setattr(admin_irl, "today_msk_str", lambda: DAY)
    journal = AsyncMock()
    monkeypatch.setattr(admin_irl.admin_journal, "record", journal)
    with database.transaction() as conn:
        conn.execute("DELETE FROM irl_bets")
        conn.execute("DELETE FROM irl_matches")
    yield journal


class FakeProvider:
    def __init__(self, fixtures=(), odds=None):
        self.fixtures = None if fixtures is None else {f.fixture_id: f for f in fixtures}
        self.odds = odds if odds is not None else {}

    async def get_prematch_fixtures(self, date, league_ids=None):
        if self.fixtures is None:
            return None
        return [f for f in self.fixtures.values() if league_ids is None or f.league_id in league_ids]

    async def get_prematch_fixture(self, fixture_id):
        fid = int(fixture_id) if str(fixture_id).isdigit() else str(fixture_id)
        return (self.fixtures or {}).get(fid)

    async def get_match_winner_odds(self, fixture_id, bookmaker_id):
        fid = int(fixture_id) if str(fixture_id).isdigit() else str(fixture_id)
        o = self.odds.get(fid)
        return None if o is None else MatchWinnerOdds(fid, bookmaker_id, *o)


def fx(fid, league=39, home="Arsenal", away="Chelsea", hours=8):
    return PrematchFixture(fixture_id=fid, league_id=league, league_name=f"L{league}", home=home,
                           away=away, kickoff=NOW + timedelta(hours=hours))


def use_provider(monkeypatch, provider):
    monkeypatch.setattr(admin_irl, "_get_provider", lambda: provider)


def update(data=None, user_id=ADMIN, chat_type="private"):
    message = SimpleNamespace(reply_text=AsyncMock())
    query = None
    if data is not None:
        query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(), message=message)
    return SimpleNamespace(effective_chat=SimpleNamespace(type=chat_type, id=1),
                           effective_user=SimpleNamespace(id=user_id),
                           effective_message=message, callback_query=query)


def ctx(args=None):
    return SimpleNamespace(args=args or [], bot=SimpleNamespace(username="LogovoBot"))


def press(data, **kw):
    u = update(data, **kw)
    run(admin_irl.cb_irl(u, ctx()))
    return u


def shown(u):
    """Text of the last edit/reply under a pressed button."""
    if u.callback_query.edit_message_text.await_args:
        return u.callback_query.edit_message_text.await_args.args[0]
    return u.effective_message.reply_text.await_args.args[0]


def draft(fid=1, hours=8, odds=(2.0, 3.3, 3.8)):
    mid, _ = database.create_irl_draft(fid, 39, "L39", "Arsenal", "Chelsea", NOW + timedelta(hours=hours),
                                       *odds, bet_day=DAY)
    return mid


def make_user(uid, balance=5000):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, ?, 'user')",
                     (uid, f"irla{uid}"))
    database.get_or_create_wallet(uid)
    with database.transaction() as conn:
        conn.execute("UPDATE user_wallets SET balance = ? WHERE user_id = ?", (balance, uid))


def balance(uid):
    with database.transaction() as conn:
        return conn.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (uid,)).fetchone()["balance"]


# ─── Access ──────────────────────────────────────────────────────────────────

class TestAccess:
    def test_non_admin_cannot_press_buttons(self):
        mid = draft()
        u = press(f"irl:pub:{mid}", user_id=999)
        assert u.callback_query.answer.await_args.kwargs.get("show_alert") is True
        assert database.get_irl_match(mid)["status"] == "draft"

    def test_non_admin_cannot_settle(self):
        mid = draft()
        u = update(user_id=999)
        run(admin_irl.cmd_irl_settle(u, ctx([str(mid), "1"])))
        assert database.get_irl_match(mid)["status"] == "draft"
        assert "Доступ запрещён" in u.effective_message.reply_text.await_args.args[0]

    def test_group_chat_gets_pm_link(self):
        u = update(chat_type="supergroup")
        run(admin_irl.cmd_irl(u, ctx()))
        kb = u.effective_message.reply_text.await_args.kwargs["reply_markup"]
        assert kb.inline_keyboard[0][0].url == "https://t.me/logovobot?start=irl"

    def test_garbage_callbacks_are_refused(self):
        for data in ("irl:pub:abc", "irl:pk:1:notaday:0", "irl:nope", "irl:can:"):
            u = press(data)
            assert u.callback_query.answer.await_args.kwargs.get("show_alert") is True


# ─── Panel / preview keyboard ────────────────────────────────────────────────

class TestPanel:
    def test_keyboard_per_status(self):
        d = draft(1)
        o = draft(2)
        database.publish_irl_match(o)
        kb = admin_irl.day_keyboard(database.list_irl_matches(bet_day=DAY), DAY)
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert {f"irl:pub:{d}", f"irl:rep:{d}", f"irl:can:{d}", f"irl:can:{o}", f"irl:add:{DAY}",
                f"irl:puball:{DAY}", f"irl:day:{DAY}"} <= set(data)
        assert f"irl:pub:{o}" not in data and f"irl:rep:{o}" not in data
        assert all(len(c.encode()) <= 64 for c in data)

    def test_cmd_irl_shows_today(self):
        draft()
        u = update()
        run(admin_irl.cmd_irl(u, ctx()))
        text = u.effective_message.reply_text.await_args.args[0]
        assert "Arsenal" in text and "черновик" in text

    def test_preview_dm_carries_keyboard(self, monkeypatch):
        draft()
        monkeypatch.setattr(config, "ADMIN_IDS", [ADMIN], raising=False)
        sent = []

        class Bot:
            async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None, **kw):
                sent.append(reply_markup)

        run(irl_jobs._send_preview(Bot(), DAY))
        assert sent and sent[0] is not None
        assert any(b.callback_data.startswith("irl:pub:") for row in sent[0].inline_keyboard for b in row)


# ─── Publish / cancel ────────────────────────────────────────────────────────

class TestPublishCancel:
    def test_publish_opens_match_and_journals(self, _setup):
        mid = draft()
        u = press(f"irl:pub:{mid}")
        assert database.get_irl_match(mid)["status"] == "open"
        assert "опубликован" in shown(u)
        assert _setup.await_args.args[1] == "irl_match_published"

    def test_publish_all_only_touches_drafts(self):
        a, b = draft(1), draft(2)
        press(f"irl:puball:{DAY}")
        assert {database.get_irl_match(a)["status"], database.get_irl_match(b)["status"]} == {"open"}

    def test_publish_started_match_is_refused(self):
        mid = draft(hours=-1)
        u = press(f"irl:pub:{mid}")
        assert "уже начался" in shown(u)
        assert database.get_irl_match(mid)["status"] == "draft"

    def test_cancel_asks_for_confirmation_first(self):
        mid = draft()
        u = press(f"irl:can:{mid}")
        assert "Отменить матч" in shown(u)
        assert database.get_irl_match(mid)["status"] == "draft"

    def test_cancel_refunds_bets(self, _setup):
        make_user(7001)
        mid = draft()
        database.publish_irl_match(mid)
        ok, _ = database.place_irl_bet(7001, mid, "home", 300, 2.0)
        assert ok and balance(7001) == 4700
        u = press(f"irl:canok:{mid}")
        assert database.get_irl_match(mid)["status"] == "void"
        assert balance(7001) == 5000
        assert "возвращено ставок: 1" in shown(u)
        assert _setup.await_args.args[1] == "irl_match_cancelled"

    def test_cancel_twice_does_not_refund_twice(self):
        make_user(7002)
        mid = draft()
        database.publish_irl_match(mid)
        database.place_irl_bet(7002, mid, "away", 100, 3.8)
        press(f"irl:canok:{mid}")
        u = press(f"irl:canok:{mid}")
        assert balance(7002) == 5000
        assert "уже рассчитан или аннулирован" in shown(u)


# ─── Add / replace ───────────────────────────────────────────────────────────

class TestAddReplace:
    def test_add_lists_only_new_upcoming_matches(self, monkeypatch):
        taken = draft(1)
        use_provider(monkeypatch, FakeProvider([fx(1), fx(2, home="Real", away="Barca"), fx(3, hours=-2)]))
        u = press(f"irl:add:{DAY}")
        text = shown(u)
        assert "Real" in text and "Arsenal" not in text
        buttons = [b.callback_data for row in u.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
                   .inline_keyboard for b in row]
        assert f"irl:pk:2:{DAY}:0" in buttons and f"irl:pk:3:{DAY}:0" not in buttons
        assert taken

    def test_add_provider_down(self, monkeypatch):
        use_provider(monkeypatch, FakeProvider(None))
        assert "не отдал расписание" in shown(press(f"irl:add:{DAY}"))

    def test_pick_creates_admin_draft(self, monkeypatch, _setup):
        use_provider(monkeypatch, FakeProvider([fx(2, home="Real", away="Barca")], {2: (1.9, 3.5, 4.1)}))
        press(f"irl:pk:2:{DAY}:0")
        m = database.get_irl_match_by_fixture(2)
        assert m["status"] == "draft" and m["picked_by"] == "admin" and m["odd_draw"] == 3.5
        assert _setup.await_args.args[1] == "irl_match_added"

    def test_pick_string_hex_fixture_id(self, monkeypatch, _setup):
        hex_fid = "c74384a37fbf9492e85a6fd2"
        use_provider(monkeypatch, FakeProvider([fx(hex_fid, home="Belarus", away="Finland")], {hex_fid: (2.1, 3.2, 3.9)}))
        u = press(f"irl:pk:{hex_fid}:{DAY}:0")
        assert not u.callback_query.answer.await_args or u.callback_query.answer.await_args.args != ("Некорректная кнопка",)
        m = database.get_irl_match_by_fixture(hex_fid)
        assert m is not None
        assert m["status"] == "draft" and m["picked_by"] == "admin" and m["odd_home"] == 2.1

    def test_pick_without_odds_creates_nothing(self, monkeypatch):
        use_provider(monkeypatch, FakeProvider([fx(2)], {}))
        u = press(f"irl:pk:2:{DAY}:0")
        assert "нет кэфов" in u.callback_query.answer.await_args.args[0]
        assert database.get_irl_match_by_fixture(2) is None

    def test_pick_started_match_refused(self, monkeypatch):
        use_provider(monkeypatch, FakeProvider([fx(2, hours=-1)], {2: (2, 3, 4)}))
        press(f"irl:pk:2:{DAY}:0")
        assert database.get_irl_match_by_fixture(2) is None

    def test_pick_without_bookmaker_refused(self, monkeypatch):
        monkeypatch.setattr(config, "IRL_BOOKMAKER_ID", 0)
        use_provider(monkeypatch, FakeProvider([fx(2)], {2: (2, 3, 4)}))
        press(f"irl:pk:2:{DAY}:0")
        assert database.get_irl_match_by_fixture(2) is None

    def test_replace_voids_draft_and_adds_new(self, monkeypatch, _setup):
        old = draft(1)
        use_provider(monkeypatch, FakeProvider([fx(1), fx(2, home="Real", away="Barca")], {2: (1.9, 3.5, 4.1)}))
        press(f"irl:pk:2:{DAY}:{old}")
        assert database.get_irl_match(old)["status"] == "void"
        assert database.get_irl_match_by_fixture(2)["status"] == "draft"
        assert [c.args[1] for c in _setup.await_args_list] == ["irl_match_replaced"]

    def test_replace_button_refuses_published_match(self, monkeypatch):
        mid = draft()
        database.publish_irl_match(mid)
        u = press(f"irl:rep:{mid}")
        assert "только отменяется" in shown(u)

    def test_replace_does_not_void_match_published_meanwhile(self, monkeypatch):
        old = draft(1)
        database.publish_irl_match(old)
        use_provider(monkeypatch, FakeProvider([fx(2, home="Real", away="Barca")], {2: (1.9, 3.5, 4.1)}))
        u = press(f"irl:pk:2:{DAY}:{old}")
        assert database.get_irl_match(old)["status"] == "open"
        assert database.get_irl_match_by_fixture(2) is not None
        assert "уже не черновик" in shown(u)


# ─── /irl_settle ─────────────────────────────────────────────────────────────

def settle(args):
    u = update()
    run(admin_irl.cmd_irl_settle(u, ctx(args)))
    return u.effective_message.reply_text.await_args.args[0]


def started_match_with_bet(uid, outcome="home", amount=200):
    make_user(uid)
    mid = draft(uid, hours=1)
    database.publish_irl_match(mid)
    odd = {"home": 2.0, "draw": 3.3, "away": 3.8}[outcome]
    ok, info = database.place_irl_bet(uid, mid, outcome, amount, odd)
    assert ok, info
    with database.transaction() as conn:       # the match kicked off
        conn.execute("UPDATE irl_matches SET status = 'closed', kickoff_at = ? WHERE id = ?",
                     ((NOW - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"), mid))
    return mid


class TestSettleCommand:
    def test_settle_home_pays_winner(self, _setup):
        mid = started_match_with_bet(7101, "home", 200)
        text = settle([str(mid), "1"])
        assert "рассчитан" in text and "П1" in text
        assert database.get_irl_match(mid)["result"] == "home"
        assert balance(7101) == 5000 - 200 + 400
        assert _setup.await_args.args[1] == "irl_match_settled"

    def test_settle_losing_side(self):
        mid = started_match_with_bet(7102, "home", 200)
        settle([str(mid), "x"])
        assert database.get_irl_match(mid)["result"] == "draw"
        assert balance(7102) == 4800

    def test_void_refunds(self):
        mid = started_match_with_bet(7103, "away", 150)
        assert "аннулирован" in settle([str(mid), "void"])
        assert balance(7103) == 5000 and database.get_irl_match(mid)["status"] == "void"

    def test_second_settle_does_not_pay_again(self):
        mid = started_match_with_bet(7104, "home", 200)
        settle([str(mid), "1"])
        assert "уже рассчитан" in settle([str(mid), "1"])
        assert balance(7104) == 5200

    def test_draft_cannot_be_settled(self):
        mid = draft()
        assert "не опубликован" in settle([str(mid), "1"])
        assert database.get_irl_match(mid)["status"] == "draft"

    def test_bad_arguments(self):
        mid = draft()
        assert "Формат" in settle(["abc", "1"])
        assert "Формат" in settle([str(mid)])
        assert "Исход" in settle([str(mid), "5"])
        assert "нет" in settle(["99999", "1"])

    def test_no_args_lists_waiting_matches(self):
        mid = started_match_with_bet(7105)
        text = settle([])
        assert f"#{mid}" in text and "irl_settle" in text


class TestWiring:
    def test_actions_are_catalogued(self):
        for action in ("irl_match_added", "irl_match_replaced", "irl_match_published",
                       "irl_match_cancelled", "irl_match_settled", "irl_match_restored"):
            assert action in admin_journal.ACTIONS


class TestRestoreIrlMatch:
    def test_voided_match_appears_in_candidates(self, monkeypatch):
        old = draft(1)
        database.void_irl_match(old, "Отменён")
        use_provider(monkeypatch, FakeProvider([fx(1)], {1: (2.1, 3.2, 3.5)}))
        u = press(f"irl:add:{DAY}")
        text = shown(u)
        assert "Arsenal" in text
        buttons = [b.callback_data for row in u.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
                   .inline_keyboard for b in row]
        assert f"irl:pk:1:{DAY}:0" in buttons

    def test_pick_voided_match_resurrects_draft(self, monkeypatch, _setup):
        old = draft(1)
        database.void_irl_match(old, "Отменён")
        use_provider(monkeypatch, FakeProvider([fx(1)], {1: (2.5, 3.4, 3.1)}))
        press(f"irl:pk:1:{DAY}:0")
        m = database.get_irl_match(old)
        assert m["status"] == "draft"
        assert m["odd_home"] == 2.5
        assert m["void_reason"] is None
        assert _setup.await_args.args[1] == "irl_match_restored"

    def test_restore_button_in_day_keyboard_for_voided_match(self):
        mid = draft(1)
        database.void_irl_match(mid, "Отменён")
        u = press(f"irl:day:{DAY}")
        buttons = [b.callback_data for row in u.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
                   .inline_keyboard for b in row]
        assert f"irl:rest:{mid}" in buttons

    def test_restore_button_restores_draft(self, monkeypatch, _setup):
        mid = draft(1)
        database.void_irl_match(mid, "Отменён")
        use_provider(monkeypatch, FakeProvider([fx(1)], {1: (2.2, 3.3, 3.4)}))
        u = press(f"irl:rest:{mid}")
        m = database.get_irl_match(mid)
        assert m["status"] == "draft"
        assert m["void_reason"] is None
        assert _setup.await_args.args[1] == "irl_match_restored"
        assert "возвращён в черновики" in shown(u)

    def test_restore_refused_if_match_started(self, monkeypatch):
        mid = draft(1, hours=-1)
        database.void_irl_match(mid, "Отменён")
        u = press(f"irl:rest:{mid}")
        assert "уже начался или завершился" in u.callback_query.answer.await_args.args[0]
        assert database.get_irl_match(mid)["status"] == "void"

    def test_menu_has_irl(self):
        from handlers import bot_menu
        assert "irl" in [c.command for c in bot_menu.GLOBAL_ADMIN_COMMANDS]
        assert "irl" not in [c.command for c in bot_menu.ADMIN_COMMANDS]


class TestTomorrowNavigation:
    def test_day_keyboard_has_tomorrow_button_on_today(self):
        u = press(f"irl:day:{DAY}")
        tomorrow = (NOW + timedelta(days=1)).date().isoformat()
        yesterday = (NOW - timedelta(days=1)).date().isoformat()
        kb = u.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"irl:day:{tomorrow}" in callbacks
        assert f"irl:day:{yesterday}" in callbacks

    def test_navigating_to_tomorrow_shows_tomorrow_panel(self):
        tomorrow = (NOW + timedelta(days=1)).date().isoformat()
        u = press(f"irl:day:{tomorrow}")
        text = shown(u)
        assert f"IRL-ставки на {tomorrow} (Завтра)" in text
        kb = u.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"irl:day:{DAY}" in callbacks
        assert f"irl:add:{tomorrow}" in callbacks

    def test_add_candidate_on_tomorrow(self, monkeypatch):
        tomorrow = (NOW + timedelta(days=1)).date().isoformat()
        use_provider(monkeypatch, FakeProvider([fx(20, hours=28)], {20: (1.9, 3.5, 4.0)}))
        u = press(f"irl:add:{tomorrow}")
        text = shown(u)
        assert "на завтра" in text
        assert "Arsenal" in text
        press(f"irl:pk:20:{tomorrow}:0")
        matches = database.list_irl_matches(bet_day=tomorrow)
        assert len(matches) == 1
        assert matches[0]["provider_fixture_id"] == "20"
        assert matches[0]["bet_day"] == tomorrow
        assert matches[0]["status"] == "draft"
