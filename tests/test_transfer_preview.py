"""Предпроверка заявки: те же правила, что у подачи, но ничего не записывается."""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import config
import database
from transfers import api as tapi
from transfers import repo, requests as req_mod, service

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
        conn.execute("DELETE FROM transfer_swap_links")
        conn.execute("DELETE FROM transfers")
        conn.execute("DELETE FROM transfer_windows")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     "('transfer_windows', 'transfers', 'transfer_club_budgets')")
    monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", MANAGER, raising=False)
    monkeypatch.setattr(config, "ADMIN_IDS", [990001])


def _user(user_id, username, team):
    with database.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO users (telegram_id, username, team_name) VALUES (?, ?, ?)",
                     (user_id, username, team))


def _window():
    wid = repo.create_window(1, 10, title="ТО Зима")
    repo.open_window(wid, 1)
    _user(101, "chelsea", "Челси")
    _user(102, "arsenal", "Арсенал")
    repo.set_club_budget(wid, "Челси", 50000, 1)
    repo.set_club_budget(wid, "Арсенал", 50000, 1)
    return wid


def _deal(**over):
    f = dict(kind="deal", role="buy", other_club="Арсенал", player="B. Saka", price="15", ovr=106)
    f.update(over)
    return f


def _swap(**over):
    f = dict(kind="swap", other_club="Арсенал", give_player="C. Palmer", give_price="20", give_ovr=105,
             get_player="B. Saka", get_price="15", get_ovr=106)
    f.update(over)
    return f


def _count():
    return len(repo.list_transfers(1))


class TestPreview:
    def test_clean_request_is_ok_and_writes_nothing(self):
        _window()
        res = req_mod.preview(101, "deal", _deal())
        assert res["ok"] and res["blocks"] == [] and res["warnings"] == []
        assert res["price_k"] == 15000
        assert _count() == 0

    def test_hard_block_reported(self):
        _window()
        res = req_mod.preview(101, "deal", _deal(ovr=999))
        assert not res["ok"] and res["blocks"]
        assert _count() == 0

    def test_warning_does_not_block(self):
        _window()
        res = req_mod.preview(101, "deal", _deal(price="80"))      # бюджет 50 млн
        assert res["ok"] and res["warnings"]
        assert _count() == 0

    def test_parse_error_is_a_block(self):
        _window()
        res = req_mod.preview(101, "deal", _deal(price="не число"))
        assert not res["ok"] and res["blocks"]

    def test_closed_window_is_a_block(self):
        _user(101, "chelsea", "Челси")
        _user(102, "arsenal", "Арсенал")
        res = req_mod.preview(101, "deal", _deal())
        assert not res["ok"] and "закрыто" in res["blocks"][0]

    def test_unknown_kind_rejected(self):
        _window()
        with pytest.raises(service.InputError):
            req_mod.preview(101, "nope", {})

    def test_matches_real_submission(self):
        """Предпроверка и подача дают одно и то же — на блокировках и на предупреждениях."""
        _window()
        warn = _deal(price="80")
        pv = req_mod.preview(101, "deal", warn)
        real = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="B. Saka",
                                   price="80", ovr=106)
        assert pv["warnings"] == [w["message"] for w in json.loads(real["warnings"])]
        bad = _deal(ovr=999, player="X. Y")
        pv = req_mod.preview(101, "deal", bad)
        with pytest.raises(service.InputError) as exc:
            req_mod.create_deal(101, role="buy", other_club="Арсенал", player="X. Y", price="15", ovr=999)
        assert "\n".join(pv["blocks"]) == str(exc.value)

    def test_preview_does_not_consume_budget_or_slots(self):
        _window()
        for _ in range(5):
            req_mod.preview(101, "deal", _deal())
        real = req_mod.create_deal(101, role="buy", other_club="Арсенал", player="B. Saka",
                                   price="15", ovr=106)
        assert real["id"] == 1 and _count() == 1


class TestPreviewKinds:
    def test_swap_ok_writes_nothing(self):
        _window()
        res = req_mod.preview(101, "swap", _swap())
        assert res["ok"] and _count() == 0
        with database.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM transfer_swap_links").fetchone()[0] == 0

    def test_swap_second_leg_block_rolls_back_first(self):
        _window()
        res = req_mod.preview(101, "swap", _swap(get_ovr=999))
        assert not res["ok"]
        assert _count() == 0

    def test_swap_warnings_from_both_legs(self):
        _window()
        res = req_mod.preview(101, "swap", _swap(give_price="80", get_price="90"))
        assert res["ok"] and res["warnings"] and _count() == 0

    def test_surcharge_returns_table_price(self):
        _window()
        res = req_mod.preview(101, "surcharge", dict(player="C. Palmer", ovr=100))
        assert res["ok"], res
        assert res["price_k"] == 10000 and _count() == 0

    def test_urn_sale_returns_payout(self):
        _window()
        res = req_mod.preview(101, "urn_sale", dict(player="C. Palmer", tm_price="15", special_price="5",
                                                    sellable="1"))
        assert res["ok"], res
        assert res["price_k"] == 10000 and _count() == 0

    def test_urn_sale_unsellable_divides_by_three(self):
        _window()
        res = req_mod.preview(101, "urn_sale", dict(player="C. Palmer", tm_price="15", special_price="6",
                                                    sellable="0"))
        assert res["ok"] and res["price_k"] == 7000

    def test_urn_buy_missing_item(self):
        _window()
        res = req_mod.preview(101, "urn_buy", dict(urn_item_id=999))
        assert not res["ok"] and "урн" in res["blocks"][0]


class TestApi:
    def _call(self, monkeypatch, fields):
        monkeypatch.setattr(tapi, "_auth", lambda r: ({"id": 101}, None))

        async def _payload(request):
            return fields, None

        monkeypatch.setattr(tapi, "_read_request_payload", _payload)
        request = make_mocked_request("POST", "/api/transfers/preview", app=web.Application())
        return asyncio.run(tapi.handle_post_preview(request))

    def test_registered(self):
        app = web.Application()
        tapi.register_routes(app)
        assert any(r.resource.canonical == "/api/transfers/preview" and r.method == "POST"
                   for r in app.router.routes())

    def test_returns_data_and_writes_nothing(self, monkeypatch):
        _window()
        response = self._call(monkeypatch, _deal())
        body = json.loads(response.text)
        assert response.status == 200 and body["status"] == "ok" and body["data"]["ok"] is True
        assert _count() == 0

    def test_unknown_kind_is_400(self, monkeypatch):
        _window()
        assert self._call(monkeypatch, {"kind": "zzz"}).status == 400
