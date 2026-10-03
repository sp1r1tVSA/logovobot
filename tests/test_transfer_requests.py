"""Тесты этапа 3: заявки на трансферы (сделка, спешл, урна, свободные агенты, API и уведомления)."""

import asyncio
import datetime as dt
import io
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import database
from time_utils import now_msk_str
from transfers import notify, repo, requests as req_mod, service


@pytest.fixture(autouse=True)
def _clean_tables():
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
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN "
            "('transfer_windows', 'transfers', 'transfer_club_budgets')"
        )
    yield


def _user(user_id: int, username: str, team_name: str | None = None):
    with database.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (telegram_id, username, team_name) VALUES (?, ?, ?)",
            (user_id, username, team_name),
        )


def _squad(team, *players):
    with database.transaction() as conn:
        for item in players:
            name = item[0] if isinstance(item, tuple) else item
            pos = item[1] if isinstance(item, tuple) and len(item) > 1 else "CM"
            conn.execute("INSERT INTO squad_players (team_name, player_name, position) VALUES (?, ?, ?)",
                         (team, name, pos))


def _window(status="open"):
    wid = repo.create_window(1, 10, title="ТО Зима 2026")
    if status == "open":
        repo.open_window(wid, 1)
    elif status == "closed":
        repo.open_window(wid, 1)
        repo.close_window(wid, 1)
    return repo.get_window(wid)


# ─── Тесты создания сделок и переходов ───────────────────────────────────────

class TestDeals:
    def test_create_deal_buy(self):
        w = _window("open")
        _user(101, "coach_chelsea", "Челси")
        _user(102, "coach_arsenal", "Арсенал")
        repo.set_club_budget(w["id"], "Челси", 50000, 1)
        repo.set_club_budget(w["id"], "Арсенал", 50000, 1)

        deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="B. Saka", price="15", ovr=106)
        assert deal["status"] == "pending_counterparty"
        assert deal["from_club"] == "Арсенал"
        assert deal["to_club"] == "Челси"
        assert deal["from_user"] == 102
        assert deal["to_user"] == 101
        assert deal["initiator_id"] == 101
        assert deal["price_k"] == 15000
        assert deal["ovr"] == 106

    def test_create_deal_sell(self):
        w = _window("open")
        _user(101, "coach_chelsea", "Челси")
        _user(102, "coach_arsenal", "Арсенал")
        repo.set_club_budget(w["id"], "Челси", 50000, 1)
        repo.set_club_budget(w["id"], "Арсенал", 50000, 1)

        deal = req_mod.create_deal(101, role="sell", other_club="Арсенал", player="E. Fernandez", price="20,5", ovr=108)
        assert deal["status"] == "pending_counterparty"
        assert deal["from_club"] == "Челси"
        assert deal["to_club"] == "Арсенал"
        assert deal["from_user"] == 101
        assert deal["to_user"] == 102
        assert deal["initiator_id"] == 101
        assert deal["price_k"] == 20500

    def test_deal_same_club_forbidden(self):
        _window("open")
        _user(101, "coach_chelsea", "Челси")
        with pytest.raises(service.InputError, match="со своим же клубом"):
            req_mod.create_deal(101, role="buy", other_club="Челси", player="Palmer", price="10", ovr=105)

    def test_deal_counterparty_without_coach(self):
        _window("open")
        _user(101, "coach_chelsea", "Челси")
        with pytest.raises(service.InputError, match="нет тренера"):
            req_mod.create_deal(101, role="buy", other_club="Ливерпуль", player="Salah", price="10", ovr=105)

    def test_deal_ovr_cap_blocked(self):
        _window("open")
        _user(101, "coach_chelsea", "Челси")
        _user(102, "coach_arsenal", "Арсенал")
        with pytest.raises(service.InputError, match="OVR 114"):
            req_mod.create_deal(101, role="buy", other_club="Арсенал", player="God", price="10", ovr=114)

    def test_deal_duplicate_pending(self):
        w = _window("open")
        _user(101, "coach_chelsea", "Челси")
        _user(102, "coach_arsenal", "Арсенал")
        repo.set_club_budget(w["id"], "Челси", 50000, 1)
        repo.set_club_budget(w["id"], "Арсенал", 50000, 1)

        req_mod.create_deal(101, role="sell", other_club="Арсенал", player="Palmer", price="10", ovr=105)
        with pytest.raises(service.InputError, match="уже есть заявка"):
            req_mod.create_deal(101, role="sell", other_club="Арсенал", player="Palmer", price="12", ovr=105)


class TestConfirmationAndWithdraw:
    def test_confirm_by_counterparty(self):
        w = _window("open")
        _user(101, "coach_chelsea", "Челси")
        _user(102, "coach_arsenal", "Арсенал")
        repo.set_club_budget(w["id"], "Челси", 50000, 1)
        repo.set_club_budget(w["id"], "Арсенал", 50000, 1)

        deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="Saka", price="15", ovr=106)

        # Инициатор не может подтвердить сам
        with pytest.raises(service.InputError, match="Заявка не найдена"):
            req_mod.confirm(101, deal["id"])

        # Вторая сторона подтверждает
        confirmed = req_mod.confirm(102, deal["id"])
        assert confirmed["status"] == "pending_manager"

    def test_decline_by_counterparty(self):
        w = _window("open")
        _user(101, "coach_chelsea", "Челси")
        _user(102, "coach_arsenal", "Арсенал")
        repo.set_club_budget(w["id"], "Челси", 50000, 1)
        repo.set_club_budget(w["id"], "Арсенал", 50000, 1)
        deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="Saka", price="15", ovr=106)

        declined = req_mod.decline(102, deal["id"])
        assert declined["status"] == "rejected"
        assert declined["decided_reason"] == req_mod.COUNTERPARTY_DECLINED

    def test_withdraw_by_initiator(self):
        w = _window("open")
        _user(101, "coach_chelsea", "Челси")
        _user(102, "coach_arsenal", "Арсенал")
        repo.set_club_budget(w["id"], "Челси", 50000, 1)
        repo.set_club_budget(w["id"], "Арсенал", 50000, 1)
        deal = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="Saka", price="15", ovr=106)

        # Не инициатор не может отозвать
        with pytest.raises(service.InputError, match="Заявка не найдена"):
            req_mod.withdraw(102, deal["id"])

        withdrawn = req_mod.withdraw(101, deal["id"])
        assert withdrawn["status"] == "withdrawn"


class TestSurchargeAndUrn:
    def test_create_surcharge(self):
        w = _window("open")
        _user(101, "coach_chelsea", "Челси")
        repo.update_window_settings(w["id"], {"surcharge_table": {105: 35000}})
        repo.set_club_budget(w["id"], "Челси", 50000, 1)

        # OVR 105 по таблице windows стоит 35 млн (35000k)
        sc = req_mod.create_surcharge(101, player="Palmer", ovr=105)
        assert sc["status"] == "pending_manager"
        assert sc["kind"] == "surcharge"
        assert sc["to_club"] == "Челси"
        assert sc["ovr"] == 105
        assert sc["price_k"] == 35000

    def test_create_urn_sale(self):
        w = _window("open")
        _user(101, "coach_chelsea", "Челси")
        repo.update_window_settings(w["id"], {"urn_max_per_club": 2})
        # TM 10, Special 2, sellable -> (10+2)/2 = 6 млн
        sale = req_mod.create_urn_sale(101, player="Old Card", tm_price="10", special_price="2", sellable=True)
        assert sale["status"] == "pending_manager"
        assert sale["kind"] == "urn_sale"
        assert sale["price_k"] == 6000
        assert sale["from_club"] == "Челси"

        # Непродаваемая карта -> (10+2)/3 = 4 млн
        sale_unsell = req_mod.create_urn_sale(101, player="Unsellable", tm_price="10", special_price="2", sellable=False)
        assert sale_unsell["price_k"] == 4000

    def test_create_urn_buy(self):
        w = _window("open")
        _user(101, "coach_chelsea", "Челси")
        _user(102, "coach_arsenal", "Арсенал")
        repo.set_club_budget(w["id"], "Арсенал", 50000, 1)

        sale = req_mod.create_urn_sale(101, player="Card X", tm_price="10", special_price="2", sellable=True)
        # Одобряем продажу в урну
        repo.set_transfer_status(sale["id"], "approved", expected=("pending_manager",))

        # Выкуп Arsenal-ом
        buy = req_mod.create_urn_buy(102, urn_item_id=sale["id"])
        assert buy["status"] == "pending_manager"
        assert buy["kind"] == "urn_buy"
        assert buy["from_club"] == req_mod.URN_CLUB
        assert buy["to_club"] == "Арсенал"
        assert buy["price_k"] == 12000  # Полная цена: 10 + 2 млн


# ─── Тесты свободных агентов ─────────────────────────────────────────────────

class TestFreeAgents:
    def test_parse_fa_comment(self):
        text = """1. Erling Haaland OVR 108
2. Borussia Dortmund
3. Manchester City
4. 45
5. 15
6. фото прикрепил"""
        moment = "2026-10-02 20:05:00"
        draft = req_mod.parse_fa_comment(text, commented_at=moment)
        assert draft.player_name == "Erling Haaland"
        assert draft.from_club in ("Borussia Dortmund", "Боруссия Дортмунд")
        assert draft.to_club in ("Manchester City", "Манчестер Сити")
        assert draft.price_k == 45000
        assert draft.reported_budget_k == 15000
        assert draft.ovr == 108
        assert draft.commented_at == moment

    def test_fa_preview_and_record(self):
        w = _window("open")
        _user(101, "coach_mancity", "Манчестер Сити")
        repo.set_club_budget(w["id"], "Манчестер Сити", 100000, 1)

        text = """1. K. Mbappe
2. PSG
3. Manchester City
4. 50
5. 50"""
        draft = req_mod.parse_fa_comment(text, commented_at="2026-10-02 20:01:00")
        preview = req_mod.fa_preview(draft)
        assert preview.can_record
        assert preview.conflict is None
        assert preview.duplicate is None

        rec = req_mod.record_free_agent(draft, actor_id=1)
        assert rec.transfer["status"] == "approved"
        assert rec.transfer["kind"] == "free_agent"
        assert rec.transfer["to_club"] == "Манчестер Сити"
        assert rec.replaced is None

    def test_fa_older_comment_reassigns_player(self):
        w = _window("open")
        _user(101, "coach_mancity", "Манчестер Сити")
        _user(102, "coach_chelsea", "Челси")
        repo.set_club_budget(w["id"], "Манчестер Сити", 100000, 1)
        repo.set_club_budget(w["id"], "Челси", 100000, 1)

        # Сначала записали комментарий Chelsea в 20:05
        draft1 = req_mod.parse_fa_comment("""1. Jude Bellingham\n2. Dortmund\n3. Chelsea\n4. 40\n5. 10""",
                                           commented_at="2026-10-02 20:05:00")
        rec1 = req_mod.record_free_agent(draft1, actor_id=1)
        assert rec1.transfer["to_club"] in ("Chelsea", "Челси")

        # Затем принесли комментарий Man City от 20:02 (раньше!)
        draft2 = req_mod.parse_fa_comment("""1. Jude Bellingham\n2. Dortmund\n3. Manchester City\n4. 40\n5. 20""",
                                           commented_at="2026-10-02 20:02:00")
        preview2 = req_mod.fa_preview(draft2)
        assert preview2.conflict is not None
        assert preview2.conflict["id"] == rec1.transfer["id"]
        assert preview2.can_reassign

        # Переписываем
        rec2 = req_mod.record_free_agent(draft2, actor_id=1, replace_id=rec1.transfer["id"])
        assert rec2.transfer["to_club"] == "Манчестер Сити"
        assert rec2.transfer["status"] == "approved"
        assert rec2.replaced["id"] == rec1.transfer["id"]
        assert rec2.replaced["status"] == "cancelled"


# ─── Тесты уведомлений ───────────────────────────────────────────────────────

class TestNotifications:
    def test_notify_deal_proposal(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock())
        t = {
            "id": 12, "kind": "deal", "from_club": "Arsenal", "to_club": "Chelsea",
            "from_user": 102, "to_user": 101, "initiator_id": 102,
            "player_name": "Saka", "price_k": 15000, "ovr": 106,
        }
        res = asyncio.run(notify.notify_deal_proposal(bot, t))
        assert res is True
        bot.send_message.assert_awaited_once()
        call_kwargs = bot.send_message.await_args.kwargs
        assert call_kwargs["chat_id"] == 101
        assert "Saka" in call_kwargs["text"]

    def test_announce_free_agent(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock())
        repo.bind_topic("feed", -100123, 42, 1)

        t = {
            "id": 99, "kind": "free_agent", "player_name": "Mbappe",
            "from_club": "PSG", "to_club": "Real Madrid",
            "price_k": 50000, "ovr": 110, "commented_at": "2026-10-02 20:01:00",
        }
        res = asyncio.run(notify.announce_free_agent(bot, t))
        assert res is True
        bot.send_message.assert_awaited_once()
        call_kwargs = bot.send_message.await_args.kwargs
        assert call_kwargs["chat_id"] == -100123
        assert call_kwargs["message_thread_id"] == 42
        assert "Mbappe" in call_kwargs["text"]


# ─── Тесты сериализации и Mini App данных ────────────────────────────────────

class TestMiniAppViews:
    def test_my_status(self):
        w = _window("open")
        _user(101, "coach_chelsea", "Челси")
        repo.update_window_settings(w["id"], {"surcharge_table": {105: 35000}})
        repo.set_club_budget(w["id"], "Челси", 50000, 1)
        req_mod.create_surcharge(101, player="Palmer", ovr=105)

        st = req_mod.my_status(101)
        assert st["window"]["status"] == "open"
        assert st["club"] == "Челси"
        assert st["ledger"]["budget_k"] == 50000
        assert len(st["requests"]) == 1
        assert st["requests"][0]["can_withdraw"] is True

    def test_urn_items(self):
        _window("open")
        _user(101, "coach_chelsea", "Челси")
        sale = req_mod.create_urn_sale(101, player="Oldie", tm_price="10", special_price="4", sellable=True)
        # Пока pending_manager — в доступных нет
        assert len(req_mod.urn_items(101)["items"]) == 0
        repo.set_transfer_status(sale["id"], "approved", expected=("pending_manager",))
        items = req_mod.urn_items(101)["items"]
        assert len(items) == 1
        assert items[0]["player_name"] == "Oldie"
        assert items[0]["buy_price_k"] == 14000

    def test_history(self):
        _window("open")
        _user(101, "coach_chelsea", "Челси")
        sale = req_mod.create_urn_sale(101, player="Oldie", tm_price="10", special_price="4", sellable=True)
        assert len(req_mod.history()["items"]) == 0
        repo.set_transfer_status(sale["id"], "approved", expected=("pending_manager",))
        hist = req_mod.history()["items"]
        assert len(hist) == 1
        assert hist[0]["player_name"] == "Oldie"


class TestPhotoProxy:
    """Фото отдаётся через бота: токена нет ни в ответе, ни в логе, кеш закрытый."""

    SECRET = "123456:SECRET-TOKEN-VALUE"

    def _call(self, monkeypatch, bot, caplog):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request
        from transfers import api as tapi

        monkeypatch.setattr(config, "TOKEN", self.SECRET, raising=False)
        monkeypatch.setattr(tapi, "_auth", lambda request: ({"id": 1}, None))
        monkeypatch.setattr(tapi.req_mod, "photo_file_id", lambda user_id, tid: "FILE-ID")
        app = web.Application()
        app["bot"] = bot
        request = make_mocked_request("GET", "/api/transfers/5/photo", app=app, match_info={"id": "5"})
        with caplog.at_level("DEBUG"):
            return asyncio.run(tapi.handle_get_photo(request))

    def test_serves_photo_through_bot_with_private_cache(self, monkeypatch, caplog):
        tg_file = MagicMock()
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"\xff\xd8jpeg"))
        bot = MagicMock()
        bot.get_file = AsyncMock(return_value=tg_file)
        resp = self._call(monkeypatch, bot, caplog)
        bot.get_file.assert_awaited_once_with("FILE-ID")
        assert resp.status == 200 and resp.body == b"\xff\xd8jpeg"
        assert resp.headers["Cache-Control"].startswith("private")
        assert self.SECRET not in str(resp.headers)

    def test_failure_does_not_leak_token(self, monkeypatch, caplog):
        bot = MagicMock()
        bot.get_file = AsyncMock(side_effect=RuntimeError(f"https://api.telegram.org/bot{self.SECRET}/getFile"))
        resp = self._call(monkeypatch, bot, caplog)
        assert resp.status == 502
        assert self.SECRET not in resp.text and self.SECRET not in caplog.text

    def test_no_bot_is_503(self, monkeypatch, caplog):
        monkeypatch.setattr(notify, "_global_bot", None)
        resp = self._call(monkeypatch, None, caplog)
        assert resp.status == 503
