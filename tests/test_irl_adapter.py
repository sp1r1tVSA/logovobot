"""tests/test_irl_adapter.py — pre-match methods of the API-Sports adapter (network mocked)."""

import asyncio
from datetime import datetime

import pytest

from services.sports.adapters.api_sports import APISportsProvider
from services.sports.adapters.base import SportsDataProvider
from services.sports.adapters.null_provider import NullSportsDataProvider


def run(coro):
    return asyncio.run(coro)


def make_fixture(fid=100, league=39, home="Arsenal", away="Chelsea",
                 date="2026-10-03T19:30:00+03:00", short="NS", ft=(None, None)):
    return {
        "fixture": {"id": fid, "date": date, "status": {"short": short}},
        "league": {"id": league, "name": "Premier League", "country": "England"},
        "teams": {"home": {"id": 1, "name": home}, "away": {"id": 2, "name": away}},
        "goals": {"home": ft[0], "away": ft[1]},
        "score": {"fulltime": {"home": ft[0], "away": ft[1]}},
    }


def make_odds(bm_id=8, values=None, bet_id=1, update="2026-10-03T10:00:00+00:00"):
    if values is None:
        values = [{"value": "Home", "odd": "1.85"}, {"value": "Draw", "odd": "3.60"},
                  {"value": "Away", "odd": "4.20"}]
    return {"update": update, "bookmakers": [
        {"id": bm_id, "name": "X", "bets": [{"id": bet_id, "name": "Match Winner", "values": values}]}]}


class Fake(APISportsProvider):
    """Adapter whose transport returns canned payloads and records the requests."""

    def __init__(self, payload=None, boom=False):
        super().__init__(api_key="test-key-not-real")
        self.payload, self.boom, self.calls = payload, boom, []

    async def _fetch_json(self, endpoint, params=None, cache_ttl=None):
        self.calls.append((endpoint, dict(params or {})))
        if self.boom:
            raise RuntimeError("down")
        return self.payload


def ok(resp):
    return {"errors": [], "response": resp}


class TestFixtures:
    def test_one_request_for_the_day_in_msk_and_local_league_filter(self):
        p = Fake(ok([make_fixture(1, 39), make_fixture(2, 140), make_fixture(3, 2)]))
        got = run(p.get_prematch_fixtures("2026-10-03", [39, 2]))
        assert [f.fixture_id for f in got] == [1, 3]
        assert p.calls == [("fixtures", {"date": "2026-10-03", "timezone": "Europe/Moscow"})]

    def test_no_filter_returns_all(self):
        p = Fake(ok([make_fixture(1), make_fixture(2, league=140)]))
        assert len(run(p.get_prematch_fixtures("2026-10-03"))) == 2

    def test_kickoff_is_naive_msk_for_any_offset(self):
        p = Fake(ok([make_fixture(1, date="2026-10-03T16:30:00+00:00"),
                     make_fixture(2, date="2026-10-03T19:30:00+03:00")]))
        a, b = run(p.get_prematch_fixtures("2026-10-03"))
        assert a.kickoff == b.kickoff == datetime(2026, 10, 3, 19, 30)
        assert a.kickoff.tzinfo is None

    def test_empty_day_is_empty_list_but_failure_is_none(self):
        assert run(Fake(ok([])).get_prematch_fixtures("2026-10-03")) == []
        assert run(Fake(boom=True).get_prematch_fixtures("2026-10-03")) is None
        assert run(Fake({"errors": {"plan": "Free plans do not have access"}, "response": []})
                   .get_prematch_fixtures("2026-10-03")) is None
        assert run(Fake({"response": "oops"}).get_prematch_fixtures("2026-10-03")) is None

    def test_broken_rows_are_skipped(self):
        bad = [make_fixture(1, home=""), {"fixture": {}}, None,
               make_fixture(3, date="not a date"), make_fixture(4)]
        got = run(Fake(ok(bad)).get_prematch_fixtures("2026-10-03"))
        assert [f.fixture_id for f in got] == [4]

    def test_no_key_means_unavailable(self):
        p = Fake(ok([]))
        p.api_key = ""
        assert run(p.get_prematch_fixtures("2026-10-03")) is None
        assert p.calls == []

    def test_season_comes_from_the_league_block(self):
        raw = make_fixture(1)
        raw["league"]["season"] = 2026
        no_season = make_fixture(2)
        a, b = run(Fake(ok([raw, no_season])).get_prematch_fixtures("2026-10-03"))
        assert (a.season, b.season) == (2026, None)

    def test_names_are_kept_as_the_provider_sent_them(self):
        got = run(Fake(ok([make_fixture(1, home="Real Madrid", away="Inter")]))
                  .get_prematch_fixtures("2026-10-03"))
        assert (got[0].home, got[0].away) == ("Real Madrid", "Inter")


class TestResult:
    def test_finished_match_has_main_time_score(self):
        p = Fake(ok([make_fixture(9, short="FT", ft=(2, 1))]))
        fx = run(p.get_prematch_fixture(9))
        assert (fx.status_short, fx.home_goals, fx.away_goals) == ("FT", 2, 1)
        assert p.calls[0][1]["id"] == 9

    def test_penalties_keep_the_ninety_minute_score(self):
        raw = make_fixture(9, short="PEN", ft=(1, 1))
        raw["goals"] = {"home": 4, "away": 5}
        raw["score"]["penalty"] = {"home": 4, "away": 5}
        fx = run(Fake(ok([raw])).get_prematch_fixture(9))
        assert (fx.status_short, fx.home_goals, fx.away_goals) == ("PEN", 1, 1)

    def test_no_score_stays_none_never_zero(self):
        fx = run(Fake(ok([make_fixture(9, short="1H")])).get_prematch_fixture(9))
        assert fx.home_goals is None and fx.away_goals is None

    def test_unknown_or_failed_is_none(self):
        assert run(Fake(ok([])).get_prematch_fixture(9)) is None
        assert run(Fake(boom=True).get_prematch_fixture(9)) is None


class TestOdds:
    def test_bookmaker_1x2(self):
        p = Fake(ok([make_odds()]))
        o = run(p.get_match_winner_odds(100, 8))
        assert (o.home, o.draw, o.away, o.bookmaker_id, o.fixture_id) == (1.85, 3.6, 4.2, 8, 100)
        assert o.updated_at == datetime(2026, 10, 3, 13, 0)
        assert p.calls == [("odds", {"fixture": 100, "bookmaker": 8})]

    def test_other_bookmaker_in_answer_is_ignored(self):
        assert run(Fake(ok([make_odds(bm_id=11)])).get_match_winner_odds(100, 8)) is None

    def test_other_market_is_ignored(self):
        assert run(Fake(ok([make_odds(bet_id=5)])).get_match_winner_odds(100, 8)) is not None  # by name
        item = make_odds(bet_id=5)
        item["bookmakers"][0]["bets"][0]["name"] = "Goals Over/Under"
        assert run(Fake(ok([item])).get_match_winner_odds(100, 8)) is None

    @pytest.mark.parametrize("values", [
        [{"value": "Home", "odd": "1.85"}, {"value": "Draw", "odd": "3.60"}],            # missing
        [{"value": "Home", "odd": "1.85"}, {"value": "Draw", "odd": "3.60"},
         {"value": "Draw", "odd": "3.70"}],                                              # repeated
        [{"value": "Home", "odd": "1.85"}, {"value": "Draw", "odd": "3.60"},
         {"value": "Away", "odd": "1.00"}],                                              # not > 1
        [{"value": "Home", "odd": "1.85"}, {"value": "Draw", "odd": "nan"},
         {"value": "Away", "odd": "4.2"}],                                               # NaN
        [{"value": "Home", "odd": "1.85"}, {"value": "Draw", "odd": None},
         {"value": "Away", "odd": "4.2"}],                                               # null
        [{"value": "Home", "odd": "1.85"}, {"value": "Draw", "odd": "3.6"},
         {"value": "Away", "odd": "5000"}],                                              # absurd
        [{"value": "Home", "odd": "1.85"}, {"value": "Draw", "odd": "3.6"},
         {"value": "Away", "odd": "4.2"}, {"value": "Extra", "odd": "9"}],               # unknown key
    ])
    def test_incomplete_or_bad_market_is_none(self, values):
        assert run(Fake(ok([make_odds(values=values)])).get_match_winner_odds(100, 8)) is None

    def test_failures_are_none(self):
        assert run(Fake(ok([])).get_match_winner_odds(100, 8)) is None
        assert run(Fake(boom=True).get_match_winner_odds(100, 8)) is None


class TestTransport:
    def test_errors_payload_is_not_cached(self, monkeypatch):
        import aiohttp

        bodies = [{"errors": {"rateLimit": "slow down"}, "response": []}, ok([make_fixture(1)])]
        hits = []

        class Resp:
            status = 200
            headers = {}

            def __init__(self, body):
                self.body = body

            async def json(self):
                return self.body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Session:
            def __init__(self, *a, **k):
                pass

            def get(self, url, headers=None, params=None):
                hits.append(url)
                return Resp(bodies[len(hits) - 1])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(aiohttp, "ClientSession", Session)
        p = APISportsProvider(api_key="test-key-not-real")
        assert run(p.get_prematch_fixtures("2026-10-03")) is None
        assert len(run(p.get_prematch_fixtures("2026-10-03"))) == 1   # not served from a cached error
        assert len(hits) == 2


class TestBaseDefaults:
    def test_providers_without_prematch_data_report_unavailable(self):
        n = NullSportsDataProvider(reason="x")
        assert isinstance(n, SportsDataProvider)
        assert run(n.get_prematch_fixtures("2026-10-03")) is None
        assert run(n.get_prematch_fixture(1)) is None
        assert run(n.get_match_winner_odds(1, 8)) is None
