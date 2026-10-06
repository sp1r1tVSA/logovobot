"""Автоподбор клуба/игрока в заявках ТО и догрузка портретов одобренных заявок."""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import config
import database
from transfers import api as tapi, repo, requests as req_mod, suggest

CLUBS = ["Челси", "Арсенал", "Манчестер Сити", "Манчестер Юнайтед", "Реал Мадрид", "Пари Сен-Жермен"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with database.transaction() as conn:
        for table in ("transfer_players", "squad_players", "users"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("INSERT INTO users (telegram_id, username, team_name) VALUES (101, 'chelsea', 'Челси')")
        for team, name in [("Челси", "K. De Bruyne"), ("Арсенал", "B. Saka"), ("Арсенал", "Martin Odegaard"),
                           ("Манчестер Сити", "Erling Haaland"), ("Манчестер Сити", "Kevin De Bruyne")]:
            conn.execute("INSERT INTO squad_players (team_name, player_name, position) VALUES (?, ?, 'X')",
                         (team, name))
    monkeypatch.setattr(config, "CLUB_REGISTRY", CLUBS, raising=False)


def _names(items):
    return [i["name"] for i in items]


class TestClubs:
    def test_prefix_and_word_prefix(self):
        assert _names(suggest.clubs(999, "ман")) == ["Манчестер Сити", "Манчестер Юнайтед"]
        assert _names(suggest.clubs(999, "сити")) == ["Манчестер Сити"]

    def test_own_club_and_urn_are_never_offered(self):
        assert _names(suggest.clubs(101, "челс")) == []
        assert "Урна" not in _names(suggest.clubs(999, "урн"))

    def test_alias_finds_canonical_club(self, monkeypatch):
        import club_registry
        monkeypatch.setattr(club_registry, "TEAM_ALIASES", {"мю": "Манчестер Юнайтед"})
        assert _names(suggest.clubs(999, "МЮ")) == ["Манчестер Юнайтед"]

    def test_typo_and_empty(self):
        assert _names(suggest.clubs(999, "арсинал")) == ["Арсенал"]
        assert suggest.clubs(999, "   ") == []

    def test_limit(self):
        assert len(suggest.clubs(999, "а", limit=2)) <= 2


class TestPlayers:
    def test_matches_by_surname_and_diacritics(self):
        assert _names(suggest.players(999, "saka")) == ["B. Saka"]
        assert _names(suggest.players(999, "odegard")) == ["Martin Odegaard"]

    def test_named_club_only_returns_club_players(self):
        got = suggest.players(999, "bruyne", club="Манчестер Сити")
        assert [(g["name"], g["club"]) for g in got] == [("Kevin De Bruyne", "Манчестер Сити")]

    def test_own_scope_strictly_uses_coach_club(self):
        got = suggest.players(101, "bruyne", own=True)
        assert [(g["name"], g["club"]) for g in got] == [("K. De Bruyne", "Челси")]
        assert suggest.players(999, "bruyne", own=True) == []

    def test_unknown_club_returns_empty(self):
        assert suggest.players(999, "bruyne", club="Несуществующий Клуб") == []

    def test_pool_players_are_offered_once(self):
        repo.upsert_player("Old Timer", last_club="Челси")
        repo.upsert_player("B. Saka", last_club="Арсенал")
        assert _names(suggest.players(999, "old")) == ["Old Timer"]
        assert _names(suggest.players(999, "saka")) == ["B. Saka"]

    def test_empty_query(self):
        assert suggest.players(999, "") == []


class TestRoute:
    def _call(self, monkeypatch, query):
        monkeypatch.setattr(tapi, "_auth", lambda r: ({"id": 101}, None))
        request = make_mocked_request("GET", "/api/transfers/suggest" + query, app=web.Application())
        response = asyncio.run(tapi.handle_get_suggest(request))
        return response.status, json.loads(response.text)

    def test_registered(self):
        app = web.Application()
        tapi.register_routes(app)
        assert any(r.resource.canonical == "/api/transfers/suggest" for r in app.router.routes())

    def test_club_and_player(self, monkeypatch):
        status, body = self._call(monkeypatch, "?kind=club&q=арс")
        assert status == 200 and body["data"] == [{"name": "Арсенал"}]
        status, body = self._call(monkeypatch, "?kind=player&q=saka&club=Арсенал")
        assert body["data"] == [{"name": "B. Saka", "club": "Арсенал", "cards": []}]
        _, body = self._call(monkeypatch, "?kind=player&q=bruyne&own=1")
        assert body["data"][0]["club"] == "Челси"

    def test_bad_kind(self, monkeypatch):
        status, body = self._call(monkeypatch, "?kind=x&q=a")
        assert status == 400 and body["status"] == "error"


class TestBackfill:
    def test_counts_cached_fetched_missing_and_dedupes(self, monkeypatch):
        from services.graphics import player_photos
        monkeypatch.setattr(req_mod, "portrait_url", lambda name, *c, **kw: "/x.png" if name == "Cached" else None)
        monkeypatch.setattr(player_photos, "fetch_and_cache",
                            lambda name, team=None, **kw: "/y.png" if name == "Fresh" else None)
        items = [
            {"player_name": "Cached", "from_club": "Челси", "to_club": "Арсенал"},
            {"player_name": "Fresh", "from_club": "Челси", "to_club": "Арсенал"},
            {"player_name": "fresh", "from_club": "Арсенал", "to_club": "Челси"},
            {"player_name": "Ghost", "from_club": "Челси", "to_club": req_mod.URN_CLUB},
            {"player_name": "Urn Only", "from_club": req_mod.URN_CLUB, "to_club": None},
            {"player_name": "", "from_club": "Челси"},
        ]
        stats = req_mod.backfill_portraits(items)
        assert stats == {"total": 4, "cached": 1, "fetched": 1, "missing": ["Ghost", "Urn Only"]}

    def test_never_raises_when_network_fails(self, monkeypatch):
        from services.graphics import player_photos

        def boom(*a, **kw):
            raise RuntimeError("down")

        monkeypatch.setattr(req_mod, "portrait_url", lambda *a, **kw: None)
        monkeypatch.setattr(player_photos, "fetch_and_cache", boom)
        stats = req_mod.backfill_portraits([{"player_name": "A", "to_club": "Челси"}])
        assert stats["missing"] == ["A"]
