"""Этап 6: доп. слоты за монеты — списание, потолок, санкции, лимиты клуба, маршруты."""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

import config
import database
from transfers import api as tapi, notify, repo, service, slots
from transfers.service import InputError

PRICE = 300


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
                     "('transfer_windows', 'transfers', 'transfer_club_budgets')")
    monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", 777, raising=False)
    monkeypatch.setattr(config, "ADMIN_IDS", [990001])


def _user(user_id, username, team):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name) VALUES (?, ?, ?)",
                     (user_id, username, team))


def _balance(user_id, amount):
    database.get_or_create_wallet(user_id)
    with database.transaction() as conn:
        conn.execute("UPDATE user_wallets SET balance = ? WHERE user_id = ?", (amount, user_id))


def _window(*, price=PRICE, cap=2, open_=True):
    wid = repo.create_window(10, 1, title="ТО Зима")
    repo.update_window_settings(wid, {"slot_price_coins": price, "max_extra_slots": cap})
    if open_:
        repo.open_window(wid, 1)
    _user(101, "chelsea", "Челси")
    _user(102, "arsenal", "Арсенал")
    repo.set_club_budget(wid, "Челси", 50000, 1)
    _balance(101, 1000)
    return wid


class TestInfo:
    def test_card_shows_price_cap_and_limits(self):
        _window()
        data = slots.info(101)
        assert data["available"] and data["reason"] is None
        assert (data["price"], data["max_extra"], data["bought"], data["left"]) == (PRICE, 2, 0, 2)
        assert data["balance"] == 1000 and data["club"] == "Челси"
        assert data["buys_limit"] == data["buys_used"] + 3

    def test_closed_window(self):
        _window(open_=False)
        data = slots.info(101)
        assert not data["available"] and "закрыто" in data["reason"]

    def test_no_window_at_all(self):
        _user(101, "chelsea", "Челси")
        assert not slots.info(101)["available"]

    def test_disabled_by_default(self):
        _window(price=0, cap=0)
        data = slots.info(101)
        assert not data["available"] and "выключена" in data["reason"]

    def test_no_club(self):
        _window()
        _user(103, "nobody", "")
        assert "клуб" in slots.info(103)["reason"]

    def test_not_enough_coins_is_explained(self):
        _window()
        _balance(101, 100)
        assert "Не хватает" in slots.info(101)["reason"]


class TestBuy:
    def test_buy_spends_coins_and_adds_a_slot(self):
        wid = _window()
        before = repo.get_club_ledger(wid, "Челси")
        result = slots.buy(101, "buy")
        assert result["price"] == PRICE and result["club"] == "Челси"
        assert database.get_wallet_balance(101) == 1000 - PRICE
        after = repo.get_club_ledger(wid, "Челси")
        assert after.buys_limit == before.buys_limit + 1
        assert after.sells_limit == before.sells_limit
        [purchase] = repo.list_slot_purchases(wid, "Челси")
        assert (purchase["slot_type"], purchase["price_coins"], purchase["user_id"]) == ("buy", PRICE, 101)
        assert result["state"]["bought"] == 1 and result["state"]["left"] == 1

    def test_sell_slot_extends_only_sells(self):
        wid = _window()
        before = repo.get_club_ledger(wid, "Челси")
        slots.buy(101, "sell")
        after = repo.get_club_ledger(wid, "Челси")
        assert after.sells_limit == before.sells_limit + 1 and after.buys_limit == before.buys_limit

    def test_transaction_is_linked_and_not_a_wager(self):
        wid = _window()
        slots.buy(101, "buy")
        [tx] = [t for t in database.get_coin_transactions(101) if t["transaction_type"] == "transfer_slot"]
        assert tx["amount"] == -PRICE and tx["reference_type"] == "transfer_slot"
        assert tx["balance_after"] == 1000 - PRICE
        assert repo.list_slot_purchases(wid)[0]["coin_tx_id"] == tx["id"]
        assert database.get_or_create_wallet(101)["total_wagered"] == 0

    def test_cap_is_per_club_for_both_kinds(self):
        wid = _window(cap=2)
        slots.buy(101, "buy")
        slots.buy(101, "sell")
        with pytest.raises(InputError, match="Лимит докупок исчерпан"):
            slots.buy(101, "buy")
        assert database.get_wallet_balance(101) == 1000 - 2 * PRICE
        assert len(repo.list_slot_purchases(wid)) == 2

    def test_cap_does_not_leak_between_clubs(self):
        _window(cap=1)
        _balance(102, 1000)
        slots.buy(101, "buy")
        assert slots.buy(102, "buy")["club"] == "Арсенал"

    def test_not_enough_coins_changes_nothing(self):
        wid = _window()
        _balance(101, PRICE - 1)
        with pytest.raises(InputError, match="Не хватает монет"):
            slots.buy(101, "buy")
        assert database.get_wallet_balance(101) == PRICE - 1
        assert repo.list_slot_purchases(wid) == []

    def test_exact_balance_is_enough(self):
        _window()
        _balance(101, PRICE)
        slots.buy(101, "buy")
        assert database.get_wallet_balance(101) == 0

    def test_failed_insert_rolls_the_coins_back(self, monkeypatch):
        wid = _window()

        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(repo, "add_slot_purchase", boom)
        with pytest.raises(RuntimeError):
            slots.buy(101, "buy")
        assert database.get_wallet_balance(101) == 1000
        assert repo.list_slot_purchases(wid) == []

    def test_bad_type(self):
        _window()
        with pytest.raises(InputError):
            slots.buy(101, "gift")

    def test_closed_window_refuses(self):
        _window(open_=False)
        with pytest.raises(InputError, match="закрыто"):
            slots.buy(101, "buy")
        assert database.get_wallet_balance(101) == 1000

    def test_disabled_refuses(self):
        _window(price=0, cap=0)
        with pytest.raises(InputError, match="выключена"):
            slots.buy(101, "buy")

    def test_sanctioned_club_refuses(self):
        wid = _window()
        repo.add_sanction(club_name="Челси", user_id=None, from_season_id=10, until_season_id=10,
                          reason="x", created_by=1)
        with pytest.raises(InputError, match="санкция"):
            slots.buy(101, "buy")
        assert database.get_wallet_balance(101) == 1000
        assert repo.list_slot_purchases(wid) == []

    def test_sanctioned_coach_refuses(self):
        _window()
        repo.add_sanction(club_name=None, user_id=101, from_season_id=10, until_season_id=10,
                          reason="x", created_by=1)
        with pytest.raises(InputError, match="санкция"):
            slots.buy(101, "buy")

    def test_refunded_purchase_frees_the_cap(self):
        wid = _window(cap=1)
        slots.buy(101, "buy")
        repo.refund_slot_purchase(repo.list_slot_purchases(wid)[0]["id"])
        assert slots.info(101)["left"] == 1
        slots.buy(101, "buy")

    def test_new_window_starts_clean(self):
        wid = _window()
        slots.buy(101, "buy")
        service.close_window(wid, 1)
        wid2 = repo.create_window(10, 1, title="ТО Лето")
        repo.update_window_settings(wid2, {"slot_price_coins": PRICE, "max_extra_slots": 2})
        repo.open_window(wid2, 1)
        assert slots.info(101)["bought"] == 0


class TestSettingsFromPanel:
    def test_price_and_cap_parse_as_integers(self):
        wid = _window(price=0, cap=0)
        service.update_setting(wid, "slot_price_coins", "250")
        service.update_setting(wid, "max_extra_slots", "3")
        data = slots.info(101)
        assert data["price"] == 250 and data["max_extra"] == 3 and data["available"]

    def test_price_change_keeps_old_purchases_price(self):
        wid = _window()
        slots.buy(101, "buy")
        service.update_setting(wid, "slot_price_coins", "999")
        assert repo.list_slot_purchases(wid)[0]["price_coins"] == PRICE


class TestSpendCoins:
    def test_refuses_bad_amounts(self):
        _balance(101, 100)
        for bad in (0, -5, True, 1.5):
            assert database.spend_coins(101, bad, "x") is None
        assert database.get_wallet_balance(101) == 100

    def test_never_goes_negative(self):
        _balance(101, 100)
        assert database.spend_coins(101, 101, "x") is None
        assert database.spend_coins(101, 100, "x") is not None
        assert database.get_wallet_balance(101) == 0


class TestAnnounce:
    def test_feed_line_mentions_club_kind_and_price(self, monkeypatch):
        sent = []

        async def _post(bot, topic, text, reply_markup=None):
            sent.append((topic, text))
            return True

        monkeypatch.setattr(notify, "post_to_topic", _post)
        asyncio.run(notify.announce_slot_bought(
            MagicMock(), {"club": "Челси", "slot_type": "sell", "price": 300,
                          "state": {"bought": 1, "max_extra": 2}}))
        [(topic, text)] = sent
        assert topic == "feed" and "Челси" in text and "продажи" in text
        assert "300" in text and "1 из 2" in text


class TestRoutes:
    def _request(self, method, body=None):
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        request = make_mocked_request(method, "/api/transfers/slots", app=web.Application())
        if body is not None:
            async def _json():
                return body
            request.json = _json
            request._content_type = "application/json"
        return request

    def _call(self, handler, request, monkeypatch, user_id=101):
        monkeypatch.setattr(tapi, "_auth", lambda r: ({"id": user_id}, None))
        monkeypatch.setattr(tapi, "_get_bot", lambda r: None)
        response = asyncio.run(handler(request))
        return response.status, json.loads(response.text)

    def test_get_returns_card(self, monkeypatch):
        _window()
        status, data = self._call(tapi.handle_get_slots, self._request("GET"), monkeypatch)
        assert status == 200 and data["data"]["price"] == PRICE

    def test_post_buys(self, monkeypatch):
        wid = _window()
        status, data = self._call(tapi.handle_post_slot, self._request("POST", {"slot_type": "buy"}), monkeypatch)
        assert status == 200 and data["purchase"]["state"]["bought"] == 1
        assert len(repo.list_slot_purchases(wid)) == 1

    def test_post_error_is_400_with_text(self, monkeypatch):
        _window()
        _balance(101, 0)
        status, data = self._call(tapi.handle_post_slot, self._request("POST", {"slot_type": "buy"}), monkeypatch)
        assert status == 400 and "Не хватает" in data["message"]

    def test_routes_registered(self):
        from aiohttp import web

        app = web.Application()
        tapi.register_routes(app)
        found = {(r.method, r.resource.canonical) for r in app.router.routes()}
        assert ("GET", "/api/transfers/slots") in found and ("POST", "/api/transfers/slots") in found
