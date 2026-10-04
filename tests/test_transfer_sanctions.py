"""Этап 7: санкции ТО (постановка, снятие, блокировка, уведомления) и история в Mini App."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import database
from services import admin_journal
from transfers import api as tapi, handlers, notify, repo, requests as req_mod, sanctions, slots
from transfers.service import InputError


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with database.transaction() as conn:
        for table in (
            "transfer_squad_ops", "transfer_slot_purchases", "transfer_club_budgets",
            "transfer_core_snapshot", "transfer_players", "transfer_topics",
            "transfer_sanctions", "squad_players", "users", "coin_transactions", "user_wallets",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE transfers SET urn_item_id = NULL")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM transfer_windows")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     "('transfer_windows', 'transfers', 'transfer_club_budgets', 'transfer_sanctions')")
    handlers._sanction_drafts.clear()
    monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", 777, raising=False)
    monkeypatch.setattr(config, "ADMIN_IDS", [990001])


def _user(user_id, username, team):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name) VALUES (?, ?, ?)",
                     (user_id, username, team))


def _window(season=10, *, open_=True):
    wid = repo.create_window(season, 1, title="ТО Зима")
    if open_:
        repo.open_window(wid, 1)
    _user(101, "chelsea", "Челси")
    _user(102, "arsenal", "Арсенал")
    repo.set_club_budget(wid, "Челси", 50000, 1)
    repo.set_club_budget(wid, "Арсенал", 50000, 1)
    return wid


class TestParseAndFind:
    @pytest.mark.parametrize("raw,expected", [("1", 1), (" 3 ", 3), (5, 5)])
    def test_parse_seasons_ok(self, raw, expected):
        assert sanctions.parse_seasons(raw) == expected

    @pytest.mark.parametrize("raw", ["0", "6", "-1", "abc", ""])
    def test_parse_seasons_rejects(self, raw):
        with pytest.raises(InputError):
            sanctions.parse_seasons(raw)

    def test_find_club_exact_and_alias(self):
        assert sanctions.find_club("Челси") == "Челси"
        assert sanctions.find_club("  челси ") == "Челси"

    def test_find_club_unknown(self):
        with pytest.raises(InputError):
            sanctions.find_club("Несуществующий ФК")
        with pytest.raises(InputError):
            sanctions.find_club("  ")

    def test_find_coach_by_username_and_id(self):
        _user(101, "chelsea", "Челси")
        by_name = sanctions.find_coach("@chelsea")
        assert by_name == {"user_id": 101, "username": "chelsea", "club": "Челси"}
        assert sanctions.find_coach("101")["user_id"] == 101

    def test_find_coach_unknown(self):
        with pytest.raises(InputError):
            sanctions.find_coach("@ghost")
        with pytest.raises(InputError):
            sanctions.find_coach("424242")


class TestAddLift:
    def test_add_club_spans_n_seasons_from_window_season(self):
        _window(season=10)
        s = sanctions.add(777, club_name="Челси", seasons=3, reason="  Нарушение  ")
        assert (s["from_season_id"], s["until_season_id"]) == (10, 12)
        assert s["reason"] == "Нарушение" and s["created_by"] == 777 and s["lifted_at"] is None

    def test_add_needs_exactly_one_target(self):
        _window()
        with pytest.raises(InputError):
            sanctions.add(777, seasons=1)
        with pytest.raises(InputError):
            sanctions.add(777, club_name="Челси", user_id=101, seasons=1)

    def test_add_refuses_duplicate_and_long_reason(self):
        _window()
        sanctions.add(777, club_name="Челси", seasons=1)
        with pytest.raises(InputError, match="Уже под санкцией"):
            sanctions.add(777, club_name="Челси", seasons=2)
        with pytest.raises(InputError, match="длиннее"):
            sanctions.add(777, club_name="Арсенал", seasons=1, reason="x" * 301)

    def test_lift_then_again(self):
        _window()
        s = sanctions.add(777, user_id=101, seasons=2)
        lifted = sanctions.lift(s["id"], 777)
        assert lifted["lifted_at"] and lifted["lifted_by"] == 777
        with pytest.raises(InputError, match="уже снята"):
            sanctions.lift(s["id"], 777)
        with pytest.raises(InputError, match="нет"):
            sanctions.lift(9999, 777)

    def test_can_sanction_again_after_lift(self):
        _window()
        s = sanctions.add(777, club_name="Челси", seasons=1)
        sanctions.lift(s["id"], 777)
        assert sanctions.add(777, club_name="Челси", seasons=1)["id"] != s["id"]

    def test_labels(self):
        _window()
        club = sanctions.add(777, club_name="Челси", seasons=1)
        coach = sanctions.add(777, user_id=102, seasons=2)
        assert sanctions.subject_label(club) == "клуб Челси"
        assert sanctions.subject_label(coach) == "тренер @arsenal"
        assert sanctions.span_label(club, {10: "Осень"}) == "сезон Осень"
        assert sanctions.span_label(coach, {10: "A", 11: "B"}) == "сезоны A — B"
        assert sanctions.span_label(club, {}) == "сезон №10"


class TestOverviewAndViewer:
    def test_overview_splits_active_and_past(self):
        _window()
        a = sanctions.add(777, club_name="Челси", seasons=2)
        b = sanctions.add(777, club_name="Арсенал", seasons=1)
        sanctions.lift(b["id"], 777)
        data = sanctions.overview()
        assert data["season"] == 10
        assert [s["id"] for s in data["active"]] == [a["id"]]
        assert [s["id"] for s in data["past"]] == [b["id"]]

    def test_expired_sanction_is_past(self):
        _window(season=10)
        repo.add_sanction(club_name="Челси", user_id=None, from_season_id=7, until_season_id=9,
                          reason=None, created_by=1)
        data = sanctions.overview()
        assert data["active"] == [] and len(data["past"]) == 1
        assert sanctions.for_user(101, "Челси") is None

    def test_for_user_club_scope_follows_new_coach(self):
        _window()
        sanctions.add(777, club_name="Челси", seasons=2, reason="Спам")
        info = sanctions.for_user(101, "Челси")
        assert info["scope"] == "club" and info["seasons_left"] == 2 and info["reason"] == "Спам"
        with database.transaction() as conn:  # клуб достался новому тренеру
            conn.execute("DELETE FROM users WHERE telegram_id = 101")
        _user(150, "newcoach", "Челси")
        assert sanctions.for_user(150, "Челси")["scope"] == "club"
        assert sanctions.for_user(102, "Арсенал") is None

    def test_for_user_coach_scope_follows_person(self):
        _window()
        sanctions.add(777, user_id=101, seasons=1)
        assert sanctions.for_user(101, "Челси")["scope"] == "coach"
        assert sanctions.for_user(101, "Ливерпуль")["scope"] == "coach"
        assert sanctions.for_user(102, "Челси") is None

    def test_for_user_seasons_left_counts_current(self):
        _window(season=10)
        repo.add_sanction(club_name="Челси", user_id=None, from_season_id=9, until_season_id=11,
                          reason=None, created_by=1)
        assert sanctions.for_user(101, "Челси")["seasons_left"] == 2

    def test_my_status_carries_sanction(self):
        _window()
        assert req_mod.my_status(101)["sanction"] is None
        sanctions.add(777, club_name="Челси", seasons=1)
        assert req_mod.my_status(101)["sanction"]["scope"] == "club"

    def test_coaches_to_notify(self):
        _window()
        club = sanctions.add(777, club_name="Челси", seasons=1)
        coach = sanctions.add(777, user_id=102, seasons=1)
        assert sanctions.coaches_to_notify(club) == [101]
        assert sanctions.coaches_to_notify(coach) == [102]


class TestBlocking:
    def test_sanctioned_club_cannot_create_requests(self):
        _window()
        sanctions.add(777, club_name="Челси", seasons=1)
        with pytest.raises(InputError):
            req_mod.create_deal(101, role="buy", other_club="Арсенал", player="B. Saka", price="15", ovr=106)

    def test_sanctioned_coach_cannot_create_requests(self):
        _window()
        sanctions.add(777, user_id=101, seasons=1)
        with pytest.raises(InputError):
            req_mod.create_urn_sale(101, player="Oldie", tm_price="10", special_price="4", sellable=True)

    def test_lifting_unblocks(self):
        _window()
        s = sanctions.add(777, club_name="Челси", seasons=1)
        sanctions.lift(s["id"], 777)
        deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="B. Saka", price="15", ovr=106)
        assert deal["status"] == "pending_counterparty"

    def test_other_club_unaffected(self):
        _window()
        sanctions.add(777, club_name="Арсенал", seasons=1)
        deal = req_mod.create_urn_sale(101, player="Oldie", tm_price="10", special_price="4", sellable=True)
        assert deal["status"] == "pending_manager"

    def test_slot_purchase_refused(self):
        wid = _window()
        repo.update_window_settings(wid, {"slot_price_coins": 300, "max_extra_slots": 2})
        database.get_or_create_wallet(101)
        with database.transaction() as conn:
            conn.execute("UPDATE user_wallets SET balance = 1000 WHERE user_id = 101")
        sanctions.add(777, user_id=101, seasons=1)
        assert not slots.info(101)["available"]


class TestNotify:
    def test_added_dm_reaches_every_recipient_once(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        sanction = {"reason": "Спам <b>"}
        sent = asyncio.run(notify.notify_sanction(
            bot, sanction, [101, 101, 150], subject="клуб Челси", span="сезон 10"))
        assert sent == 2 and bot.send_message.await_count == 2
        text = bot.send_message.await_args.kwargs["text"]
        assert "Санкция" in text and "клуб Челси" in text and "&lt;b&gt;" in text

    def test_lifted_dm(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        sent = asyncio.run(notify.notify_sanction(
            bot, {}, [101], subject="клуб Челси", span="", lifted=True))
        assert sent == 1
        assert "снята" in bot.send_message.await_args.kwargs["text"]

    def test_failed_dm_is_not_counted(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("blocked"))
        assert asyncio.run(notify.notify_sanction(bot, {}, [101], subject="x", span="y")) == 0


class TestJournal:
    def test_actions_are_labelled(self):
        for key in ("transfer_sanction_added", "transfer_sanction_lifted"):
            assert key in admin_journal.ACTIONS
            assert admin_journal.ACTIONS[key][0] == "transfers"


class TestPanel:
    def test_view_lists_active_with_lift_buttons(self):
        _window()
        s = sanctions.add(777, club_name="Челси", seasons=1, reason="Спам")
        text, kb = handlers._sanctions_view()
        assert "Челси" in text and "Спам" in text
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"tw:sl:{s['id']}" in data and "tw:sadd:club" in data and "tw:sadd:coach" in data

    def test_view_empty(self):
        _window()
        text, _ = handlers._sanctions_view()
        assert "Действующих санкций нет" in text

    def test_finish_sanction_adds_journals_and_notifies(self, monkeypatch):
        _window()
        record = AsyncMock()
        monkeypatch.setattr(handlers.admin_journal, "record", record)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        draft = handlers._save_sanction_draft("Челси", None, "клуб Челси")
        head = asyncio.run(handlers._finish_sanction(bot, 777, draft, 2, "Спам"))
        assert "Санкция поставлена" in head and "1 из 1" in head
        assert record.await_args.args[1] == "transfer_sanction_added"
        assert repo.is_sanctioned(10, club_name="Челси")
        assert draft not in handlers._sanction_drafts
        assert bot.send_message.await_args.kwargs["chat_id"] == 101

    def test_finish_sanction_with_stale_draft(self):
        with pytest.raises(InputError):
            asyncio.run(handlers._finish_sanction(MagicMock(), 777, "deadbeef", 1, None))

    def test_input_flow_resolves_club_and_coach(self):
        _window()
        update = MagicMock()
        update.effective_message.reply_text = AsyncMock()
        asyncio.run(handlers._input_sanction_target(update, {"target": "club"}, "Челси"))
        asyncio.run(handlers._input_sanction_target(update, {"target": "coach"}, "@arsenal"))
        assert update.effective_message.reply_text.await_count == 2
        kinds = [(d["club_name"], d["user_id"]) for d in handlers._sanction_drafts.values()]
        assert ("Челси", None) in kinds and (None, 102) in kinds

    def test_callbacks_are_registered_for_all_buttons(self):
        source = open(handlers.__file__, encoding="utf-8").read()
        for pattern in ("^tw:sanc$", r"^tw:sadd:(club|coach)$", r"^tw:sn:[a-f0-9]+:\d+$",
                        r"^tw:snr:[a-f0-9]+:\d+$", r"^tw:sl:\d+$", r"^tw:sly:\d+$"):
            assert pattern in source


class TestHistory:
    def _seed(self):
        w1 = _window(season=10)
        sale = req_mod.create_urn_sale(101, player="Oldie", tm_price="10", special_price="4", sellable=True)
        repo.set_transfer_status(sale["id"], "approved", expected=("pending_manager",))
        other = req_mod.create_urn_sale(102, player="Veteran", tm_price="10", special_price="4", sellable=True)
        return w1, sale, other

    def test_public_shows_approved_only_with_windows_and_clubs(self):
        w1, sale, other = self._seed()
        data = req_mod.history(101)
        assert data["window"]["id"] == w1 and not data["mine"]
        assert [i["id"] for i in data["items"]] == [sale["id"]]
        assert [w["id"] for w in data["windows"]] == [w1]
        assert "Челси" in data["clubs"]

    def test_mine_shows_all_statuses_of_own_club_only(self):
        _, sale, other = self._seed()
        mine = req_mod.history(101, mine=True)
        assert mine["mine"]
        assert [i["id"] for i in mine["items"]] == [sale["id"]]
        arsenal = req_mod.history(102, mine=True)
        assert [i["id"] for i in arsenal["items"]] == [other["id"]]
        assert arsenal["items"][0]["status"] == "pending_manager"

    def test_mine_requires_a_viewer(self):
        self._seed()
        assert req_mod.history(None, mine=True)["mine"] is False

    def test_club_filter(self):
        _, sale, _ = self._seed()
        assert [i["id"] for i in req_mod.history(101, club="Челси")["items"]] == [sale["id"]]
        assert req_mod.history(101, club="Арсенал")["items"] == []

    def test_other_window_and_unknown_window_fallback(self):
        w1, sale, _ = self._seed()
        repo.close_window(w1, 1)
        w2 = repo.create_window(11, 1, title="ТО Лето")
        repo.open_window(w2, 1)
        old = req_mod.history(101, window_id=w1)
        assert old["window"]["id"] == w1 and [i["id"] for i in old["items"]] == [sale["id"]]
        assert req_mod.history(101)["window"]["id"] == w2
        assert req_mod.history(101, window_id=99999)["window"]["id"] == w2
        assert {w["id"] for w in old["windows"]} == {w1, w2}

    def test_no_windows(self):
        data = req_mod.history(101)
        assert data["window"] is None and data["items"] == [] and data["windows"] == []


class TestRoute:
    def _call(self, monkeypatch, query):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        monkeypatch.setattr(tapi, "_auth", lambda r: ({"id": 101}, None))
        request = make_mocked_request("GET", "/api/transfers/history" + query, app=web.Application())
        response = asyncio.run(tapi.handle_get_history(request))
        return response.status, json.loads(response.text)

    def test_query_params_reach_history(self, monkeypatch):
        seen = {}

        def fake(user_id, **kwargs):
            seen.update(user_id=user_id, **kwargs)
            return {"items": []}

        monkeypatch.setattr(tapi.req_mod, "history", fake)
        status, body = self._call(monkeypatch, "?window=7&mine=1&club=" + "Ч" * 200)
        assert status == 200 and body["status"] == "ok"
        assert seen["user_id"] == 101 and seen["window_id"] == 7 and seen["mine"] is True
        assert len(seen["club"]) == 80

    def test_defaults_and_garbage(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(tapi.req_mod, "history", lambda user_id, **kw: seen.update(kw) or {"items": []})
        self._call(monkeypatch, "?window=abc&mine=0")
        assert seen == {"window_id": None, "mine": False, "club": None}

    def test_end_to_end(self, monkeypatch):
        _window()
        _, body = self._call(monkeypatch, "?mine=1")
        assert body["data"]["mine"] is True and body["data"]["window"]["title"] == "ТО Зима"


class TestPortraitUrl:
    """Портрет игрока в карточке истории: только из кэша, без сети."""

    def test_no_file_gives_none(self, tmp_path, monkeypatch):
        from services.graphics import player_photos
        monkeypatch.setattr(player_photos, "PHOTOS_DIR", str(tmp_path))
        assert req_mod.portrait_url("Oldie", "Челси") is None
        assert req_mod.portrait_url(None) is None

    def test_club_file_wins_then_clubless_fallback(self, tmp_path, monkeypatch):
        from services.graphics import player_photos
        monkeypatch.setattr(player_photos, "PHOTOS_DIR", str(tmp_path))
        (tmp_path / "oldie.png").write_bytes(b"x" * 10)
        assert req_mod.portrait_url("Oldie", "Челси", "Арсенал") == "/assets/players/oldie.png"
        (tmp_path / "oldie_арсенал.png").write_bytes(b"x" * 10)
        url = req_mod.portrait_url("Oldie", "Челси", "Арсенал")
        assert url.startswith("/assets/players/oldie_") and url.endswith(".png")
        assert "%D0" in url  # кириллица в имени файла закодирована

    def test_urn_is_not_a_club_for_the_lookup(self, tmp_path, monkeypatch):
        from services.graphics import player_photos
        monkeypatch.setattr(player_photos, "PHOTOS_DIR", str(tmp_path))
        (tmp_path / "oldie_урна.png").write_bytes(b"x" * 10)
        assert req_mod.portrait_url("Oldie", req_mod.URN_CLUB) is None

    def test_prefetch_tries_source_club_then_target_and_skips_urn(self, monkeypatch):
        from services.graphics import player_photos
        calls = []

        def fake_fetch(name, team=None, **kw):
            calls.append(team)
            return "/x.png" if team == "Арсенал" else None

        monkeypatch.setattr(player_photos, "fetch_and_cache", fake_fetch)
        monkeypatch.setattr(req_mod, "portrait_url", lambda *a: None)
        t = {"player_name": "Oldie", "from_club": "Челси", "to_club": "Арсенал"}
        assert req_mod.prefetch_portrait(t) == "/x.png"
        assert calls == ["Челси", "Арсенал"]
        calls.clear()
        assert req_mod.prefetch_portrait({"player_name": "Oldie", "from_club": "Челси",
                                          "to_club": req_mod.URN_CLUB}) is None
        assert calls == ["Челси"]
        calls.clear()
        assert req_mod.prefetch_portrait({"player_name": "Oldie", "from_club": req_mod.URN_CLUB,
                                          "to_club": None}) is None
        assert calls == []

    def test_prefetch_skips_network_when_portrait_cached_and_never_raises(self, monkeypatch):
        from services.graphics import player_photos

        def boom(*a, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr(player_photos, "fetch_and_cache", boom)
        monkeypatch.setattr(req_mod, "portrait_url", lambda *a: "/assets/players/o.png")
        assert req_mod.prefetch_portrait({"player_name": "Oldie", "to_club": "Арсенал"}) is None
        monkeypatch.setattr(req_mod, "portrait_url", lambda *a: None)
        assert req_mod.prefetch_portrait({"player_name": "Oldie", "to_club": "Арсенал"}) is None

    def test_history_items_carry_portrait_url(self, tmp_path, monkeypatch):
        from services.graphics import player_photos
        monkeypatch.setattr(player_photos, "PHOTOS_DIR", str(tmp_path))
        (tmp_path / "oldie.png").write_bytes(b"x" * 10)
        _window(season=10)
        sale = req_mod.create_urn_sale(101, player="Oldie", tm_price="10", special_price="4", sellable=True)
        repo.set_transfer_status(sale["id"], "approved", expected=("pending_manager",))
        item = req_mod.history(101)["items"][0]
        assert item["portrait_url"] == "/assets/players/oldie.png"
