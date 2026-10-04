"""Рынок ТО: урна + каталог игроков справочника, фильтры и сортировка."""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import database
from transfers import api as tapi, market, repo, requests as req_mod

COACH = 101


@pytest.fixture(autouse=True)
def _clean():
    with database.transaction() as conn:
        for table in ("transfer_squad_ops", "transfer_slot_purchases", "transfer_club_budgets",
                      "transfer_core_snapshot", "transfer_players", "transfer_topics",
                      "transfer_topics_ext", "transfer_sanctions", "squad_players", "users"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE transfers SET urn_item_id = NULL")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM transfer_windows")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     "('transfer_windows', 'transfers', 'transfer_club_budgets')")
        conn.execute("INSERT INTO users (telegram_id, username, team_name) VALUES (?, 'chelsea', 'Челси')",
                     (COACH,))
    wid = repo.create_window(1, 10, title="ТО")
    repo.open_window(wid, 1)
    repo.set_club_budget(wid, "Челси", 100000, 1)
    for name, club, ovr, price in [
        ("Erling Haaland", "Манчестер Сити", 108, 45000),
        ("Bukayo Saka", "Арсенал", 104, 20000),
        ("Martin Odegaard", "Арсенал", 101, 12500),
        ("Reece James", "Челси", 99, 8000),             # свой клуб — в каталог не попадает
        ("Old Timer", "Арсенал", 90, None),
    ]:
        repo.upsert_player(name, last_club=club, ovr=ovr, price_k=price)
    return wid


def _names(items):
    return [i["player_name"] for i in items]


def _urn_item(wid, name="Oldie", ovr=95, tm="10", special="4", club="Арсенал"):
    tid = repo.insert_transfer(wid, "urn_sale", name, "approved", from_club=club, to_club="Урна",
                               price_k=5000, ovr=ovr, tm_price_k=int(float(tm) * 1000),
                               special_price_k=int(float(special) * 1000), sellable=1, initiator_id=102)
    return tid


class TestCatalog:
    def test_own_club_players_hidden_and_default_sort_by_ovr(self):
        data = market.market(COACH)
        assert _names(data["catalog"]) == ["Erling Haaland", "Bukayo Saka", "Martin Odegaard", "Old Timer"]
        assert data["own_club"] == "Челси" and data["catalog_total"] == 4
        assert data["catalog"][0]["price"] and data["catalog"][-1]["price"] is None

    def test_query_filters_by_name_and_typo(self):
        assert _names(market.market(COACH, q="saka")["catalog"]) == ["Bukayo Saka"]
        assert _names(market.market(COACH, q="haland")["catalog"]) == ["Erling Haaland"]
        assert market.market(COACH, q="zzzzzz")["catalog"] == []

    def test_ovr_range(self):
        got = market.market(COACH, ovr_min="100", ovr_max="105")["catalog"]
        assert _names(got) == ["Bukayo Saka", "Martin Odegaard"]
        assert market.market(COACH, ovr_min="abc")["catalog_total"] == 4     # мусор игнорируется

    def test_club_filter(self):
        got = market.market(COACH, club="Арсенал")["catalog"]
        assert set(_names(got)) == {"Bukayo Saka", "Martin Odegaard", "Old Timer"}

    @pytest.mark.parametrize("sort, first", [
        ("price", "Martin Odegaard"), ("price_desc", "Erling Haaland"), ("name", "Bukayo Saka"),
        ("ovr", "Erling Haaland"), ("nonsense", "Erling Haaland"),
    ])
    def test_sorts(self, sort, first):
        assert market.market(COACH, sort=sort)["catalog"][0]["player_name"] == first

    def test_unknown_price_goes_last_when_sorting_cheap_first(self):
        assert market.market(COACH, sort="price")["catalog"][-1]["player_name"] == "Old Timer"

    def test_banned_player_is_flagged(self):
        repo.set_player_ban("Erling Haaland", True, "запрет")
        card = market.market(COACH, q="haaland")["catalog"][0]
        assert card["banned"] is True and card["ban_reason"] == "запрет"

    def test_limit_and_total(self):
        data = market.market(COACH, limit=2)
        assert len(data["catalog"]) == 2 and data["catalog_total"] == 4

    def test_clubs_list_for_the_filter(self):
        assert market.market(COACH)["clubs"] == ["Арсенал", "Манчестер Сити"]


class TestUrn:
    def test_urn_items_listed_and_not_duplicated_in_catalog(self, _clean):
        repo.upsert_player("Oldie", last_club="Урна", ovr=95, price_k=14000)
        _urn_item(_clean)
        data = market.market(COACH)
        assert _names(data["urn"]) == ["Oldie"] and data["urn"][0]["buy_price_k"] == 14000
        assert "Oldie" not in _names(data["catalog"]) and data["can_buy"] is True

    def test_urn_filters_apply(self, _clean):
        _urn_item(_clean, "Oldie", ovr=95)
        _urn_item(_clean, "Veteran", ovr=88, club="Манчестер Сити")
        assert _names(market.market(COACH, ovr_min="90")["urn"]) == ["Oldie"]
        assert _names(market.market(COACH, club="Манчестер Сити")["urn"]) == ["Veteran"]
        assert _names(market.market(COACH, q="vet")["urn"]) == ["Veteran"]
        assert _names(market.market(COACH, sort="price")["urn"])[0] in ("Oldie", "Veteran")

    def test_bought_out_item_is_gone(self, _clean):
        tid = _urn_item(_clean)
        repo.insert_transfer(_clean, "urn_buy", "Oldie", "pending_manager", from_club="Урна",
                             to_club="Челси", to_user=COACH, price_k=14000, ovr=95, urn_item_id=tid,
                             initiator_id=COACH)
        assert market.market(COACH)["urn"] == []

    def test_user_without_club_cannot_buy(self, _clean):
        _urn_item(_clean)
        data = market.market(555)
        assert data["can_buy"] is False and data["own_club"] is None

    def test_no_active_window(self, _clean):
        repo.close_window(_clean, 1)
        data = market.market(COACH)
        assert data["window"] is None and data["urn"] == []


class TestRoute:
    def _call(self, monkeypatch, query):
        monkeypatch.setattr(tapi, "_auth", lambda r: ({"id": COACH}, None))
        request = make_mocked_request("GET", "/api/transfers/market" + query, app=web.Application())
        response = asyncio.run(tapi.handle_get_market(request))
        return response.status, json.loads(response.text)

    def test_registered(self):
        app = web.Application()
        tapi.register_routes(app)
        assert any(r.resource.canonical == "/api/transfers/market" for r in app.router.routes())

    def test_filters_passed_through(self, monkeypatch):
        status, body = self._call(monkeypatch, "?q=saka&ovr_min=100&club=Арсенал&sort=price")
        assert status == 200 and body["status"] == "ok"
        assert _names(body["data"]["catalog"]) == ["Bukayo Saka"]

    def test_unauthorized_passes_through(self, monkeypatch):
        denied = web.json_response({"status": "error"}, status=401)
        monkeypatch.setattr(tapi, "_auth", lambda r: (None, denied))
        request = make_mocked_request("GET", "/api/transfers/market", app=web.Application())
        assert asyncio.run(tapi.handle_get_market(request)).status == 401
